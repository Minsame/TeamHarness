"""GovernanceMetrics — 指标采集 + /v1/metrics 批量上报端点（SubTask 9.5 + 9.6）。

对应技术方案 6.1 指标落地 + 8.6 采纳率反馈：
- Prometheus 指标（可选依赖，未安装时降级为 no-op）
- POST /v1/metrics：客户端批量上报采纳事件
- GET /v1/metrics/dashboard：Grafana 嵌入数据
- GET /v1/metrics/prometheus：Prometheus scrape 端点（prometheus_client 可用时）

设计要点：
- prometheus_client 不可用时，Counter/Gauge 降级为 no-op stub（不影响业务）
- 客户端上报事件写入 adoption_event 表（event_id 存入 payload 实现幂等）
- 服务端可采指标（recall_log 聚合）由 AdoptionMetricsService 提供，本模块仅采集客户端上报
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel, Field
from sqlalchemy import select

from server.governance.dashboard import DashboardService
from server.governance.metrics_docs import METRICS_DOCS, to_dict_list
from server.governance.pr_review_dedup import PRReviewDedupService
from server.infra_db.db import Database
from server.infra_db.models import AdoptionEvent

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Prometheus 可选依赖（降级 stub）
# ---------------------------------------------------------------------------

try:
    from prometheus_client import Counter, Gauge, Histogram, generate_latest

    PROMETHEUS_AVAILABLE = True
except ImportError:
    PROMETHEUS_AVAILABLE = False

    class _NoOpMetric:
        """prometheus_client 不可用时的 no-op stub。"""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        def labels(self, **_: Any) -> "_NoOpMetric":
            return self

        def inc(self, *_: Any, **__: Any) -> None:
            pass

        def set(self, *_: Any, **__: Any) -> None:
            pass

        def observe(self, *_: Any, **__: Any) -> None:
            pass

    Counter = _NoOpMetric  # type: ignore[assignment]
    Gauge = _NoOpMetric  # type: ignore[assignment]
    Histogram = _NoOpMetric  # type: ignore[assignment]

    def generate_latest() -> bytes:  # type: ignore[no-redef]
        return b""


# ---------------------------------------------------------------------------
# GovernanceMetrics — Prometheus 指标注册
# ---------------------------------------------------------------------------


class GovernanceMetrics:
    """治理指标采集器（Prometheus 可选）。

    用法：
        metrics = GovernanceMetrics(database)
        # 客户端上报事件 → 写 adoption_event + inc counter
        ack = metrics.ingest_events(events, agent_id="a-1")
        # Prometheus scrape
        body = metrics.render_prometheus()
    """

    def __init__(self, database: Database) -> None:
        self._db = database
        # Prometheus 指标定义（prometheus_client 不可用时为 no-op）
        self.asset_total = Gauge(
            "teamharness_asset_total", "资产总量", ["type", "scope"]
        )
        self.asset_active = Gauge(
            "teamharness_asset_active", "活跃资产数", ["type", "module_path"]
        )
        self.recall_count = Counter(
            "teamharness_recall_count_total",
            "召回次数累计",
            ["module_path", "asset_id"],
        )
        self.adoption_rate = Gauge(
            "teamharness_adoption_rate",
            "采纳率（read/recall）",
            ["asset_id", "module_path"],
        )
        self.adoption_stale = Gauge(
            "teamharness_adoption_stale_count", "采纳率 stale 资产数"
        )
        self.sync_lag = Gauge(
            "teamharness_index_sync_lag_seconds",
            "索引同步滞后秒数",
            ["sync_source"],
        )
        self.embedding_queue_pending = Gauge(
            "teamharness_embedding_queue_pending",
            "待处理 embedding 任务数",
            ["status", "model_version"],
        )
        self.distill_job_running = Gauge(
            "teamharness_distill_job_running",
            "运行中提炼任务数",
            ["trigger_source"],
        )
        self.repo_size = Gauge(
            "teamharness_repo_size_bytes", "仓库大小（字节）", ["repo_path"]
        )
        self.metrics_ingest_total = Counter(
            "teamharness_metrics_ingest_total",
            "客户端上报事件累计",
            ["event_type", "agent_id"],
        )
        self.metrics_ingest_errors = Counter(
            "teamharness_metrics_ingest_errors_total",
            "客户端上报失败累计",
            ["agent_id"],
        )

    # ------------------------------------------------------------------
    # 客户端上报事件采集
    # ------------------------------------------------------------------

    def ingest_events(
        self, events: list[dict[str, Any]], agent_id: str = ""
    ) -> tuple[int, int]:
        """写入客户端上报事件到 adoption_event 表。

        - event_id 存入 payload JSON 实现幂等（重试不重复计数）
        - occurred_at 从事件 timestamp 解析，缺失用 now
        - 返回 (accepted, rejected)
        """
        if not events:
            return (0, 0)
        accepted = 0
        rejected = 0
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            # 预查已存在的 event_id（幂等去重）
            new_event_ids = {
                str(e.get("event_id", "")) for e in events if e.get("event_id")
            }
            existing_ids: set[str] = set()
            if new_event_ids:
                # payload 字段含 event_id，用 LIKE 预筛（避免全表扫）
                stmt = (
                    select(AdoptionEvent.payload)
                    .where(AdoptionEvent.payload.like('%"event_id"%'))
                )
                for payload_str in sess.scalars(stmt):
                    try:
                        payload = json.loads(payload_str or "")
                        eid = str(payload.get("event_id", ""))
                        if eid in new_event_ids:
                            existing_ids.add(eid)
                    except (json.JSONDecodeError, TypeError):
                        continue

            for event in events:
                try:
                    event_id = str(event.get("event_id", ""))
                    if event_id and event_id in existing_ids:
                        rejected += 1
                        continue
                    asset_id = str(event.get("asset_id", ""))
                    if not asset_id:
                        rejected += 1
                        continue
                    event_type = str(event.get("event_type", "recall"))
                    member_id = str(event.get("member_id", ""))
                    module_path = str(event.get("module_path", ""))
                    # occurred_at 从 timestamp 解析
                    occurred_at = self._parse_timestamp(
                        str(event.get("timestamp", "")), fallback=now
                    )
                    metadata = dict(event.get("metadata") or {})
                    payload = {
                        "event_id": event_id,
                        "module_path": module_path,
                        "metadata": metadata,
                    }
                    sess.add(
                        AdoptionEvent(
                            asset_id=asset_id,
                            member_id=member_id or agent_id,
                            event_type=event_type,
                            stale=False,
                            occurred_at=occurred_at,
                            received_at=now,
                            payload=json.dumps(payload, ensure_ascii=False),
                        )
                    )
                    accepted += 1
                    # Prometheus counter
                    self.metrics_ingest_total.labels(
                        event_type=event_type, agent_id=agent_id
                    ).inc()
                    existing_ids.add(event_id)
                except Exception as exc:  # noqa: BLE001
                    rejected += 1
                    logger.warning("上报事件写入失败 event=%s err=%s", event, exc)
                    self.metrics_ingest_errors.labels(agent_id=agent_id).inc()
        return (accepted, rejected)

    # ------------------------------------------------------------------
    # Prometheus scrape
    # ------------------------------------------------------------------

    def render_prometheus(self) -> bytes:
        """渲染 Prometheus exposition format。"""
        if not PROMETHEUS_AVAILABLE:
            return b""
        # 更新实时派生指标
        self._refresh_gauges()
        return generate_latest()

    def _refresh_gauges(self) -> None:
        """刷新 Gauge 指标（从 DB 实时聚合）。"""
        from sqlalchemy import func

        from server.infra_db.models import (
            AssetIndex as AssetIndexRow,
            EmbeddingTaskQueue,
        )
        try:
            with self._db.session() as sess:
                # asset_total by type/scope
                stmt = (
                    select(
                        AssetIndexRow.type,
                        AssetIndexRow.scope,
                        func.count(AssetIndexRow.id),
                    )
                    .group_by(AssetIndexRow.type, AssetIndexRow.scope)
                )
                for t, s, c in sess.execute(stmt):
                    self.asset_total.labels(type=t, scope=s).set(int(c))

                # asset_active by type/module_path
                stmt = (
                    select(
                        AssetIndexRow.type,
                        AssetIndexRow.module_path,
                        func.count(AssetIndexRow.id),
                    )
                    .where(AssetIndexRow.status == "active")
                    .group_by(AssetIndexRow.type, AssetIndexRow.module_path)
                )
                for t, mp, c in sess.execute(stmt):
                    self.asset_active.labels(type=t, module_path=mp or "").set(int(c))

                # embedding_queue_pending by status/model_version
                stmt = (
                    select(
                        EmbeddingTaskQueue.status,
                        EmbeddingTaskQueue.model_version,
                        func.count(EmbeddingTaskQueue.id),
                    )
                    .group_by(
                        EmbeddingTaskQueue.status, EmbeddingTaskQueue.model_version
                    )
                )
                for st, mv, c in sess.execute(stmt):
                    self.embedding_queue_pending.labels(
                        status=st, model_version=mv
                    ).set(int(c))
        except Exception as exc:  # noqa: BLE001
            logger.warning("Prometheus gauge 刷新失败 err=%s", exc)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _parse_timestamp(
        self, ts: str, *, fallback: datetime
    ) -> datetime:
        """解析 ISO 格式时间戳，失败用 fallback。"""
        if not ts:
            return fallback
        try:
            # 兼容带 Z 后缀的 ISO 格式
            cleaned = ts.replace("Z", "+00:00")
            dt = datetime.fromisoformat(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt
        except (ValueError, TypeError):
            return fallback


# ---------------------------------------------------------------------------
# FastAPI Pydantic 模型
# ---------------------------------------------------------------------------


class MetricsEventSchema(BaseModel):
    """单个上报事件。"""

    event_id: str = ""
    event_type: str = "recall"
    asset_id: str
    agent_id: str = ""
    member_id: str = ""
    module_path: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MetricsBatchRequest(BaseModel):
    """POST /v1/metrics 请求体。"""

    events: list[MetricsEventSchema]
    agent_id: str = ""


class MetricsBatchAckSchema(BaseModel):
    """POST /v1/metrics 响应体。"""

    accepted: int
    rejected: int = 0
    error: str | None = None


class MetricDefinitionSchema(BaseModel):
    """指标定义。"""

    name: str
    description: str
    collector: str
    instrument_location: str
    labels: list[str] = Field(default_factory=list)
    aggregation: str = ""
    alert_threshold: str = ""


class PRReviewAssetSchema(BaseModel):
    """PR Review 待去重资产。"""

    id: str = ""
    type: str = "rule"
    content: str = ""
    content_hash: str = ""
    git_path: str = ""
    module_path: str = ""


class PRReviewDedupRequest(BaseModel):
    """POST /v1/review/dedup 请求体。

    契约（overview.md §1 + spec.md GovernanceService）：
      (pr_id, assets) → {duplicates, suggestions}
    assets 字段缺省时按空列表处理（向后兼容仅传 pr_id 的调用）。
    """

    pr_id: str
    assets: list[PRReviewAssetSchema] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# FastAPI router
# ---------------------------------------------------------------------------


governance_router = APIRouter(prefix="/v1", tags=["governance"])

# 全局 GovernanceMetrics 实例（由 configure_governance 注入）
_GOVERNANCE_METRICS: GovernanceMetrics | None = None
# 全局 PRReviewDedupService / DashboardService 实例（由 configure_governance 注入）
# 未注入时对应端点返回 503（与 _GOVERNANCE_METRICS 一致）
_GOVERNANCE_DEDUP: PRReviewDedupService | None = None
_GOVERNANCE_DASHBOARD: DashboardService | None = None


def configure_governance(
    metrics: GovernanceMetrics,
    *,
    dedup: PRReviewDedupService | None = None,
    dashboard: DashboardService | None = None,
) -> None:
    """注入 GovernanceMetrics 及相关治理服务实例（FastAPI 启动事件调用）。

    dedup / dashboard 为可选注入：
    - dedup：POST /v1/review/dedup 端点依赖
    - dashboard：GET /v1/governance/dashboard 端点依赖
    未注入时这两个端点返回 503（不阻断 /v1/metrics* 路由）。
    """
    global _GOVERNANCE_METRICS, _GOVERNANCE_DEDUP, _GOVERNANCE_DASHBOARD
    _GOVERNANCE_METRICS = metrics
    _GOVERNANCE_DEDUP = dedup
    _GOVERNANCE_DASHBOARD = dashboard


def _get_metrics() -> GovernanceMetrics:
    if _GOVERNANCE_METRICS is None:
        raise HTTPException(status_code=503, detail="GovernanceMetrics 未配置")
    return _GOVERNANCE_METRICS


def _get_dedup() -> PRReviewDedupService:
    if _GOVERNANCE_DEDUP is None:
        raise HTTPException(status_code=503, detail="PRReviewDedupService 未配置")
    return _GOVERNANCE_DEDUP


def _get_dashboard() -> DashboardService:
    if _GOVERNANCE_DASHBOARD is None:
        raise HTTPException(status_code=503, detail="DashboardService 未配置")
    return _GOVERNANCE_DASHBOARD


def build_router(metrics: GovernanceMetrics | None = None) -> APIRouter:
    """构造 router，可显式传入 metrics（用于测试隔离）。"""
    if metrics is not None:
        return _build_router_with_metrics(metrics)
    return governance_router


def _build_router_with_metrics(metrics: GovernanceMetrics) -> APIRouter:
    """为测试场景构造绑定特定 metrics 的 router。"""
    router = APIRouter(prefix="/v1", tags=["governance"])

    @router.post("/metrics", response_model=MetricsBatchAckSchema)
    def ingest_metrics(req: MetricsBatchRequest) -> dict[str, Any]:
        accepted, rejected = metrics.ingest_events(
            [e.model_dump() for e in req.events], agent_id=req.agent_id
        )
        return {"accepted": accepted, "rejected": rejected}

    @router.get("/metrics/definitions")
    def get_metric_definitions() -> dict[str, Any]:
        return {"metrics": to_dict_list()}

    @router.get("/metrics/prometheus")
    def prometheus_scrape(response: Response) -> Response:
        body = metrics.render_prometheus()
        return Response(
            content=body,
            media_type="text/plain; version=0.0.4; charset=utf-8",
        )

    @router.get("/metrics/dashboard")
    def metrics_dashboard() -> dict[str, Any]:
        return {
            "definitions": to_dict_list(),
            "prometheus_available": PROMETHEUS_AVAILABLE,
        }

    return router


# ---------------------------------------------------------------------------
# 全局 router 端点（生产路径）
# ---------------------------------------------------------------------------


@governance_router.post("/metrics", response_model=MetricsBatchAckSchema)
def ingest_metrics_endpoint(req: MetricsBatchRequest) -> dict[str, Any]:
    """POST /v1/metrics：客户端批量上报采纳事件。"""
    metrics = _get_metrics()
    accepted, rejected = metrics.ingest_events(
        [e.model_dump() for e in req.events], agent_id=req.agent_id
    )
    return {"accepted": accepted, "rejected": rejected}


@governance_router.get("/metrics/definitions")
def get_metric_definitions_endpoint() -> dict[str, Any]:
    """GET /v1/metrics/definitions：指标定义文档。"""
    return {"metrics": to_dict_list()}


@governance_router.get("/metrics/prometheus")
def prometheus_scrape_endpoint(response: Response) -> Response:
    """GET /v1/metrics/prometheus：Prometheus scrape 端点。"""
    metrics = _get_metrics()
    body = metrics.render_prometheus()
    return Response(
        content=body,
        media_type="text/plain; version=0.0.4; charset=utf-8",
    )


@governance_router.get("/metrics/dashboard")
def metrics_dashboard_endpoint() -> dict[str, Any]:
    """GET /v1/metrics/dashboard：Grafana 嵌入数据。"""
    return {
        "definitions": to_dict_list(),
        "prometheus_available": PROMETHEUS_AVAILABLE,
    }


@governance_router.post("/review/dedup")
def review_dedup_endpoint(req: PRReviewDedupRequest) -> dict[str, Any]:
    """POST /v1/review/dedup：PR Review 语义去重。

    契约（overview.md §1 GovernanceService）：
      (pr_id, assets) → {duplicates, suggestions}
    接收 PR 内新增/修改资产，返回每条资产的去重建议（命中资产、
    相似度、LLM 判断结果）。服务未注入时返回 503。
    """
    dedup = _get_dedup()
    result = dedup.review_pr(
        pr_id=req.pr_id,
        assets=[a.model_dump() for a in req.assets],
    )
    return result.to_dict()


@governance_router.get("/governance/dashboard")
def governance_dashboard_endpoint() -> dict[str, Any]:
    """GET /v1/governance/dashboard：治理看板聚合数据。

    契约（overview.md §1 GovernanceService）：
      → {module_stats, split_suggestions, alerts}
    返回模块资产数、拆分建议、未登记告警、召回命中率、采纳率、
    仓库大小告警等聚合数据。服务未注入时返回 503。
    """
    dashboard = _get_dashboard()
    return dashboard.get_dashboard().to_dict()


__all__ = [
    "GovernanceMetrics",
    "MetricsBatchAckSchema",
    "MetricsBatchRequest",
    "MetricsEventSchema",
    "MetricDefinitionSchema",
    "PRReviewAssetSchema",
    "PRReviewDedupRequest",
    "PROMETHEUS_AVAILABLE",
    "build_router",
    "configure_governance",
    "governance_router",
]
