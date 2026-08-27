"""SubTask 9.5+9.6+9.7+9.13: 指标采集 + /v1/metrics 批量上报 + 指标文档测试。

覆盖：
- ingest_events 写入 adoption_event 表
- event_id 幂等去重
- POST /v1/metrics 端点
- GET /v1/metrics/definitions 返回 10 个指标定义
- GET /v1/metrics/prometheus scrape 端点
- METRICS_DOCS 含 10 个核心指标
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from server.governance.metrics import (
    GovernanceMetrics,
    MetricsBatchRequest,
    MetricsEventSchema,
    PROMETHEUS_AVAILABLE,
    build_router,
    configure_governance,
    governance_router,
)
from server.governance.metrics_docs import METRICS_DOCS, get_metric_doc, to_dict_list


# ---------------------------------------------------------------------------
# METRICS_DOCS 指标文档化（SubTask 9.7）
# ---------------------------------------------------------------------------


class TestMetricsDocs:
    """10 个核心指标定义。"""

    def test_metrics_docs_has_10_definitions(self):
        """METRICS_DOCS 含 10 个核心指标。"""
        assert len(METRICS_DOCS) == 10

    def test_all_metrics_have_required_fields(self):
        """每个指标含 name/description/collector/instrument_location。"""
        for m in METRICS_DOCS:
            assert m.name, f"指标缺 name: {m}"
            assert m.description, f"指标缺 description: {m.name}"
            assert m.collector, f"指标缺 collector: {m.name}"
            assert m.instrument_location, f"指标缺 instrument_location: {m.name}"

    def test_get_metric_doc_by_name(self):
        """按名查询指标定义。"""
        m = get_metric_doc("teamharness_asset_total")
        assert m is not None
        assert m.name == "teamharness_asset_total"

    def test_get_metric_doc_not_found(self):
        """不存在的指标名 → None。"""
        assert get_metric_doc("nonexistent") is None

    def test_to_dict_list(self):
        """to_dict_list 返回 dict 列表。"""
        lst = to_dict_list()
        assert len(lst) == 10
        assert all(isinstance(d, dict) for d in lst)
        assert "name" in lst[0]


# ---------------------------------------------------------------------------
# GovernanceMetrics.ingest_events（SubTask 9.5）
# ---------------------------------------------------------------------------


class TestIngestEvents:
    """客户端上报事件采集。"""

    def test_ingest_writes_to_adoption_event(
        self, database, asset_index, upsert_helper
    ):
        """上报事件写入 adoption_event 表。"""
        upsert_helper(asset_index, id="rule-x")
        metrics = GovernanceMetrics(database)
        events = [
            {
                "event_id": "evt-1",
                "event_type": "recall",
                "asset_id": "rule-x",
                "agent_id": "agent-1",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            }
        ]
        accepted, rejected = metrics.ingest_events(events, agent_id="agent-1")
        assert accepted == 1
        assert rejected == 0

        from server.infra_db.models import AdoptionEvent
        from sqlalchemy import select
        with database.session() as sess:
            rows = list(sess.scalars(select(AdoptionEvent)))
            assert len(rows) == 1
            assert rows[0].asset_id == "rule-x"
            assert rows[0].event_type == "recall"
            assert rows[0].member_id == "alice"
            payload = json.loads(rows[0].payload)
            assert payload["event_id"] == "evt-1"

    def test_ingest_idempotent_by_event_id(
        self, database, asset_index, upsert_helper
    ):
        """相同 event_id 重试 → 不重复计数。"""
        upsert_helper(asset_index, id="rule-x")
        metrics = GovernanceMetrics(database)
        events = [
            {
                "event_id": "evt-dup",
                "event_type": "recall",
                "asset_id": "rule-x",
                "member_id": "alice",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            }
        ]
        # 第一次上报
        a1, r1 = metrics.ingest_events(events, agent_id="agent-1")
        assert a1 == 1
        # 重复上报同一 event_id
        a2, r2 = metrics.ingest_events(events, agent_id="agent-1")
        assert a2 == 0
        assert r2 == 1  # 被拒绝（重复）

    def test_ingest_rejects_empty_asset_id(
        self, database
    ):
        """asset_id 为空 → rejected。"""
        metrics = GovernanceMetrics(database)
        events = [
            {
                "event_id": "evt-empty",
                "event_type": "recall",
                "asset_id": "",
                "member_id": "alice",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            }
        ]
        accepted, rejected = metrics.ingest_events(events, agent_id="agent-1")
        assert accepted == 0
        assert rejected == 1

    def test_ingest_empty_events(self, database):
        """空事件列表 → (0, 0)。"""
        metrics = GovernanceMetrics(database)
        accepted, rejected = metrics.ingest_events([], agent_id="agent-1")
        assert accepted == 0
        assert rejected == 0

    def test_ingest_batch_multiple_events(
        self, database, asset_index, upsert_helper
    ):
        """批量上报多事件。"""
        upsert_helper(asset_index, id="rule-a")
        upsert_helper(asset_index, id="rule-b")
        metrics = GovernanceMetrics(database)
        events = [
            {
                "event_id": "evt-1",
                "event_type": "recall",
                "asset_id": "rule-a",
                "member_id": "alice",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            },
            {
                "event_id": "evt-2",
                "event_type": "adopt",
                "asset_id": "rule-b",
                "member_id": "bob",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "metadata": {},
            },
        ]
        accepted, rejected = metrics.ingest_events(events, agent_id="agent-1")
        assert accepted == 2
        assert rejected == 0

    def test_ingest_timestamp_parsed(
        self, database, asset_index, upsert_helper
    ):
        """timestamp 字段解析为 occurred_at。"""
        upsert_helper(asset_index, id="rule-x")
        metrics = GovernanceMetrics(database)
        ts = "2026-01-15T10:30:00Z"
        events = [
            {
                "event_id": "evt-ts",
                "event_type": "recall",
                "asset_id": "rule-x",
                "member_id": "alice",
                "timestamp": ts,
                "metadata": {},
            }
        ]
        metrics.ingest_events(events, agent_id="agent-1")

        from server.infra_db.models import AdoptionEvent
        from sqlalchemy import select
        with database.session() as sess:
            row = sess.scalars(select(AdoptionEvent)).first()
            assert row is not None
            assert row.occurred_at.year == 2026
            assert row.occurred_at.month == 1
            assert row.occurred_at.day == 15


# ---------------------------------------------------------------------------
# Prometheus render（SubTask 9.5）
# ---------------------------------------------------------------------------


class TestPrometheusRender:
    """Prometheus scrape 输出。"""

    def test_render_returns_bytes(self, database):
        """render_prometheus 返回 bytes（prometheus_client 可用时非空）。"""
        metrics = GovernanceMetrics(database)
        body = metrics.render_prometheus()
        assert isinstance(body, bytes)
        if PROMETHEUS_AVAILABLE:
            # 若 prometheus_client 可用，body 应含指标名
            assert b"teamharness" in body or len(body) == 0  # 无数据时可能为空


# ---------------------------------------------------------------------------
# FastAPI /v1/metrics 端点（SubTask 9.6）
# ---------------------------------------------------------------------------


class TestMetricsEndpoint:
    """/v1/metrics 批量上报端点。"""

    def _build_app(self, metrics: GovernanceMetrics):
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(build_router(metrics))
        return app

    def test_post_metrics_endpoint(
        self, database, asset_index, upsert_helper
    ):
        """POST /v1/metrics 批量上报 → ack。"""
        upsert_helper(asset_index, id="rule-x")
        metrics = GovernanceMetrics(database)
        app = self._build_app(metrics)
        client = TestClient(app)

        resp = client.post(
            "/v1/metrics",
            json={
                "events": [
                    {
                        "event_id": "evt-1",
                        "event_type": "recall",
                        "asset_id": "rule-x",
                        "member_id": "alice",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    }
                ],
                "agent_id": "agent-1",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["accepted"] == 1
        assert data["rejected"] == 0

    def test_get_metric_definitions_endpoint(
        self, database
    ):
        """GET /v1/metrics/definitions → 10 个指标定义。"""
        metrics = GovernanceMetrics(database)
        app = self._build_app(metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/definitions")
        assert resp.status_code == 200
        data = resp.json()
        assert len(data["metrics"]) == 10

    def test_get_prometheus_endpoint(
        self, database
    ):
        """GET /v1/metrics/prometheus → Prometheus exposition format。"""
        metrics = GovernanceMetrics(database)
        app = self._build_app(metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/prometheus")
        assert resp.status_code == 200
        # media_type 应为 text/plain
        assert "text/plain" in resp.headers.get("content-type", "")

    def test_get_dashboard_endpoint(
        self, database
    ):
        """GET /v1/metrics/dashboard → Grafana 嵌入数据。"""
        metrics = GovernanceMetrics(database)
        app = self._build_app(metrics)
        client = TestClient(app)

        resp = client.get("/v1/metrics/dashboard")
        assert resp.status_code == 200
        data = resp.json()
        assert "definitions" in data
        assert "prometheus_available" in data
        assert len(data["definitions"]) == 10
