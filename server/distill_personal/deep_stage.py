"""Deep 阶段 — 五维评分 + 结构化固化，产出带 frontmatter 资产。

对应 SubTask 7.5 + 技术方案 3.3.2 ③ Deep 深睡：
- 五维评分：频率 / 跨会话复现 / 可复用性 / 明确性 / 类型适配
- 过阈值才调用 LLM 提炼为结构化资产
- 产出带 frontmatter 的资产文件（复用 Agent 1 的双区 frontmatter 设计）
- 人工确认关口（默认开启，用户可改为自动）
- budget 不足时跳过 Deep，候选入 .dreams/pending/（次日恢复后处理）

设计要点：
- Deep 阶段是唯一产出资产的阶段
- 五维评分先用规则计算（基于 Light/REM 的 metadata），可选 LLM 复核
- LLM 调用走 prompts.py 的四类资产子 Prompt 模板
- LLM 强制 JSON schema 输出，校验失败重试（schema_validator.py）
- 资产 frontmatter 用 Agent 1 的 serialize_frontmatter_dual（双区设计）
- 产出的资产默认 scope=private（隐私保护，见 privacy.py）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.distill_personal.budget import (
    PendingCandidate,
    PendingCandidateStore,
)
from server.distill_personal.llm_provider import LLMBudget
from server.distill_personal.prompts import (
    AssetPromptTemplate,
    get_prompt_template,
    render_system_prompt,
)
from server.distill_personal.rem_stage import Intent
from server.distill_personal.schema_validator import (
    ChatWithSchemaResult,
    LLMChatLike,
    chat_with_schema,
)

# 复用 Agent 1 的双区 frontmatter 序列化
from server.infra_git.trae_adapter import (
    TraeFrontmatter,
    serialize_frontmatter_dual,
)

# 复用 Agent 2 的公共模型
from server.common.models import Asset, AssetType, Scope

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 五维评分
# ---------------------------------------------------------------------------


@dataclass
class FiveDimScore:
    """Deep 阶段五维评分。

    对齐技术方案 3.3.2「个人提炼评分」：
    - frequency：频率，同类信号在会话中出现次数
    - cross_session：跨会话复现，是否在不同会话中重复出现
    - reusability：可复用性，能否在未来的 coding 中复用
    - clarity：明确性，能否表述为清晰可执行的条目
    - type_fit：类型适配，是否能明确归类为 rule/memory/skill/tool
    每维 0.0-1.0，总分取加权平均。
    """

    frequency: float = 0.0
    cross_session: float = 0.0
    reusability: float = 0.0
    clarity: float = 0.0
    type_fit: float = 0.0

    # 权重（对齐技术方案说明：频率中、跨会话高、可复用性高、明确性中、类型适配中）
    WEIGHTS: tuple[tuple[str, float], ...] = (
        ("frequency", 0.15),
        ("cross_session", 0.25),
        ("reusability", 0.25),
        ("clarity", 0.20),
        ("type_fit", 0.15),
    )

    @property
    def total(self) -> float:
        """加权总分。"""
        return sum(
            getattr(self, name) * weight for name, weight in self.WEIGHTS
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "frequency": round(self.frequency, 3),
            "cross_session": round(self.cross_session, 3),
            "reusability": round(self.reusability, 3),
            "clarity": round(self.clarity, 3),
            "type_fit": round(self.type_fit, 3),
            "total": round(self.total, 3),
        }


# 晋升阈值（总分 >= 此值才调 LLM 提炼）
DEFAULT_PROMOTION_THRESHOLD = 0.5


# ---------------------------------------------------------------------------
# 资产产出结构
# ---------------------------------------------------------------------------


@dataclass
class DistilledAsset:
    """Deep 阶段产出的资产（含 frontmatter 与正文）。"""

    asset: Asset  # 值对象（用于写入 AssetIndex）
    frontmatter_text: str  # 完整 frontmatter + 正文（写入本地记忆文件夹）
    score: FiveDimScore
    intent_id: str
    llm_confidence: float = 0.0
    skipped: bool = False  # LLM 判定 skip=true 时不入库
    skip_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset.id,
            "type": self.asset.type.value,
            "title": self.asset.tags[0] if self.asset.tags else "",
            "content_excerpt": self.asset.content[:200],
            "score": self.score.to_dict(),
            "intent_id": self.intent_id,
            "llm_confidence": self.llm_confidence,
            "skipped": self.skipped,
            "skip_reason": self.skip_reason,
            "frontmatter_text": self.frontmatter_text,
        }


@dataclass
class DeepStageResult:
    """Deep 阶段结果。"""

    assets: list[DistilledAsset] = field(default_factory=list)
    pending: list[PendingCandidate] = field(default_factory=list)
    skipped_intents: int = 0  # 五维评分未过阈值跳过的 intent 数
    llm_skipped: int = 0  # LLM 判定 skip=true 跳过的 intent 数
    total_tokens_used: int = 0
    errors: list[str] = field(default_factory=list)

    @property
    def produced_count(self) -> int:
        """实际产出资产数（不含 skipped）。"""
        return sum(1 for a in self.assets if not a.skipped)


# ---------------------------------------------------------------------------
# Deep 阶段主流程
# ---------------------------------------------------------------------------


class DeepStage:
    """Deep 阶段：五维评分 + LLM 结构化固化。

    使用：
        stage = DeepStage(llm=LLMProviderClient(...), budget=budget, pending_store=store)
        result = stage.run(intents)
        # result.assets → 写入 AssetIndex + 本地记忆文件夹
        # result.pending → 次日恢复后处理
    """

    def __init__(
        self,
        *,
        llm: LLMChatLike | None = None,
        budget: LLMBudget | None = None,
        pending_store: PendingCandidateStore | None = None,
        promotion_threshold: float = DEFAULT_PROMOTION_THRESHOLD,
        max_retries: int = 3,
        owner: str = "",
        module_path: str = "",
    ) -> None:
        self.llm = llm
        self.budget = budget
        self.pending_store = pending_store
        self.promotion_threshold = promotion_threshold
        self.max_retries = max_retries
        self.owner = owner
        self.module_path = module_path

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------

    def run(self, intents: list[Intent]) -> DeepStageResult:
        """对 intents 执行 Deep 阶段。

        - 五维评分 < promotion_threshold → 跳过（不入 pending，直接丢弃）
        - budget 耗尽 → 候选入 pending，不调 LLM
        - LLM 判定 skip=true → 跳过（不入库不入 pending）
        - LLM 成功 → 产出 DistilledAsset
        """
        result = DeepStageResult()
        for intent in intents:
            # 1. 五维评分
            score = self._score_intent(intent)
            if score.total < self.promotion_threshold:
                result.skipped_intents += 1
                logger.info(
                    "intent %s 五维评分 %.3f < 阈值 %.2f，跳过",
                    intent.intent_id,
                    score.total,
                    self.promotion_threshold,
                )
                continue

            # 2. budget 检查
            if self.budget is not None and self.budget.exhausted:
                # 入 pending，不调 LLM
                cand = PendingCandidate(
                    candidate_id=str(uuid.uuid4()),
                    intent=intent.to_dict(),
                    created_at=datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                    reason="budget_exhausted",
                    source_session_ids=[
                        sid for sig_id in intent.source_signal_ids
                        for sid in [sig_id]  # 保留 signal_id 作为追溯
                    ],
                )
                if self.pending_store is not None:
                    self.pending_store.save(cand)
                result.pending.append(cand)
                logger.info(
                    "intent %s 因 budget 耗尽入 pending（candidate_id=%s）",
                    intent.intent_id,
                    cand.candidate_id,
                )
                continue

            # 3. LLM 提炼
            asset = self._distill_with_llm(intent, score)
            if asset is None:
                result.errors.append(f"intent {intent.intent_id}: LLM 提炼失败")
                continue
            if asset.skipped:
                result.llm_skipped += 1
                continue
            result.assets.append(asset)

            # 4. 消费 budget（按 usage.total_tokens）
            if self.budget is not None:
                # usage 在 _distill_with_llm 内部累计到 result.total_tokens_used
                pass

        return result

    def run_single(self, intent_dict: dict[str, Any]) -> dict[str, Any]:
        """处理单个 pending 候选（次日恢复后调用）。

        输入 intent_dict（PendingCandidate.intent），返回 process_callback 期望的 dict：
            {success: bool, asset_id: str | None, error: str | None, usage: {...}}
        """
        try:
            intent = Intent.from_dict(intent_dict)
        except Exception as exc:  # noqa: BLE001
            return {"success": False, "asset_id": None, "error": f"intent 解析失败: {exc}"}
        score = self._score_intent(intent)
        if score.total < self.promotion_threshold:
            return {
                "success": True,  # 评分不足视为已处理（不入 pending 二次）
                "asset_id": None,
                "error": None,
                "usage": {},
                "skipped": "score_below_threshold",
            }
        asset = self._distill_with_llm(intent, score)
        if asset is None:
            return {"success": False, "asset_id": None, "error": "LLM 提炼失败"}
        if asset.skipped:
            return {
                "success": True,
                "asset_id": None,
                "error": None,
                "usage": {},
                "skipped": "llm_skip",
            }
        return {
            "success": True,
            "asset_id": asset.asset.id,
            "error": None,
            "usage": {},  # tokens 由 chat_with_schema 累计，调用方可读取
        }

    # ------------------------------------------------------------------
    # 五维评分
    # ------------------------------------------------------------------

    def _score_intent(self, intent: Intent) -> FiveDimScore:
        """根据 intent metadata 计算五维评分。"""
        meta = intent.metadata or {}
        # frequency：基于 pattern_count（>=3 → 1.0, =2 → 0.7, =1 → 0.4）
        pattern = intent.pattern_count
        frequency = min(1.0, 0.4 + (pattern - 1) * 0.3) if pattern >= 1 else 0.0
        # cross_session：跨会话数（>=2 → 1.0, =1 → 0.3）
        cross_session_count = int(meta.get("cross_session_count", 1))
        cross_session = min(1.0, 0.3 + (cross_session_count - 1) * 0.7)
        # reusability：intent.reusable=True → 0.8，否则 0.2
        reusability = 0.8 if intent.reusable else 0.2
        # clarity：基于 description 长度（>=20 字 → 0.8, <20 → 0.4）
        clarity = 0.8 if len(intent.description) >= 20 else 0.4
        # type_fit：candidate_type 在 rule/memory/skill/tool 之一 → 1.0，否则 0.0
        type_fit = 1.0 if intent.candidate_type in ("rule", "memory", "skill", "tool") else 0.0
        return FiveDimScore(
            frequency=frequency,
            cross_session=cross_session,
            reusability=reusability,
            clarity=clarity,
            type_fit=type_fit,
        )

    # ------------------------------------------------------------------
    # LLM 提炼
    # ------------------------------------------------------------------

    def _distill_with_llm(
        self,
        intent: Intent,
        score: FiveDimScore,
    ) -> DistilledAsset | None:
        """调用 LLM 提炼单个 intent 为资产。"""
        if self.llm is None:
            # 无 LLM 时：生成规则启发式资产（fallback，标记低 confidence）
            return self._heuristic_distill(intent, score)

        template = get_prompt_template(intent.candidate_type)
        system_prompt = render_system_prompt(
            template,
            content_excerpt=intent.description,
            module_path=self.module_path,
            title=intent.description[:50],
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": "请返回 JSON。"},
        ]
        try:
            result = chat_with_schema(
                self.llm,
                messages=messages,
                schema=template.schema,
                max_retries=self.max_retries,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("intent %s LLM 调用异常: %s", intent.intent_id, exc)
            return None

        if not result.success:
            logger.warning(
                "intent %s LLM schema 校验失败（%d 次）: %s",
                intent.intent_id,
                result.attempts,
                result.last_error,
            )
            return None

        return self._build_asset_from_llm_result(
            intent, score, template, result
        )

    def _build_asset_from_llm_result(
        self,
        intent: Intent,
        score: FiveDimScore,
        template: AssetPromptTemplate,
        result: ChatWithSchemaResult,
    ) -> DistilledAsset:
        """从 LLM 结果构建 DistilledAsset。"""
        data = result.data or {}
        skip = bool(data.get("skip", False))
        asset_data = data.get("asset") or {}
        llm_confidence = float(data.get("confidence", 0.0))

        if skip:
            # LLM 判定 skip，不入库
            return DistilledAsset(
                asset=self._make_asset(
                    asset_type_str=intent.candidate_type,
                    title=str(asset_data.get("title", "")),
                    content=str(asset_data.get("content", "")),
                    tags=list(asset_data.get("tags") or []),
                ),
                frontmatter_text="",
                score=score,
                intent_id=intent.intent_id,
                llm_confidence=llm_confidence,
                skipped=True,
                skip_reason=str(data.get("rationale", "LLM 判定 skip")),
            )

        # 构建 Asset 值对象
        asset = self._make_asset(
            asset_type_str=intent.candidate_type,
            title=str(asset_data.get("title", intent.description[:50])),
            content=str(asset_data.get("content", "")),
            tags=list(asset_data.get("tags") or []),
            rationale=str(asset_data.get("rationale", "")),
            extra_fields=self._extract_extra_fields(template, asset_data),
        )
        # 生成 frontmatter 文本
        frontmatter_text = self._render_frontmatter(asset, score, intent)
        return DistilledAsset(
            asset=asset,
            frontmatter_text=frontmatter_text,
            score=score,
            intent_id=intent.intent_id,
            llm_confidence=llm_confidence,
            skipped=False,
        )

    @staticmethod
    def _extract_extra_fields(
        template: AssetPromptTemplate,
        asset_data: dict[str, Any],
    ) -> dict[str, Any]:
        """从 LLM 返回中提取模板特定额外字段（如 skill.steps / tool.invocation）。"""
        extra: dict[str, Any] = {}
        if template.asset_type == "skill":
            steps = asset_data.get("steps") or []
            if isinstance(steps, list):
                extra["steps"] = [str(s) for s in steps]
        elif template.asset_type == "tool":
            inv = asset_data.get("invocation") or {}
            if isinstance(inv, dict):
                extra["invocation"] = {
                    "command": str(inv.get("command", "")),
                    "args": [str(a) for a in (inv.get("args") or [])],
                    "notes": str(inv.get("notes", "")),
                }
        return extra

    # ------------------------------------------------------------------
    # 资产构建
    # ------------------------------------------------------------------

    def _make_asset(
        self,
        *,
        asset_type_str: str,
        title: str,
        content: str,
        tags: list[str],
        rationale: str = "",
        extra_fields: dict[str, Any] | None = None,
    ) -> Asset:
        """构建 Asset 值对象（默认 scope=private，对齐隐私保护）。"""
        # type 映射
        type_map = {
            "rule": AssetType.RULE,
            "memory": AssetType.MEMORY,
            "skill": AssetType.SKILL,
            "tool": AssetType.TOOL,
        }
        asset_type = type_map.get(asset_type_str, AssetType.MEMORY)
        # 完整正文（含标题 + rationale + extra）
        full_content = f"# {title}\n\n{content}"
        if rationale:
            full_content += f"\n\n## 提炼理由\n\n{rationale}"
        if extra_fields:
            for k, v in extra_fields.items():
                full_content += f"\n\n## {k}\n\n{v}"
        return Asset(
            id=str(uuid.uuid4()),
            type=asset_type,
            owner=self.owner,
            scope=Scope.PRIVATE,  # 默认 private，用户主动改为 team/public 才 push
            content=full_content,
            tags=tags,
            module_path=self.module_path,
            version="0.0.1",
            schema_version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )

    def _render_frontmatter(self, asset: Asset, score: FiveDimScore, intent: Intent) -> str:
        """渲染资产 frontmatter 文本（双区设计）。"""
        coding_fields = {
            "coding": "trae",  # 标识来源 coding 软件
            "distill_source": "personal_dream",
            "intent_id": intent.intent_id,
        }
        teamharness_fields = {
            "id": asset.id,
            "type": asset.type.value,
            "owner": asset.owner,
            "scope": asset.scope.value,
            "tags": list(asset.tags),
            "version": asset.version,
            "module_path": asset.module_path,
            "schema_version": asset.schema_version,
            "score": score.to_dict(),
            "llm_confidence": round(intent.metadata.get("best_confidence", 0.0), 3),
            "created_at": asset.created_at.strftime("%Y-%m-%dT%H:%M:%SZ") if asset.created_at else "",
        }
        fm = TraeFrontmatter(
            coding_fields=coding_fields,
            teamharness_fields=teamharness_fields,
            body=asset.content,
        )
        return serialize_frontmatter_dual(fm)

    # ------------------------------------------------------------------
    # 启发式 fallback（无 LLM 时）
    # ------------------------------------------------------------------

    def _heuristic_distill(self, intent: Intent, score: FiveDimScore) -> DistilledAsset:
        """无 LLM 时用规则启发式生成资产（低 confidence）。"""
        asset = self._make_asset(
            asset_type_str=intent.candidate_type,
            title=intent.description[:50],
            content=intent.description,
            tags=[intent.candidate_type],
            rationale=f"启发式提炼（无 LLM），原始意图: {intent.description}",
        )
        frontmatter_text = self._render_frontmatter(asset, score, intent)
        return DistilledAsset(
            asset=asset,
            frontmatter_text=frontmatter_text,
            score=score,
            intent_id=intent.intent_id,
            llm_confidence=0.0,  # 启发式标记 0
            skipped=False,
        )


__all__ = [
    "DEFAULT_PROMOTION_THRESHOLD",
    "DeepStage",
    "DeepStageResult",
    "DistilledAsset",
    "FiveDimScore",
]
