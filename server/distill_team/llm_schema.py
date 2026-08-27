"""LLM 强制 JSON schema + SKIP 审查区写 DREAMS.md（SubTask 8.9）。

职责：
1. 调用 LLM（run_distill_llm）执行 6 步推理链
2. SKIP 候选写入 DREAMS.md SKIP 审查区（用 infra_git/dreams.py 的 append_entry）
3. 每周人工抽查 10% SKIP 候选（标记 needs_human_review）
4. 生成 DistilledPrompt 对象（含 confidence / cold_start / counter_example_pass）

SKIP 审查区：每月 DREAMS 文件中 stage="skip" 的条目，供人工抽查。
"""

from __future__ import annotations

import logging
import random
import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.infra_git.dreams import DreamEntry, append_entry
from server.distill_team.models import (
    DistilledPrompt,
    GateResult,
    SixDimScore,
)
from server.distill_team.prompts import (
    DistillLLMResult,
    LLMChatProtocol,
    run_distill_llm,
    build_assets_excerpt,
    build_cluster_info,
    build_score_info,
)
from server.distill_team.rem import REMCluster

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# SKIP 审查抽样
# ---------------------------------------------------------------------------


def should_human_review(*, skip_count_this_week: int, seed: int | None = None) -> bool:
    """每周人工抽查 10% SKIP 候选。

    - skip_count_this_week: 本周已 SKIP 数（用于确定性抽样）
    - seed: 随机种子（测试可固定，与 skip_count_this_week 组合确保每次调用不同）
    返回是否需要人工审查。
    """
    base = seed if seed is not None else 0
    rng = random.Random(base + skip_count_this_week)
    return rng.random() < 0.1


# ---------------------------------------------------------------------------
# DistillPromptRunner
# ---------------------------------------------------------------------------


