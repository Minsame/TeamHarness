"""SubTask 9.3+9.13: 治理看板测试。

覆盖：
- module_stats 聚合
- split_suggestions 呈现
- orphan_asset_alerts（未登记告警）
- recall_hit_rates（召回命中率）
- adoption_rates（采纳率）
- repo_size_alerts（仓库大小告警）
- counts_mismatch 告警
- archive_overdue 告警
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.governance.dashboard import DashboardService
from server.governance.archive import SemanticArchiveService


class TestDashboard:
    """治理看板聚合。"""

    def test_dashboard_aggregates_all(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """get_dashboard 聚合全部数据。"""
        upsert_helper(
            asset_index,
            id="rule-1",
            content="# test",
            module_path="modules/backend",
        )
        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        assert data.generated_at
        assert isinstance(data.module_stats, list)
        assert isinstance(data.split_suggestions, list)
        assert isinstance(data.orphan_asset_alerts, list)
        assert isinstance(data.recall_hit_rates, list)
        assert isinstance(data.adoption_rates, list)

    def test_orphan_asset_detected(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """module_path 为空的 active 资产 → orphan 告警。"""
        upsert_helper(asset_index, id="orphan-rule", content="# test", module_path="")
        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        orphan_alerts = [
            a for a in data.orphan_asset_alerts if a.category == "orphan_asset"
        ]
        assert len(orphan_alerts) == 1
        assert orphan_alerts[0].asset_id == "orphan-rule"

    def test_no_orphan_when_module_set(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """module_path 非空 → 无 orphan 告警。"""
        upsert_helper(
            asset_index,
            id="rule-1",
            content="# test",
            module_path="modules/backend",
        )
        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        orphan_alerts = [
            a for a in data.orphan_asset_alerts if a.category == "orphan_asset"
        ]
        assert len(orphan_alerts) == 0

    def test_recall_hit_rates(
        self, database, asset_index, archive_root, upsert_helper, recall_log_helper
    ):
        """recall_hit_rates 按模块聚合。"""
        upsert_helper(
            asset_index,
            id="rule-1",
            content="# test",
            module_path="modules/backend",
        )
        # 2 条 list + 1 条 read
        recall_log_helper(
            database, asset_id="rule-1", module_path="modules/backend", query="lint"
        )
        recall_log_helper(
            database, asset_id="rule-1", module_path="modules/backend", query="lint"
        )
        recall_log_helper(
            database, asset_id="rule-1", module_path="modules/backend", query=""
        )

        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        backend_rates = [
            r for r in data.recall_hit_rates if r.module_path == "modules/backend"
        ]
        assert len(backend_rates) == 1
        assert backend_rates[0].recall_count == 3
        assert backend_rates[0].read_count == 1
        assert backend_rates[0].hit_rate == 1.0 / 3.0
        assert backend_rates[0].asset_count == 1

    def test_counts_mismatch_alert(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """declared vs actual 不一致 → counts_mismatch 告警。"""
        from server.infra_db.models import ModuleStats
        from sqlalchemy import insert

        upsert_helper(
            asset_index,
            id="rule-1",
            content="# test",
            module_path="modules/backend",
        )
        # 写入 declared counts 与 actual 不一致
        with database.session() as sess:
            sess.add(
                ModuleStats(
                    module_path="modules/backend",
                    declared_asset_count=5,  # actual 是 1
                    declared_submodule_count=0,
                    actual_asset_count=1,
                    actual_submodule_count=0,
                    counts_consistent=False,
                    last_synced_at=datetime.now(timezone.utc),
                    last_synced_commit="abc",
                )
            )

        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        mismatch_alerts = [
            a for a in data.orphan_asset_alerts if a.category == "counts_mismatch"
        ]
        assert len(mismatch_alerts) == 1
        assert "modules/backend" in mismatch_alerts[0].message

    def test_archive_overdue_alert(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """归档过期 → archive_overdue 告警。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        archive_svc = SemanticArchiveService(
            database, archive_root=archive_root, ttl_days=1
        )
        archive_svc.archive_asset(asset_id="rule-x")
        # 改为已过期
        manifest = archive_svc._read_manifest()
        manifest[0]["hard_delete_at"] = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
        archive_svc._write_manifest(manifest)

        svc = DashboardService(database, archive_root=archive_root)
        data = svc.get_dashboard()
        overdue_alerts = [
            a for a in data.orphan_asset_alerts if a.category == "archive_overdue"
        ]
        assert len(overdue_alerts) == 1
        assert overdue_alerts[0].asset_id == "rule-x"

    def test_get_overview(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """get_overview 返回概览统计。"""
        upsert_helper(asset_index, id="rule-1", module_path="modules/backend")
        upsert_helper(asset_index, id="rule-2", module_path="modules/backend")
        upsert_helper(asset_index, id="orphan", module_path="")

        svc = DashboardService(database, archive_root=archive_root)
        overview = svc.get_overview()
        assert overview["asset_total"] == 3
        assert overview["asset_active"] == 3
        assert overview["module_count"] == 1  # modules/backend
        assert overview["orphan_asset_count"] == 1


# ---------------------------------------------------------------------------
# HTTP 端点 GET /v1/governance/dashboard（SubTask 9.3 + Agent 10 契约回归）
# ---------------------------------------------------------------------------


class TestGovernanceDashboardEndpoint:
    """GET /v1/governance/dashboard HTTP 端点。

    覆盖：
    - happy path：注入服务后返回 200 + 看板聚合数据
    - 服务未注入 → 503
    全局服务变量用 monkeypatch 注入，测试结束自动清理（遵守
    gotchas.md「测试全局状态隔离」规则）。
    """

    def _build_app(self):
        from fastapi import FastAPI

        from server.governance.metrics import governance_router

        app = FastAPI()
        app.include_router(governance_router)
        return app

    def test_get_governance_dashboard_happy_path(
        self, database, asset_index, archive_root, upsert_helper, monkeypatch
    ):
        """注入服务后 GET /v1/governance/dashboard 返回 200 + 看板数据。"""
        from fastapi.testclient import TestClient

        upsert_helper(
            asset_index,
            id="rule-1",
            content="# test",
            module_path="modules/backend",
        )
        svc = DashboardService(database, archive_root=archive_root)
        monkeypatch.setattr(
            "server.governance.metrics._GOVERNANCE_DASHBOARD", svc
        )

        client = TestClient(self._build_app())
        resp = client.get("/v1/governance/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        # 契约字段（overview.md §1 GovernanceService）：
        # {module_stats, split_suggestions, alerts}
        assert "module_stats" in data
        assert "split_suggestions" in data
        # 告警类字段
        assert "orphan_asset_alerts" in data
        assert "recall_hit_rates" in data
        assert "adoption_rates" in data
        assert "repo_size_alerts" in data
        assert data["generated_at"]

    def test_get_governance_dashboard_service_not_configured_returns_503(
        self, monkeypatch
    ):
        """服务未注入时 GET /v1/governance/dashboard 返回 503（非 404/500）。"""
        from fastapi.testclient import TestClient

        monkeypatch.setattr(
            "server.governance.metrics._GOVERNANCE_DASHBOARD", None
        )
        client = TestClient(self._build_app())

        resp = client.get("/v1/governance/dashboard")
        assert resp.status_code == 503
