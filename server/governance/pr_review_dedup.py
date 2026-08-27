"""PRReviewDedupService — PR Review 语义去重服务（SubTask 9.1）。

对应技术方案 3.2.1 + 关键风险提醒：
- 内容级去重：content_hash 精确匹配 → 建议撤回
- 语义级去重：计算 embedding，相似度 ≥0.92 进入"待归并"
- ≥0.92 相似度时用 LLM 判断"归并 vs 独立"
- 命中后归并建议作为 PR Review 评论呈现，由 Reviewer 决定

设计要点：
- LLM 调用通过 LLMChatLike 协议注入（Agent 7 LLMProvider 已提供）
- LLM 不可用时降级为"needs_review"（不阻断 PR）
- 批量处理：传入 pr_id + assets 列表，逐条去重
- 仅与同 type 资产比对（避免跨类型误判）
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from typing import Any, Protocol

from sqlalchemy import select

from server.governance.models import (
    DedupSuggestion,
    DuplicateMatch,
    PRReviewDedupResult,
)
from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import AssetIndex as AssetIndexRow
from server.infra_db.vectorstore import VectorRecord, VectorStore

logger = logging.getLogger(__name__)


# 相似度阈值（红线遵守：≥0.92 触发 LLM 判断）
SIMILARITY_THRESHOLD = 0.92


class LLMChatLike(Protocol):
    """LLM Provider 协议（与 Agent 7 LLMProviderClient / LocalLLMProvider 对齐）。

    chat(messages, *, schema, model, temperature, max_tokens) →
        {"content": str, "usage": dict, "model": str}
    """

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """LLM chat 协议方法。"""
        ...


class PRReviewDedupService:
    """PR Review 语义去重服务。

    用法：
        svc = PRReviewDedupService(
            database=db,
            asset_index=asset_index,
            embedding_service=emb,
            vector_store=vs,
            llm=llm_provider,  # Agent 7 LLMProvider 注入
        )
        result = svc.review_pr(
            pr_id="pr-123",
            assets=[
                {"id": "rule-x", "type": "rule", "content": "...", "content_hash": "...", "git_path": "..."},
            ],
        )
    """

    def __init__(
        self,
        *,
        database: Database,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        llm: LLMChatLike | None = None,
        similarity_threshold: float = SIMILARITY_THRESHOLD,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._embedding = embedding_service
        self._vector_store = vector_store
        self._llm = llm
        self._threshold = similarity_threshold

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def review_pr(
        self,
        *,
        pr_id: str,
        assets: list[dict[str, Any]],
    ) -> PRReviewDedupResult:
        """对 PR 内的新增/修改资产做语义去重。

        assets 每项含：
        - id: 资产 id
        - type: 资产类型（rule/memory/skill/tool/prompt）
        - content: 资产文本内容
        - content_hash: 内容 hash（用于精确匹配）
        - git_path: git 路径
        - module_path: 模块路径（可选）
        """
        result = PRReviewDedupResult(pr_id=pr_id)

        for asset_data in assets:
            suggestion = self._dedup_single(asset_data)
            result.suggestions.append(suggestion)
            result.total_duplicates += len(suggestion.duplicates)
            # 统计 LLM 调用/失败次数（duplicates 中 llm_decision != unknown 即调用过 LLM）
            for dup in suggestion.duplicates:
                if dup.llm_decision != "unknown":
                    result.llm_calls += 1
            if suggestion.llm_error:
                result.llm_errors += 1

        return result

    # ------------------------------------------------------------------
    # 内部：单条资产去重
    # ------------------------------------------------------------------

    def _dedup_single(self, asset_data: dict[str, Any]) -> DedupSuggestion:
        """对单条新资产做内容级 + 语义级去重。"""
        new_id = str(asset_data.get("id", ""))
        new_path = str(asset_data.get("git_path", ""))
        asset_type = str(asset_data.get("type", "rule"))
        content = str(asset_data.get("content", ""))
        content_hash = str(asset_data.get("content_hash", "") or "")

        suggestion = DedupSuggestion(
            new_asset_id=new_id,
            new_asset_path=new_path,
        )

        if not content:
            suggestion.suggestion = "skip"
            suggestion.llm_error = "资产内容为空，跳过去重"
            return suggestion

        # 1. 内容级去重：content_hash 精确匹配
        if content_hash:
            exact_match = self._find_exact_match(content_hash, exclude_id=new_id)
            if exact_match:
                suggestion.duplicates.append(
                    DuplicateMatch(
                        asset_id=exact_match.id,
                        git_path=exact_match.git_path,
                        module_path=exact_match.module_path,
                        similarity=1.0,
                        llm_decision="merge",
                        llm_rationale="content_hash 完全一致，建议撤回重复资产",
                    )
                )
                suggestion.suggestion = "merge"
                return suggestion

        # 2. 语义级去重：计算 embedding + 相似度检索
        candidates = self._find_semantic_candidates(
            content=content,
            asset_type=asset_type,
            exclude_id=new_id,
        )

        if not candidates:
            suggestion.suggestion = "keep_separate"
            return suggestion

        # 3. ≥0.92 相似度时用 LLM 判断"归并 vs 独立"
        for candidate in candidates:
            if candidate.similarity < self._threshold:
                continue
            llm_decision, llm_rationale, llm_error = self._llm_judge(
                new_content=content,
                new_id=new_id,
                candidate=candidate,
            )
            suggestion.duplicates.append(
                DuplicateMatch(
                    asset_id=candidate.asset_id,
                    git_path=candidate.git_path,
                    module_path=candidate.module_path,
                    similarity=candidate.similarity,
                    llm_decision=llm_decision,
                    llm_rationale=llm_rationale,
                )
            )
            if llm_error:
                suggestion.llm_error = llm_error

        # 综合建议：若全部为 merge → merge；若全部为 independent → keep_separate；
        # 混合或 LLM 失败 → needs_review
        if suggestion.duplicates:
            all_merge = all(d.llm_decision == "merge" for d in suggestion.duplicates)
            all_independent = all(
                d.llm_decision == "independent" for d in suggestion.duplicates
            )
            if all_merge:
                suggestion.suggestion = "merge"
            elif all_independent:
                suggestion.suggestion = "keep_separate"
            else:
                suggestion.suggestion = "needs_review"
        else:
            # 有候选但无一达到 ≥threshold → 视为独立资产
            suggestion.suggestion = "keep_separate"

        return suggestion

    # ------------------------------------------------------------------
    # 内部：内容级精确匹配
    # ------------------------------------------------------------------

    def _find_exact_match(
        self, content_hash: str, *, exclude_id: str = ""
    ) -> AssetIndexRow | None:
        """按 content_hash 精确匹配（仅 active 资产）。"""
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.content_hash == content_hash)
                .where(AssetIndexRow.status == "active")
            )
            if exclude_id:
                stmt = stmt.where(AssetIndexRow.id != exclude_id)
            return sess.scalars(stmt).first()

    # ------------------------------------------------------------------
    # 内部：语义级相似度检索
    # ------------------------------------------------------------------

    def _find_semantic_candidates(
        self,
        *,
        content: str,
        asset_type: str,
        exclude_id: str = "",
    ) -> list[SemanticCandidate]:
        """计算 embedding + 相似度检索同 type 资产。

        流程：
        1. 计算新资产内容的 embedding
        2. vector_store.search 检索 top-K 候选
        3. 同步过滤：仅同 type + status=active + exclude_id
        4. 返回候选清单（含 asset_id / git_path / module_path / similarity）
        """
        emb = self._embedding.embed(content)
        active_version = self._embedding.get_active_version()
        # 多召回用于 type 过滤后仍够数
        top_k = 20
        hits = self._vector_store.search(
            emb.vector,
            model_version=active_version,
            top_k=top_k,
        )

        # 收集候选 asset_id（仅命中向量库的）
        hit_ids = [h.asset_id for h in hits]
        if not hit_ids:
            return []

        score_map = {h.asset_id: float(h.score) for h in hits}

        # 查 asset_index 元数据 + 同 type 过滤
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.id.in_(hit_ids))
                .where(AssetIndexRow.type == asset_type)
                .where(AssetIndexRow.status == "active")
            )
            if exclude_id:
                stmt = stmt.where(AssetIndexRow.id != exclude_id)
            rows = list(sess.scalars(stmt))

        candidates: list[SemanticCandidate] = []
        for row in rows:
            sim = score_map.get(row.id, 0.0)
            candidates.append(
                SemanticCandidate(
                    asset_id=row.id,
                    git_path=row.git_path,
                    module_path=row.module_path,
                    similarity=sim,
                    content_snapshot=row.content_snapshot or "",
                )
            )
        # 按相似度降序
        candidates.sort(key=lambda c: c.similarity, reverse=True)
        return candidates

    # ------------------------------------------------------------------
    # 内部：LLM 判断归并 vs 独立
    # ------------------------------------------------------------------

    def _llm_judge(
        self,
        *,
        new_content: str,
        new_id: str,
        candidate: SemanticCandidate,
    ) -> tuple[str, str, str]:
        """LLM 判断"同一规则不同表述" vs "相似但独立"。

        返回 (decision, rationale, error)。
        - decision: merge（归并）/ independent（独立）
        - rationale: LLM 推理理由
        - error: LLM 调用错误（若有）

        LLM 不可用时降级为 needs_review（不阻断 PR）。
        """
        if self._llm is None:
            return ("needs_review", "", "LLM Provider 未注入，无法判断")

        # 构造 LLM prompt（参考技术方案 3.2.1）
        system_msg = (
            "你是 PR Review 语义去重助手。下面给出两条资产，它们语义相似度已 ≥0.92。"
            "请判断它们是「同一规则的不同表述」（应归并）还是「相似但独立」（应保留为两条）。"
            "严格按 JSON 格式输出：{\"decision\": \"merge\"|\"independent\", \"rationale\": \"...\"}"
        )
        user_msg = (
            f"【新资产 id={new_id}】\n{new_content[:800]}\n\n"
            f"【已有资产 id={candidate.asset_id}】\n{candidate.content_snapshot[:800]}\n\n"
            "判断维度：\n"
            "1. 核心意图是否一致（剥离具体表述后是否指向同一规则/经验）\n"
            "2. 适用场景是否重叠（若完全重叠 → merge；若互补场景 → independent）\n"
            "3. 是否存在新增信息（新资产是否带来 candidate 没有的内容）\n"
            "请严格输出 JSON。"
        )

        messages = [
            {"role": "system", "content": system_msg},
            {"role": "user", "content": user_msg},
        ]

        try:
            # 用 schema 强制 JSON 输出
            schema = {
                "type": "object",
                "properties": {
                    "decision": {"type": "string", "enum": ["merge", "independent"]},
                    "rationale": {"type": "string"},
                },
                "required": ["decision", "rationale"],
            }
            result = self._llm.chat(
                messages,
                schema=schema,
                temperature=0.1,
                max_tokens=300,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM 判断失败 new=%s candidate=%s err=%s", new_id, candidate.asset_id, exc)
            return ("needs_review", "", f"LLM 调用失败: {exc}")

        content_str = str(result.get("content", ""))
        decision, rationale = _parse_llm_json(content_str)
        if not decision:
            return ("needs_review", "", f"LLM 响应无法解析: {content_str[:200]}")
        return (decision, rationale, "")


# ---------------------------------------------------------------------------
# 内部数据结构
# ---------------------------------------------------------------------------


@dataclass
class SemanticCandidate:
    """向量检索候选。"""

    asset_id: str
    git_path: str
    module_path: str
    similarity: float
    content_snapshot: str


__all__ = [
    "LLMChatLike",
    "PRReviewDedupService",
    "SIMILARITY_THRESHOLD",
    "SemanticCandidate",
]


# ---------------------------------------------------------------------------
# 辅助：解析 LLM JSON 响应
# ---------------------------------------------------------------------------


def _parse_llm_json(content: str) -> tuple[str, str]:
    """解析 LLM 返回的 JSON {decision, rationale}。

    容错：若 LLM 未严格按 JSON 输出（如 ```json 代码块包裹），尝试提取。
    """
    if not content:
        return ("", "")
    text = content.strip()
    # 去掉 markdown 代码块包裹
    if text.startswith("```"):
        # 去掉首行 ```json 或 ```
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:-1] if lines[-1].strip() == "```" else lines[1:])
    try:
        data = json.loads(text)
        if isinstance(data, dict):
            decision = str(data.get("decision", "")).lower()
            rationale = str(data.get("rationale", ""))
            if decision in ("merge", "independent"):
                return (decision, rationale)
    except (json.JSONDecodeError, ValueError):
        pass
    return ("", "")
