"""AdoptionMetricsService — 采纳率服务端可采 + stale 标记（SubTask 9.8 + 9.9）。

对应技术方案 3.3.6 + 关键风险提醒（🔴 红线）：
- 采纳率必须服务端可采（基于 recall_log 召回 + read 次数）
- 客户端上报仅作辅助信号
- adoption_rate 连续 7 天无客户端上报 → 标记 stale=True

设计要点（红线遵守）：
- recall_count_30d：recall_log 表近 30 天该资产被召回次数（服务端可采，主信号）
- read_count_30d：recall_log 表近 30 天该资产被 read 次数（query='' 区分 read 事件）
- adoption_rate = read_count / recall_count（被召回后实际被读取的比例）
- client_events_30d：adoption_event 表近 30 天客户端上报事件数（辅助信号）
- last_client_event_at：adoption_event 表最后上报时间
- stale：last_client_event_at 距今 > 7 天 → True（客户端未上报，采纳率可能过时）

recall_log 区分 list/read 事件：
- recall_list 写入：query 字段非空（用户查询词）
- recall_read 写入：query 字段为空（直接读取资产）
故 read_count = COUNT(recall_log WHERE query='' AND asset_id=?)
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from server.governance.models import AdoptionMetric
from server.infra_db.db import Database
from server.infra_db.models import AdoptionEvent, AssetIndex as AssetIndexRow, RecallLog

logger = logging.getLogger(__name__)


# 采纳率统计窗口（近 30 天）
ADOPTION_WINDOW_DAYS = 30
# stale 阈值：连续 7 天无客户端上报 → stale=True
STALE_THRESHOLD_DAYS = 7


class AdoptionMetricsService:
    """采纳率服务端采集服务。

    用法：
        svc = AdoptionMetricsService(database)
        # 单资产采纳率
        metric = svc.get_metric("rule-x")
        # 批量采集
        metrics = svc.collect_metrics()
        # 标记 stale（cron 调用）
        stale_count = svc.mark_stale()
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def get_metric(self, asset_id: str) -> AdoptionMetric:
        """采集单资产的采纳率指标（服务端可采）。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=ADOPTION_WINDOW_DAYS)
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset_id)
            module_path = row.module_path if row else ""

            recall_count = self._count_recall(sess, asset_id, cutoff)
            read_count = self._count_read(sess, asset_id, cutoff)
            client_events = self._count_client_events(sess, asset_id, cutoff)
            last_client_at = self._last_client_event_at(sess, asset_id)

        adoption_rate = (read_count / recall_count) if recall_count > 0 else 0.0
        stale = self._is_stale(last_client_at)

        return AdoptionMetric(
            asset_id=asset_id,
            module_path=module_path,
            recall_count_30d=recall_count,
            read_count_30d=read_count,
            adoption_rate=adoption_rate,
            client_events_30d=client_events,
            last_client_event_at=last_client_at,
            stale=stale,
        )

    def collect_metrics(
        self, asset_ids: list[str] | None = None
    ) -> list[AdoptionMetric]:
        """批量采集采纳率指标。

        - asset_ids=None → 全部 active 资产
        - 否则按指定 asset_ids 采集
        """
        with self._db.session() as sess:
            if asset_ids is None:
                stmt = (
                    select(AssetIndexRow.id)
                    .where(AssetIndexRow.status == "active")
                )
                asset_ids = list(sess.scalars(stmt))
        return [self.get_metric(aid) for aid in asset_ids]

    def mark_stale(self) -> int:
        """标记 stale：连续 7 天无客户端上报的资产 → stale=True。

        遍历全部 active 资产，检查 adoption_event 表最后上报时间。
        返回标记为 stale 的资产数。
        """
        now = datetime.now(timezone.utc)
        stale_cutoff = now - timedelta(days=STALE_THRESHOLD_DAYS)
        with self._db.session() as sess:
            # 全部有客户端上报的资产 + 最后上报时间
            stmt = (
                select(
                    AdoptionEvent.asset_id,
                    func.max(AdoptionEvent.occurred_at).label("last_at"),
                )
                .group_by(AdoptionEvent.asset_id)
            )
            last_event_map: dict[str, datetime] = {}
            for asset_id, last_at in sess.execute(stmt):
                if last_at is not None:
                    last_event_map[asset_id] = last_at

            # 全部 active 资产
            active_ids = list(
                sess.scalars(
                    select(AssetIndexRow.id).where(
                        AssetIndexRow.status == "active"
                    )
                )
            )

        stale_count = 0
        for asset_id in active_ids:
            last_at = last_event_map.get(asset_id)
            if self._is_stale(last_at):
                stale_count += 1
        return stale_count

    # ------------------------------------------------------------------
    # 内部：recall_log 聚合（服务端可采主信号）
    # ------------------------------------------------------------------

    def _count_recall(
        self, sess, asset_id: str, cutoff: datetime
    ) -> int:
        """统计 recall_log 近 N 天召回次数（list + read 合计）。"""
        stmt = (
            select(func.count(RecallLog.id))
            .where(RecallLog.asset_id == asset_id)
            .where(RecallLog.recalled_at >= cutoff)
        )
        return int(sess.scalar(stmt) or 0)

    def _count_read(
        self, sess, asset_id: str, cutoff: datetime
    ) -> int:
        """统计 recall_log 近 N 天 read 次数（query='' 区分 read 事件）。

        recall_read 写 recall_log 时 query=''；
        recall_list 写 recall_log 时 query=用户查询词。
        """
        stmt = (
            select(func.count(RecallLog.id))
            .where(RecallLog.asset_id == asset_id)
            .where(RecallLog.recalled_at >= cutoff)
            .where(RecallLog.query == "")
        )
        return int(sess.scalar(stmt) or 0)

    def _count_client_events(
        self, sess, asset_id: str, cutoff: datetime
    ) -> int:
        """统计 adoption_event 近 N 天客户端上报事件数（辅助信号）。"""
        stmt = (
            select(func.count(AdoptionEvent.id))
            .where(AdoptionEvent.asset_id == asset_id)
            .where(AdoptionEvent.occurred_at >= cutoff)
        )
        return int(sess.scalar(stmt) or 0)

    def _last_client_event_at(
        self, sess, asset_id: str
    ) -> datetime | None:
        """查询客户端最后上报时间。"""
        stmt = (
            select(func.max(AdoptionEvent.occurred_at))
            .where(AdoptionEvent.asset_id == asset_id)
        )
        return sess.scalar(stmt)

    # ------------------------------------------------------------------
    # 内部：stale 判定
    # ------------------------------------------------------------------

    def _is_stale(self, last_client_at: datetime | None) -> bool:
        """判定 stale：连续 7 天无客户端上报 → True。

        - last_client_at 为 None（从未上报）→ True
        - last_client_at 距今 > 7 天 → True
        - 否则 False
        """
        if last_client_at is None:
            return True
        now = datetime.now(timezone.utc)
        # 容错：确保带时区
        if last_client_at.tzinfo is None:
            last_client_at = last_client_at.replace(tzinfo=timezone.utc)
        delta = (now - last_client_at).total_seconds()
        return delta > STALE_THRESHOLD_DAYS * 86400


__all__ = [
    "ADOPTION_WINDOW_DAYS",
    "AdoptionMetricsService",
    "STALE_THRESHOLD_DAYS",
]
