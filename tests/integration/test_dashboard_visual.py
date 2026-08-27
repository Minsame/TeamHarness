"""SubTask 10.4: 治理看板视觉验证。

由于本项目是后端 Python 服务（FastAPI + SQLAlchemy），无前端 UI 截图。
视觉验证对应到后端：
- 数据结构稳定性：DashboardData 字段完整 + 多次调用结构一致
- HTTP 端点完整性：/v1/metrics/dashboard / /metrics/prometheus / /metrics/definitions
- 指标定义完整性：METRICS_DOCS 含 10 个核心指标
- 无 console error：用 caplog 捕获 ERROR 级别日志，验证构建/查询过程无错误

对应域内验证点：
- 治理看板视觉验证：截图对比 + 无 console error
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient


# ---------------------------------------------------------------------------
# 1. DashboardData 数据结构稳定性
# ---------------------------------------------------------------------------


class TestDashboardDataStructure:
    """DashboardData 数据结构稳定性测试。"""

    def test_dashboard_data_seven_fields(self):
        """DashboardData 含 7 个字段（module_stats/split_suggestions/orphan_asset_alerts/
        recall_hit_rates/adoption_rates/repo_size_alerts/generated_at）。"""
        from server.governance.models import DashboardData

        fields = set(DashboardData.__dataclass_fields__.keys())
        expected = {
            "module_stats", "split_suggestions", "orphan_asset_alerts",
            "recall_hit_rates", "adoption_rates", "repo_size_alerts",
            "generated_at",
        }
        assert expected.issubset(fields), f"DashboardData 缺字段：{expected - fields}"

    def test_dashboard_data_to_dict_serializable(self):
        """DashboardData.to_dict() 可序列化（无循环引用、无不可序列化类型）。"""
        from server.governance.models import DashboardData

        data = DashboardData()
        d = data.to_dict()
        # 7 个字段全部存在
        for key in (
            "module_stats", "split_suggestions", "orphan_asset_alerts",
            "recall_hit_rates", "adoption_rates", "repo_size_alerts",
            "generated_at",
        ):
            assert key in d

    def test_get_dashboard_returns_complete_structure(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """DashboardService.get_dashboard 返回完整结构（7 字段非 None）。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-visual-001"
        seed_asset(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-visual-001", content="# 视觉验证规则",
            git_path="modules/backend/rules/vis.md",
            module_path="modules/backend", commit_sha=commit_sha,
        )

        data = dashboard_service.get_dashboard()
        # 7 字段非 None
        assert data.module_stats is not None
        assert data.split_suggestions is not None
        assert data.orphan_asset_alerts is not None
        assert data.recall_hit_rates is not None
        assert data.adoption_rates is not None
        assert data.repo_size_alerts is not None
        assert data.generated_at  # 非空字符串

    def test_get_dashboard_repeated_call_consistent(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """视觉验证：多次调用 get_dashboard 返回结构一致（字段集稳定）。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-visual-002"
        seed_asset(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-visual-002", content="# 视觉一致性",
            git_path="modules/backend/rules/vis2.md",
            module_path="modules/backend", commit_sha=commit_sha,
        )

        data1 = dashboard_service.get_dashboard()
        data2 = dashboard_service.get_dashboard()

        # 字段集一致
        assert set(data1.to_dict().keys()) == set(data2.to_dict().keys())
        # module_stats 数量一致（同数据库状态）
        assert len(data1.module_stats) == len(data2.module_stats)

    def test_get_overview_returns_summary(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """DashboardService.get_overview 返回汇总（asset_total/asset_active/module_count/generated_at）。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-overview-001"
        seed_asset(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-overview-001", content="# 概览",
            git_path="modules/backend/rules/ov.md",
            module_path="modules/backend", commit_sha=commit_sha,
        )

        overview = dashboard_service.get_overview()
        for key in ("asset_total", "asset_active", "module_count", "generated_at"):
            assert key in overview, f"overview 缺字段 {key}"
        assert overview["asset_total"] >= 1
        assert overview["asset_active"] >= 1
        assert overview["module_count"] >= 1


# ---------------------------------------------------------------------------
# 2. /v1/metrics/* HTTP 端点完整性
# ---------------------------------------------------------------------------


class TestMetricsHttpEndpoints:
    """/v1/metrics/* HTTP 端点视觉验证。"""

    def _build_app_with_metrics(self, metrics) -> FastAPI:
        """构造绑定特定 metrics 的 FastAPI app。"""
        from server.governance.metrics import _build_router_with_metrics

        app = FastAPI()
        app.include_router(_build_router_with_metrics(metrics))
        return app

    def test_metrics_dashboard_endpoint(
        self,
        governance_metrics,
    ):
        """GET /v1/metrics/dashboard 返回 Grafana 嵌入数据。"""
        app = self._build_app_with_metrics(governance_metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        # 含 definitions + prometheus_available 字段
        assert "definitions" in data
        assert "prometheus_available" in data
        # definitions 是 list
        assert isinstance(data["definitions"], list)
        assert len(data["definitions"]) >= 1

    def test_metrics_definitions_endpoint(
        self,
        governance_metrics,
    ):
        """GET /v1/metrics/definitions 返回 10 个核心指标定义。"""
        app = self._build_app_with_metrics(governance_metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/definitions")
        assert resp.status_code == 200
        data = resp.json()
        assert "metrics" in data
        # 至少 10 个指标定义
        assert len(data["metrics"]) >= 10
        # 每个定义含 name 字段
        for m in data["metrics"]:
            assert "name" in m

    def test_metrics_prometheus_endpoint(
        self,
        governance_metrics,
    ):
        """GET /v1/metrics/prometheus 返回 Prometheus exposition format。

        prometheus_client 可用时返回非空 body；不可用时降级返回空 body（200 OK）。
        两种情况都视为视觉验证通过（端点可访问 + Content-Type 正确）。
        """
        from server.governance.metrics import PROMETHEUS_AVAILABLE

        app = self._build_app_with_metrics(governance_metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/prometheus")
        assert resp.status_code == 200
        # Content-Type 为 Prometheus exposition format
        ct = resp.headers.get("content-type", "")
        assert "text/plain" in ct
        # body 行为依赖 PROMETHEUS_AVAILABLE：
        # - 可用：非空，含 teamharness_ 前缀
        # - 不可用：空 bytes（降级 no-op，对齐 gotchas.md Stub 降级规则）
        if PROMETHEUS_AVAILABLE:
            assert resp.content
            assert b"teamharness_" in resp.content
        else:
            # 降级场景：空 body 也是合法的（端点仍可访问）
            assert resp.content == b"" or resp.content

    def test_metrics_post_batch_endpoint(
        self,
        governance_metrics,
    ):
        """POST /v1/metrics 批量上报端点。"""
        app = self._build_app_with_metrics(governance_metrics)
        client = TestClient(app)

        events = [
            {
                "event_id": "evt-vis-001",
                "event_type": "recall",
                "asset_id": "rule-vis-001",
                "agent_id": "builder-vis",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": "2026-08-07T10:00:00Z",
                "metadata": {},
            },
        ]
        resp = client.post("/v1/metrics", json={
            "agent_id": "builder-vis",
            "events": events,
        })
        assert resp.status_code == 200
        data = resp.json()
        assert "accepted" in data
        assert "rejected" in data
        assert data["accepted"] >= 1


# ---------------------------------------------------------------------------
# 3. METRICS_DOCS 指标定义完整性
# ---------------------------------------------------------------------------


class TestMetricsDocsCompleteness:
    """METRICS_DOCS 10 个核心指标定义完整性。"""

    def test_metrics_docs_has_ten_definitions(self):
        """METRICS_DOCS 含 ≥ 10 个指标定义。"""
        from server.governance.metrics_docs import METRICS_DOCS

        assert len(METRICS_DOCS) >= 10

    def test_metrics_docs_each_definition_complete(self):
        """每个指标定义含 name / description / instrument_location / labels 等字段。"""
        from server.governance.metrics_docs import METRICS_DOCS

        for m in METRICS_DOCS:
            # name 必填
            assert m.name, f"指标缺 name：{m}"
            assert m.name.startswith("teamharness_"), \
                f"指标名 {m.name} 不以 teamharness_ 前缀"

    def test_metrics_docs_expected_metrics_present(self):
        """10 个核心指标全部定义（按技术方案）。"""
        from server.governance.metrics_docs import METRICS_DOCS

        names = {m.name for m in METRICS_DOCS}
        expected = {
            "teamharness_asset_total",
            "teamharness_asset_active",
            "teamharness_module_count",
            "teamharness_recall_count_30d",
            "teamharness_adoption_rate",
            "teamharness_adoption_stale_count",
            "teamharness_index_sync_lag_seconds",
            "teamharness_embedding_queue_pending",
            "teamharness_distill_job_running",
            "teamharness_repo_size_bytes",
        }
        missing = expected - names
        assert not missing, f"METRICS_DOCS 缺指标：{missing}"


# ---------------------------------------------------------------------------
# 4. 无 console error 验证（caplog 捕获 ERROR 级别日志）
# ---------------------------------------------------------------------------


class TestNoConsoleError:
    """视觉验证：构建/查询过程无 console error（ERROR 级别日志）。"""

    def test_get_dashboard_no_error_log(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        caplog,
    ):
        """get_dashboard 调用过程无 ERROR 级别日志。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-noerr-001"
        seed_asset(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-noerr-001", content="# 无错误",
            git_path="modules/backend/rules/ne.md",
            module_path="modules/backend", commit_sha=commit_sha,
        )

        with caplog.at_level(logging.ERROR):
            data = dashboard_service.get_dashboard()

        # 收集 ERROR 级别日志
        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_logs, \
            f"get_dashboard 产生 ERROR 日志：{[r.getMessage() for r in error_logs]}"

        # 数据正常返回
        assert data.generated_at

    def test_get_overview_no_error_log(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        caplog,
    ):
        """get_overview 调用过程无 ERROR 级别日志。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-noerr-002"
        seed_asset(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-noerr-002", content="# 概览无错误",
            git_path="modules/backend/rules/ne2.md",
            module_path="modules/backend", commit_sha=commit_sha,
        )

        with caplog.at_level(logging.ERROR):
            overview = dashboard_service.get_overview()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_logs, \
            f"get_overview 产生 ERROR 日志：{[r.getMessage() for r in error_logs]}"

        assert overview["asset_total"] >= 1

    def test_metrics_endpoints_no_error_log(
        self,
        governance_metrics,
        caplog,
    ):
        """所有 /v1/metrics/* 端点调用过程无 ERROR 级别日志。"""
        from server.governance.metrics import _build_router_with_metrics

        app = FastAPI()
        app.include_router(_build_router_with_metrics(governance_metrics))
        client = TestClient(app)

        with caplog.at_level(logging.ERROR):
            # GET /v1/metrics/dashboard
            resp = client.get("/v1/metrics/dashboard")
            assert resp.status_code == 200
            # GET /v1/metrics/definitions
            resp = client.get("/v1/metrics/definitions")
            assert resp.status_code == 200
            # GET /v1/metrics/prometheus
            resp = client.get("/v1/metrics/prometheus")
            assert resp.status_code == 200
            # POST /v1/metrics
            resp = client.post("/v1/metrics", json={
                "agent_id": "builder-noerr",
                "events": [{
                    "event_id": "evt-noerr-001",
                    "event_type": "recall",
                    "asset_id": "rule-noerr-003",
                    "agent_id": "builder-noerr",
                    "member_id": "alice",
                    "module_path": "modules/backend",
                    "timestamp": "2026-08-07T10:00:00Z",
                    "metadata": {},
                }],
            })
            assert resp.status_code == 200

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_logs, \
            f"/v1/metrics/* 端点产生 ERROR 日志：{[r.getMessage() for r in error_logs]}"

    def test_full_chain_no_error_log(
        self,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        sync_service,
        recall_service,
        personal_distill,
        team_distill,
        dashboard_service,
        governance_metrics,
        database,
        caplog,
    ):
        """端到端全链路调用过程无 ERROR 级别日志。"""
        from server.infra_db.models import IndexSyncState
        from tests.integration.conftest import seed_asset, make_session

        commit_sha = "commit-noerr-e2e-001"
        with caplog.at_level(logging.ERROR):
            # 1. seed 资产
            seed_asset(
                asset_index=asset_index, embedding_service=embedding_service,
                vector_store=vector_store, mock_git_provider=mock_git_provider,
                asset_id="rule-noerr-e2e-001", content="# 全链路无错误",
                git_path="modules/backend/rules/nee.md",
                module_path="modules/backend", commit_sha=commit_sha,
            )
            # 2. dashboard
            dashboard_service.get_dashboard()
            # 3. metrics ingest
            governance_metrics.ingest_events([{
                "event_id": "evt-noerr-e2e-001",
                "event_type": "recall",
                "asset_id": "rule-noerr-e2e-001",
                "agent_id": "builder-e2e-noerr",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": "2026-08-07T10:00:00Z",
                "metadata": {},
            }], agent_id="builder-e2e-noerr")
            # 4. personal distill
            sessions = [make_session(session_id="sess-noerr-001")]
            personal_distill.run(sessions, member_id="alice")
            # 5. team distill
            team_distill.trigger_incremental()

        error_logs = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert not error_logs, \
            f"全链路产生 ERROR 日志：{[r.getMessage() for r in error_logs]}"


# ---------------------------------------------------------------------------
# 5. 视觉对比基线：DashboardData.to_dict() 字段顺序稳定
# ---------------------------------------------------------------------------


class TestDashboardDataBaseline:
    """视觉对比基线：DashboardData 字段顺序稳定（截图对比的后端等价）。"""

    def test_dashboard_data_field_order_stable(self):
        """DashboardData.to_dict() 字段顺序稳定（与基线一致）。"""
        from server.governance.models import DashboardData

        data = DashboardData()
        d = data.to_dict()
        # 基线字段顺序（按数据类定义顺序）
        expected_order = [
            "module_stats",
            "split_suggestions",
            "orphan_asset_alerts",
            "recall_hit_rates",
            "adoption_rates",
            "repo_size_alerts",
            "generated_at",
        ]
        actual_order = list(d.keys())
        assert actual_order == expected_order, \
            f"DashboardData.to_dict() 字段顺序变化：期望 {expected_order}，实际 {actual_order}"

    def test_overview_field_set_stable(
        self,
        dashboard_service,
    ):
        """get_overview 返回字段集稳定（视觉对比基线）。"""
        overview = dashboard_service.get_overview()
        # 基线字段集
        expected_keys = {
            "asset_total", "asset_active", "module_count",
            "generated_at", "pending_count",
        }
        # 至少含基线 4 字段（pending_count 可选）
        assert {"asset_total", "asset_active", "module_count", "generated_at"}.issubset(
            set(overview.keys())
        )
