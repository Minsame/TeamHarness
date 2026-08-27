"""SubTask 9.8+9.9+9.13: 采纳率服务端采集 + stale 标记测试。

覆盖：
- recall_count_30d 从 recall_log 聚合（服务端可采主信号）
- read_count_30d 区分 query='' 的 read 事件
- adoption_rate = read_count / recall_count
- client_events_30d 从 adoption_event 聚合（辅助信号）
- stale 标记：连续 7 天无客户端上报 → True
- 从未上报 → stale=True
- 7 天内有上报 → stale=False
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.governance.adoption import (
    ADOPTION_WINDOW_DAYS,
    STALE_THRESHOLD_DAYS,
    AdoptionMetricsService,
)


class TestGetMetric:
    """单资产采纳率采集。"""

    def test_recall_count_from_recall_log(
        self, database, asset_index, upsert_helper, recall_log_helper
    ):
        """recall_count_30d 从 recall_log 聚合。"""
        upsert_helper(asset_index, id="rule-x", module_path="modules/backend")
        # 写入 3 条 recall_log（近 30 天内）
        recall_log_helper(
            database, asset_id="rule-x", module_path="modules/backend", query="lint"
        )
        recall_log_helper(
            database, asset_id="rule-x", module_path="modules/backend", query="lint"
        )
        recall_log_helper(
            database, asset_id="rule-x", module_path="modules/backend", query="lint"
        )

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.recall_count_30d == 3
        assert metric.module_path == "modules/backend"

    def test_read_count_distinguished_by_empty_query(
        self, database, asset_index, upsert_helper, recall_log_helper
    ):
        """read_count_30d = query='' 的 recall_log 条数。"""
        upsert_helper(asset_index, id="rule-x", module_path="modules/backend")
        # 2 条 list 事件（query 非空）
        recall_log_helper(database, asset_id="rule-x", query="lint")
        recall_log_helper(database, asset_id="rule-x", query="lint")
        # 1 条 read 事件（query 为空）
        recall_log_helper(database, asset_id="rule-x", query="")

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.recall_count_30d == 3
        assert metric.read_count_30d == 1
        assert metric.adoption_rate == 1.0 / 3.0

    def test_adoption_rate_zero_when_no_recall(
        self, database, asset_index, upsert_helper
    ):
        """无召回 → adoption_rate=0（不除零）。"""
        upsert_helper(asset_index, id="rule-x")
        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.recall_count_30d == 0
        assert metric.read_count_30d == 0
        assert metric.adoption_rate == 0.0

    def test_client_events_from_adoption_event(
        self, database, asset_index, upsert_helper, adoption_event_helper
    ):
        """client_events_30d 从 adoption_event 聚合。"""
        upsert_helper(asset_index, id="rule-x")
        adoption_event_helper(database, asset_id="rule-x", event_type="recall")
        adoption_event_helper(database, asset_id="rule-x", event_type="adopt")
        adoption_event_helper(database, asset_id="rule-x", event_type="view")

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.client_events_30d == 3

    def test_old_recall_excluded(
        self, database, asset_index, upsert_helper, recall_log_helper
    ):
        """超过 30 天的 recall_log 不计入。"""
        upsert_helper(asset_index, id="rule-x")
        # 31 天前
        recall_log_helper(database, asset_id="rule-x", days_ago=31)
        # 5 天前
        recall_log_helper(database, asset_id="rule-x", days_ago=5)

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.recall_count_30d == 1  # 仅 5 天前


class TestStaleMarking:
    """stale 标记（连续 7 天无客户端上报）。"""

    def test_stale_when_no_client_events(
        self, database, asset_index, upsert_helper
    ):
        """从未上报 → stale=True。"""
        upsert_helper(asset_index, id="rule-x")
        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.stale is True
        assert metric.last_client_event_at is None

    def test_stale_when_7_days_no_report(
        self, database, asset_index, upsert_helper, adoption_event_helper
    ):
        """7 天前有上报，之后无 → stale=True。"""
        upsert_helper(asset_index, id="rule-x")
        adoption_event_helper(database, asset_id="rule-x", days_ago=8)

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.stale is True
        assert metric.last_client_event_at is not None

    def test_not_stale_when_recent_report(
        self, database, asset_index, upsert_helper, adoption_event_helper
    ):
        """3 天前有上报 → stale=False。"""
        upsert_helper(asset_index, id="rule-x")
        adoption_event_helper(database, asset_id="rule-x", days_ago=3)

        svc = AdoptionMetricsService(database)
        metric = svc.get_metric("rule-x")
        assert metric.stale is False

    def test_mark_stale_returns_count(
        self, database, asset_index, upsert_helper, adoption_event_helper
    ):
        """mark_stale 返回 stale 资产数。"""
        upsert_helper(asset_index, id="rule-a")
        upsert_helper(asset_index, id="rule-b")
        upsert_helper(asset_index, id="rule-c")
        # rule-a 3 天前有上报 → 不 stale
        adoption_event_helper(database, asset_id="rule-a", days_ago=3)
        # rule-b 8 天前有上报 → stale
        adoption_event_helper(database, asset_id="rule-b", days_ago=8)
        # rule-c 从未上报 → stale

        svc = AdoptionMetricsService(database)
        stale_count = svc.mark_stale()
        assert stale_count == 2  # rule-b + rule-c


class TestCollectMetrics:
    """批量采集。"""

    def test_collect_all_active(
        self, database, asset_index, upsert_helper
    ):
        """collect_metrics(None) → 全部 active 资产。"""
        upsert_helper(asset_index, id="rule-a")
        upsert_helper(asset_index, id="rule-b")
        upsert_helper(asset_index, id="rule-c")

        svc = AdoptionMetricsService(database)
        metrics = svc.collect_metrics()
        asset_ids = {m.asset_id for m in metrics}
        assert asset_ids == {"rule-a", "rule-b", "rule-c"}

    def test_collect_specific_ids(
        self, database, asset_index, upsert_helper
    ):
        """collect_metrics(ids) → 仅指定资产。"""
        upsert_helper(asset_index, id="rule-a")
        upsert_helper(asset_index, id="rule-b")

        svc = AdoptionMetricsService(database)
        metrics = svc.collect_metrics(["rule-a"])
        assert len(metrics) == 1
        assert metrics[0].asset_id == "rule-a"

    def test_collect_excludes_deleted(
        self, database, asset_index, upsert_helper
    ):
        """归档/删除的资产不纳入采集。"""
        from server.infra_db.models import AssetIndex as AssetIndexRow
        from sqlalchemy import update

        upsert_helper(asset_index, id="rule-active")
        upsert_helper(asset_index, id="rule-deleted")
        with database.session() as sess:
            sess.execute(
                update(AssetIndexRow)
                .where(AssetIndexRow.id == "rule-deleted")
                .values(status="deleted")
            )

        svc = AdoptionMetricsService(database)
        metrics = svc.collect_metrics()
        asset_ids = {m.asset_id for m in metrics}
        assert "rule-active" in asset_ids
        assert "rule-deleted" not in asset_ids