class DistillPromptRunner:
    """二级提炼 Prompt 生成器。

    用法：
        runner = DistillPromptRunner(repo_root=Path("./repo"), llm=llm_provider)
        prompt = runner.run(rem_cluster, score, gate)
        if prompt.in_skip_review:
            # 写入 DREAMS.md SKIP 审查区（已自动写入）
            ...
    """

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        llm: LLMChatProtocol | None = None,
        skip_review_sample_rate: float = 0.1,
    ) -> None:
        self._repo_root = repo_root
        self._llm = llm
        self._skip_sample_rate = skip_review_sample_rate
        self._skip_count = 0

    def run(
        self,
        rem_cluster: REMCluster,
        score: SixDimScore,
        gate: GateResult,
        *,
        assets_content: list[dict] | None = None,
    ) -> DistilledPrompt:
        """执行二级提炼：LLM 6 步推理 → 生成 Prompt → SKIP 写审查区。"""
        cluster = rem_cluster.cluster
        cluster_info = build_cluster_info(
            {
                "cluster_id": cluster.cluster_id,
                "category": cluster.category,
                "size": cluster.size,
                "owners": cluster.owners,
                "module_paths": cluster.module_paths,
                "is_convention": cluster.is_convention,
            }
        )
        excerpt = build_assets_excerpt(assets_content or [])
        score_info = build_score_info(score.to_dict())

        # 调用 LLM（或启发式 fallback）
        llm_result = run_distill_llm(
            self._llm,
            cluster_info=cluster_info,
            assets_excerpt=excerpt,
            score_info=score_info,
        )

        # 生成 prompt_id
        prompt_id = f"prompt-{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)

        # 判断是否进 SKIP 审查区
        in_skip_review = False
        skip_reason = ""
        if llm_result.is_skip:
            in_skip_review = self._should_sample_skip()
            skip_reason = llm_result.skip_reason
            self._skip_count += 1
            # 写入 DREAMS.md SKIP 审查区
            self._write_skip_entry(
                rem_cluster=rem_cluster,
                llm_result=llm_result,
                prompt_id=prompt_id,
            )
        elif not llm_result.counter_example_pass:
            # PROMOTE 但反例检验失败 → 强制 SKIP（一致性兜底）
            llm_result.decision = "SKIP"
            llm_result.skip_reason = "counter_example_failed"
            in_skip_review = self._should_sample_skip()
            self._skip_count += 1
            self._write_skip_entry(
                rem_cluster=rem_cluster,
                llm_result=llm_result,
                prompt_id=prompt_id,
            )

        # 构造 DistilledPrompt
        confidence = "high"
        if gate.cold_start:
            confidence = "low"
        elif llm_result.used_fallback:
            confidence = "medium"
        elif gate.score.total < 0.8:
            confidence = "medium"

        prompt = DistilledPrompt(
            prompt_id=prompt_id,
            title=llm_result.draft_title or rem_cluster.common_topic or "未命名",
            content=llm_result.draft_content,
            category=cluster.category,
            cluster_id=cluster.cluster_id,
            score=score,
            gate=gate,
            confidence=confidence,
            cold_start=gate.cold_start,
            counter_example_pass=llm_result.counter_example_pass,
            in_skip_review=in_skip_review,
            skip_reason=skip_reason,
            source_asset_ids=cluster.asset_ids,
            created_at=now,
        )
        return prompt

    # ------------------------------------------------------------------
    # 内部：SKIP 审查区写入
    # ------------------------------------------------------------------

    def _should_sample_skip(self) -> bool:
        """按抽样率决定是否标记人工审查。"""
        if self._skip_sample_rate >= 1.0:
            return True
        if self._skip_sample_rate <= 0.0:
            return False
        # 简化：每 N 个 SKIP 抽 1 个（N = 1 / rate）
        import random
        return random.random() < self._skip_sample_rate

    def _write_skip_entry(
        self,
        *,
        rem_cluster: REMCluster,
        llm_result: DistillLLMResult,
        prompt_id: str,
    ) -> None:
        """写入 DREAMS.md SKIP 审查区。

        若 repo_root 未配置则跳过（测试用）。
        """
        if self._repo_root is None:
            logger.info(
                "repo_root 未配置，SKIP 条目未写入 DREAMS.md prompt_id=%s", prompt_id
            )
            return
        try:
            entry = DreamEntry(
                timestamp=datetime.now(timezone.utc).isoformat(),
                stage="skip",
                title=f"SKIP: {llm_result.skip_reason}",
                body=(
                    f"prompt_id: {prompt_id}\n"
                    f"cluster_id: {rem_cluster.cluster.cluster_id}\n"
                    f"category: {rem_cluster.cluster.category or '(无)'}\n"
                    f"owners: {', '.join(rem_cluster.cross_owners)}\n"
                    f"cross_member_count: {rem_cluster.cross_member_count}\n"
                    f"counter_example_pass: {llm_result.counter_example_pass}\n"
                    f"overfit: {llm_result.overfit}\n"
                    f"overfit_reason: {llm_result.overfit_reason}\n"
                    f"step1_topic: {llm_result.step1_topic}\n"
                    f"step2_pattern: {llm_result.step2_pattern}\n"
                    f"skip_reason: {llm_result.skip_reason}\n"
                ),
                metadata={
                    "prompt_id": prompt_id,
                    "cluster_id": rem_cluster.cluster.cluster_id,
                    "skip_reason": llm_result.skip_reason,
                    "needs_human_review": str(self._should_sample_skip()),
                },
            )
            append_entry(self._repo_root, entry)
            logger.info(
                "SKIP 条目写入 DREAMS.md prompt_id=%s cluster=%s",
                prompt_id,
                rem_cluster.cluster.cluster_id,
            )
        except Exception as exc:
            logger.warning("写入 DREAMS.md SKIP 审查区失败: %s", exc)


__all__ = ["DistillPromptRunner", "should_human_review"]
