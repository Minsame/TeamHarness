"""虚拟团队测试 — conftest

在单进程内模拟多成员并发，不依赖 Docker。

模拟方式：
- mock 服务端 FastAPI app（用 TestClient）
- 每个成员用独立线程 + 独立 API Key + 独立 RecallClient 实例
- 共享同一个 TestClient（线程安全）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest


# ---------------------------------------------------------------------------
# 虚拟成员定义
# ---------------------------------------------------------------------------


@dataclass
class VirtualMember:
    """虚拟团队成员。"""

    member_id: str
    agent_id: str
    api_key: str
    personal_branch: str = ""

    def __post_init__(self) -> None:
        if not self.personal_branch:
            self.personal_branch = f"members/{self.member_id}"


# 默认 3 人团队
TEAM_3 = [
    VirtualMember("alice", "agent-alice", "key-alice"),
    VirtualMember("bob", "agent-bob", "key-bob"),
    VirtualMember("charlie", "agent-charlie", "key-charlie"),
]

# 默认 5 人团队
TEAM_5 = TEAM_3 + [
    VirtualMember("dave", "agent-dave", "key-dave"),
    VirtualMember("eve", "agent-eve", "key-eve"),
]


# ---------------------------------------------------------------------------
# Mock 服务端
# ---------------------------------------------------------------------------


class MockServer:
    """线程安全的 mock 服务端，记录所有请求。

    模拟 TeamHarness 服务端的核心端点行为：
    - /v1/auth/apikey → 返回 API Key
    - /v1/recall/list → 返回召回结果
    - /v1/recall/read → 返回资产内容
    - /v1/metrics → 记录 metrics 事件
    - /v1/webhook/git → 记录 webhook 事件
    - /v1/system/selfcheck → 返回健康状态
    - /v1/governance/dashboard → 返回看板数据
    - /v1/review/dedup → 返回去重建议
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.recall_requests: list[dict[str, Any]] = []
        self.read_requests: list[dict[str, Any]] = []
        self.metrics_events: list[dict[str, Any]] = []
        self.webhook_events: list[dict[str, Any]] = []
        self.apikey_issues: list[dict[str, Any]] = []
        self.dedup_requests: list[dict[str, Any]] = []
        self.dashboard_requests: list[dict[str, Any]] = []
        self._event_ids_seen: set[str] = set()

    def recall_list(self, agent_id: str, query: str = "", **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            self.recall_requests.append({
                "agent_id": agent_id,
                "query": query,
                "timestamp": time.time(),
            })
        return {"assets": [], "total": 0, "query": query}

    def recall_read(self, agent_id: str, asset_id: str) -> dict[str, Any]:
        with self._lock:
            self.read_requests.append({
                "agent_id": agent_id,
                "asset_id": asset_id,
                "timestamp": time.time(),
            })
        return {"asset_id": asset_id, "content": "mock content"}

    def metrics_batch(self, agent_id: str, event_id: str = "", **kwargs: Any) -> dict[str, Any]:
        with self._lock:
            is_duplicate = event_id in self._event_ids_seen if event_id else False
            if event_id:
                self._event_ids_seen.add(event_id)
            self.metrics_events.append({
                "agent_id": agent_id,
                "event_id": event_id,
                "is_duplicate": is_duplicate,
                "timestamp": time.time(),
                **kwargs,
            })
        return {"acknowledged": True, "is_duplicate": is_duplicate}

    def webhook(self, ref: str, commits: list[dict] | None = None) -> dict[str, Any]:
        with self._lock:
            self.webhook_events.append({
                "ref": ref,
                "commits": commits or [],
                "timestamp": time.time(),
            })
        return {"status": "accepted"}

    def issue_apikey(self, member_id: str) -> dict[str, str]:
        with self._lock:
            self.apikey_issues.append({"member_id": member_id, "timestamp": time.time()})
        return {"api_key": f"key-{member_id}", "agent_id": f"agent-{member_id}"}

    def dedup(self, pr_id: str, assets: list[dict] | None = None) -> dict[str, Any]:
        with self._lock:
            self.dedup_requests.append({
                "pr_id": pr_id,
                "assets": assets or [],
                "timestamp": time.time(),
            })
        return {"pr_id": pr_id, "suggestions": []}

    def dashboard(self) -> dict[str, Any]:
        with self._lock:
            self.dashboard_requests.append({"timestamp": time.time()})
        return {"modules": [], "alerts": []}

    def selfcheck(self) -> dict[str, str]:
        return {"status": "ok"}

    def reset(self) -> None:
        with self._lock:
            self.recall_requests.clear()
            self.read_requests.clear()
            self.metrics_events.clear()
            self.webhook_events.clear()
            self.apikey_issues.clear()
            self.dedup_requests.clear()
            self.dashboard_requests.clear()
            self._event_ids_seen.clear()


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_server() -> MockServer:
    """线程安全的 mock 服务端。"""
    server = MockServer()
    yield server
    server.reset()


@pytest.fixture
def team_3() -> list[VirtualMember]:
    """3 人团队。"""
    return TEAM_3.copy()


@pytest.fixture
def team_5() -> list[VirtualMember]:
    """5 人团队。"""
    return TEAM_5.copy()


@pytest.fixture
def thread_pool() -> ThreadPoolExecutor:
    """线程池（测试结束后自动关闭）。"""
    pool = ThreadPoolExecutor(max_workers=10)
    yield pool
    pool.shutdown(wait=True)
