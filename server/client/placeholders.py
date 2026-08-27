"""占位 API 契约：依赖其他 Agent 但本波未就绪时使用 mock。

对应技术方案占位 API 契约（agent6-client.md 第 32-57 行）：
- Agent 4 提供 RecallService（/v1/recall/* / /v1/sync/status）— 本波未就绪
- Agent 5 提供 BindingService（/v1/category/suggest / /v1/auth/apikey）— 本波未就绪
- Agent 9 提供 GovernanceService（/v1/metrics 批量上报）— 第三波才就绪

切换真实调用：
    RecallService 真实实现位于 server.recall.service（Agent 4）；
    BindingService 真实实现位于 server.binding.service（Agent 5）；
    GovernanceService 真实实现位于 server.governance.service（Agent 9）。

切换方法：在 ClientConfig.server_url 非空时，recall_client / category-suggest /
adoption 模块会优先尝试 HTTP 真实调用；占位函数仅作离线/未就绪兜底，
返回格式与契约严格一致，便于上层代码无差别处理。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# RecallService 占位契约（Agent 4）
# ---------------------------------------------------------------------------


@dataclass
class RecallListItem:
    """recall/list 返回的单条资产摘要（契约格式）。"""

    asset_id: str
    type: str
    title: str
    tags: list[str] = field(default_factory=list)
    relevance_score: float = 0.0
    git_path: str = ""
    module_path: str = ""


@dataclass
class RecallListResult:
    """recall/list 返回结构。"""

    items: list[RecallListItem] = field(default_factory=list)
    as_of_commit: str = ""
    sync_lag_seconds: int = 0
    degraded: bool = False


@dataclass
class RecallReadResult:
    """recall/read 返回结构。"""

    content: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    gone: bool = False  # 410 Gone 标记
    alternative_asset_ids: list[str] = field(default_factory=list)


@dataclass
class SyncStatusResult:
    """GET /v1/sync/status 返回结构。"""

    last_synced_commit: str = ""
    lag_seconds: int = 0
    sync_source: str = ""


def mock_recall_list(
    agent_id: str,
    query: str | None = None,
    module_path: str | None = None,
    consistency: str = "eventual",
) -> RecallListResult:
    """RecallService.list 占位实现：返回空结果，标记 degraded=True。

    真实 API 就绪后，由 recall_client.RecallClient._call_remote_list 走 HTTP
    调用；占位仅在 Agent 4 未就绪或离线降级时使用。
    """
    return RecallListResult(items=[], as_of_commit="", sync_lag_seconds=0, degraded=True)


def mock_recall_read(agent_id: str, asset_id: str) -> RecallReadResult:
    """RecallService.read 占位实现：返回 gone=True 表示读不到。"""
    return RecallReadResult(content="", frontmatter={}, gone=True)


def mock_sync_status() -> SyncStatusResult:
    """sync/status 占位实现。"""
    return SyncStatusResult(last_synced_commit="", lag_seconds=0, sync_source="mock")


# ---------------------------------------------------------------------------
# BindingService 占位契约（Agent 5）
# ---------------------------------------------------------------------------


def mock_category_suggest(content: str, module_path: str | None = None) -> list[str]:
    """POST /v1/category/suggest 占位实现：返回空候选列表。

    真实服务由 Agent 5 提供，会返回 3 个候选项；占位返回 []，
    由 CLI category-suggest 命令告知用户"装配服务未就绪"。
    """
    return []


@dataclass
class ApiKeyIssueResult:
    """POST /v1/auth/apikey 返回结构。"""

    api_key: str
    agent_id: str


def mock_issue_api_key(member_id: str) -> ApiKeyIssueResult:
    """POST /v1/auth/apikey 占位实现：返回空 api_key。"""
    return ApiKeyIssueResult(api_key="", agent_id="")


# ---------------------------------------------------------------------------
# GovernanceService 占位契约（Agent 9）
# ---------------------------------------------------------------------------


@dataclass
class MetricsBatchAck:
    """POST /v1/metrics 批量上报 ack。"""

    accepted: int
    rejected: int = 0
    error: str | None = None


def mock_metrics_batch(events: list[dict[str, Any]]) -> MetricsBatchAck:
    """POST /v1/metrics 占位实现：返回 accepted=0。

    真实服务由 Agent 9 提供；占位仅记录到本地采纳率缓存（adoption.py），
    联网恢复后由守护进程批量 flush。
    """
    return MetricsBatchAck(accepted=0, rejected=len(events), error="governance service unavailable")


# ---------------------------------------------------------------------------
# 时间工具（占位模块共用）
# ---------------------------------------------------------------------------


def utcnow() -> datetime:
    """统一 UTC 当前时间（便于测试 mock）。"""
    return datetime.now(timezone.utc)
