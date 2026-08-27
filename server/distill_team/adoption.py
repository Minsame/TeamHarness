"""采纳率降级（SubTask 8.14）。

设计：
- 查询近 30 天 recall_log 表，统计 cluster 内资产被召回总次数
- 若 recall_count < threshold（默认 1）→ 自动降级
- 降级动作：DistilledPrompt.confidence = "low"
- 用于在 RecallService 召回时降权显示，提示用户该 Prompt 缺乏实战验证

数据源：Agent 4 已将召回日志写入 recall_log 表（含 trace_id/asset_id/agent_id/...）。
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from server.infra_db.db import Database
from server.infra_db.models import RecallLog
from server.distill_team.models import AdoptionStatus, DistilledPrompt

logger = logging.getLogger(__name__)


class AdoptionRateChecker:
    """采纳率检查器。

    用法：
        checker = AdoptionRateChecker(database)
        status = checker.check(asset_ids, days=30, threshold=1)
        if status.degraded:
            prompt = checker.apply_degradation(prompt, status)
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def check(
        self,
        asset_ids: list[str],
        *,
        days: int = 30,
        threshold: int = 1,
    ) -> AdoptionStatus:
        """检查近 N 天召回次数是否 < threshold。

        - asset_ids: cluster 内资产 id 清单
        - 返回 AdoptionStatus，degraded=True 表示需降级
        """
        if not asset_ids:
            return AdoptionStatus(
                recall_count_30d=0,
                threshold=threshold,
                degraded=True,
                reason="空资产清单，自动降级",
            )
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._db.session() as sess:
            stmt = (
                select(func.count(RecallLog.id))
                .where(RecallLog.asset_id.in_(asset_ids))
                .where(RecallLog.recalled_at >= cutoff)
            )
            total = int(sess.scalar(stmt) or 0)

        degraded = total < threshold
        reason = ""
        if degraded:
            reason = (
                f"近 {days} 天召回 {total} < {threshold}，"
                f"缺乏实战验证，降级为 confidence=low"
            )
        return AdoptionStatus(
            recall_count_30d=total,
            threshold=threshold,
            degraded=degraded,
            reason=reason,
        )

    def apply_degradation(
        self, prompt: DistilledPrompt, status: AdoptionStatus
    ) -> DistilledPrompt:
        """对 Prompt 应用降级（confidence=low）。"""
        if status.degraded:
            prompt.confidence = "low"
            logger.info(
                "采纳率降级 prompt_id=%s recall=%d threshold=%d",
                prompt.prompt_id,
                status.recall_count_30d,
                status.threshold,
            )
        return prompt


__all__ = ["AdoptionRateChecker"]
