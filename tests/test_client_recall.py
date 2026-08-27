"""RecallClient 测试（SubTask 6.4 + 6.11）。

覆盖：
- 离线模式（无 server_url）→ 本地 BM25-lite 匹配 + working copy 读取
- 在线模式（mock httpx）→ 远端 /v1/recall/* 调用
- 网络检测 check_network
- module_path 推断（explicit / env / cwd）
- recall_list / recall_read 在线与离线路径
- 私有资产参与本地匹配
- 离线降级一致性（degraded=True 标记）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import httpx
import pytest

from server.client.config import ClientConfig
from server.client.placeholders import (
    RecallListItem,
    RecallListResult,
    RecallReadResult,
)
from server.client.recall_client import (
    NetworkStatus,
    OfflineRecallResult,
    RecallClient,
    _bm25_lite,
    _tokenize,
)
from server.common.models import AssetType, Scope
from server.client.working_copy import WorkingCopy
from server.client.private_isolation import PrivateIsolation
from server.transport.types import Message, SyncResult


# ---------------------------------------------------------------------------
# 辅助：构造含资产的仓库
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_assets(tmp_path: Path) -> Path:
    """构造含项目级 + 模块级资产的仓库。"""
    wc = WorkingCopy(tmp_path)
    wc.create_asset(
        AssetType.RULE,
        "global-lint",
        owner="alice",
        body="# 全局 lint 规范\n关键词: lint naming import",
        scope=Scope.TEAM,
    )
    wc.create_asset(
        AssetType.RULE,
        "backend-lint",
        owner="bob",
        body="# 后端 lint\n关键词: backend lint api",
        module_path="modules/backend",
        scope=Scope.TEAM,
    )
    wc.create_asset(
        AssetType.MEMORY,
        "backend-tips",
        owner="alice",
        body="# 后端开发备忘\n关键词: backend api tips",
        module_path="modules/backend",
        scope=Scope.TEAM,
    )
    return tmp_path


# ---------------------------------------------------------------------------
# BM25-lite 单元测试
# ---------------------------------------------------------------------------


def test_tokenize_simple():
    toks = _tokenize("Hello World lint")
    assert toks == ["hello", "world", "lint"]


def test_tokenize_empty():
    assert _tokenize("") == []


def test_tokenize_chinese():
    # 中文按 word 字符分词（\w 含 Unicode）
    toks = _tokenize("lint 规范")
    assert "lint" in toks


def test_bm25_lite_basic_match():
    query = ["lint"]
    doc = ["lint", "naming", "import"]
    score = _bm25_lite(
        query, doc, avg_doc_len=3.0, doc_count=2, doc_freq={"lint": 1}
    )
    assert score > 0.0


def test_bm25_lite_no_match_returns_zero():
    query = ["nonexistent"]
    doc = ["lint", "naming"]
    score = _bm25_lite(
        query, doc, avg_doc_len=2.0, doc_count=1, doc_freq={"lint": 1}
    )
    assert score == 0.0


def test_bm25_lite_empty_query_returns_zero():
    assert _bm25_lite([], ["a"], avg_doc_len=1.0, doc_count=1, doc_freq={}) == 0.0


def test_bm25_lite_empty_doc_returns_zero():
    assert _bm25_lite(["a"], [], avg_doc_len=0.0, doc_count=0, doc_freq={}) == 0.0


# ---------------------------------------------------------------------------
# 离线模式（无 server_url）
# ---------------------------------------------------------------------------


def test_offline_mode_no_server_url(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    assert client.is_online() is False


def test_offline_recall_list_returns_degraded(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    result = client.recall_list(agent_id="agent-1", query="lint")
    assert result.degraded is True
    # 至少匹配到含 "lint" 的资产
    assert len(result.items) >= 1
    ids = {it.asset_id for it in result.items}
    assert "rule-global-lint" in ids
    assert "rule-backend-lint" in ids


def test_offline_recall_list_no_query_returns_all(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    result = client.recall_list(agent_id="agent-1", query=None)
    # 无 query → 退化为按 module_path 过滤的全部
    assert len(result.items) >= 2


def test_offline_recall_list_filter_by_module(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    result = client.recall_list(
        agent_id="agent-1", query="backend", module_path="modules/backend"
    )
    ids = {it.asset_id for it in result.items}
    # 应命中 backend-lint 和 backend-tips（均在 modules/backend 下）
    assert "rule-backend-lint" in ids
    assert "memory-backend-tips" in ids
    # 项目级资产也参与（module_path 非空时项目级也纳入候选）
    # 但 query="backend" 不匹配 global-lint 的正文
    assert "rule-global-lint" not in ids


def test_offline_recall_read_existing(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-global-lint")
    assert result.gone is False
    assert "lint" in result.content
    assert result.frontmatter.get("id") == "rule-global-lint"


def test_offline_recall_read_not_found_returns_gone(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-nonexistent")
    assert result.gone is True


def test_offline_get_sync_status(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    status = client.get_sync_status()
    # 离线时返回占位
    assert status.sync_source == "offline"


# ---------------------------------------------------------------------------
# 私有资产参与本地匹配
# ---------------------------------------------------------------------------


def test_offline_recall_includes_private_assets(tmp_path: Path):
    wc = WorkingCopy(tmp_path)
    wc.create_asset(
        AssetType.RULE,
        "public-rule",
        owner="alice",
        body="public lint rule",
        scope=Scope.TEAM,
    )
    pi = PrivateIsolation(tmp_path)
    pi.write_private_asset(
        "rules",
        "private-secret",
        frontmatter={"id": "rule-private-secret", "type": "rule", "owner": "alice"},
        body="private lint secret",
    )
    cfg = ClientConfig(repo_root=str(tmp_path))
    client = RecallClient(cfg)
    result = client.recall_list(agent_id="agent-1", query="lint")
    ids = {it.asset_id for it in result.items}
    assert "rule-public-rule" in ids
    assert "rule-private-secret" in ids


# ---------------------------------------------------------------------------
# 在线模式（mock httpx）
# ---------------------------------------------------------------------------


class RecallMockTransport(httpx.BaseTransport):
    """mock httpx transport，返回预设的 recall 响应。"""

    def __init__(self, list_response: dict[str, Any] | None = None,
                 read_response: dict[str, Any] | None = None,
                 status_response: dict[str, Any] | None = None):
        self.list_response = list_response or {
            "items": [
                {
                    "asset_id": "rule-remote-1",
                    "type": "rule",
                    "title": "remote rule",
                    "tags": ["lint"],
                    "relevance_score": 0.95,
                    "git_path": "rules/remote-1.md",
                    "module_path": "",
                }
            ],
            "as_of_commit": "abc123",
            "sync_lag_seconds": 0,
            "degraded": False,
        }
        self.read_response = read_response or {
            "content": "# remote content",
            "frontmatter": {"id": "rule-remote-1", "type": "rule"},
        }
        self.status_response = status_response or {
            "last_synced_commit": "abc123",
            "lag_seconds": 0,
            "sync_source": "git",
        }
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        url = str(request.url)
        if "/v1/recall/list" in url:
            return httpx.Response(200, json=self.list_response)
        if "/v1/recall/read" in url:
            return httpx.Response(200, json=self.read_response)
        if "/v1/sync/status" in url:
            return httpx.Response(200, json=self.status_response)
        return httpx.Response(404, json={"error": "not found"})


def test_online_recall_list_calls_remote(tmp_path: Path):
    transport = RecallMockTransport()
    http_client = httpx.Client(transport=transport)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        api_key="sk-test",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, http_client=http_client)
    result = client.recall_list(agent_id="agent-1", query="lint")
    assert result.degraded is False
    assert len(result.items) == 1
    assert result.items[0].asset_id == "rule-remote-1"
    assert result.as_of_commit == "abc123"
    # 验证调用了远端 API
    assert any("/v1/recall/list" in str(r.url) for r in transport.calls)


def test_online_recall_read_calls_remote(tmp_path: Path):
    transport = RecallMockTransport()
    http_client = httpx.Client(transport=transport)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        api_key="sk-test",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, http_client=http_client)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-remote-1")
    assert result.gone is False
    assert "remote content" in result.content
    assert result.frontmatter.get("id") == "rule-remote-1"


def test_online_recall_read_410_gone(tmp_path: Path):
    transport = RecallMockTransport(
        read_response={"alternative_asset_ids": ["rule-alt"]}
    )
    # 自定义 410 响应
    class GoneTransport(httpx.BaseTransport):
        def __init__(self):
            self.calls = []

        def handle_request(self, request: httpx.Request) -> httpx.Response:
            self.calls.append(request)
            if "/v1/recall/read" in str(request.url):
                return httpx.Response(410, json={"alternative_asset_ids": ["rule-alt"]})
            return httpx.Response(200, json={})

    http_client = httpx.Client(transport=GoneTransport())
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, http_client=http_client)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-deleted")
    assert result.gone is True
    assert "rule-alt" in result.alternative_asset_ids


def test_check_network_online(tmp_path: Path):
    transport = RecallMockTransport()
    http_client = httpx.Client(transport=transport)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, http_client=http_client)
    status = client.check_network(force=True)
    assert status.online is True
    assert status.error is None


def test_check_network_offline_no_server_url(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    client = RecallClient(cfg)
    status = client.check_network(force=True)
    assert status.online is False
    assert "server_url" in (status.error or "")


def test_check_network_server_error(tmp_path: Path):
    class ErrorTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

    http_client = httpx.Client(transport=ErrorTransport())
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, http_client=http_client)
    status = client.check_network(force=True)
    assert status.online is False
    assert "500" in (status.error or "")


# ---------------------------------------------------------------------------
# online override
# ---------------------------------------------------------------------------


def test_online_override_forces_offline(repo_with_assets: Path):
    cfg = ClientConfig(
        repo_root=str(repo_with_assets),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, online=False)
    # 即使有 server_url，online=False 强制离线
    assert client.is_online() is False
    result = client.recall_list(agent_id="agent-1", query="lint")
    assert result.degraded is True


def test_online_override_forces_online(tmp_path: Path):
    transport = RecallMockTransport()
    http_client = httpx.Client(transport=transport)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, online=True, http_client=http_client)
    assert client.is_online() is True


# ---------------------------------------------------------------------------
# 远端失败降级到本地
# ---------------------------------------------------------------------------


def test_remote_failure_falls_back_to_local(repo_with_assets: Path):
    class FailTransport(httpx.BaseTransport):
        def handle_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

    http_client = httpx.Client(transport=FailTransport())
    cfg = ClientConfig(
        repo_root=str(repo_with_assets),
        server_url="https://th.example.com",
    )
    # online=True 但远端调用失败 → _call_remote_list 返回 mock（degraded=True）
    # → 叠加本地匹配
    client = RecallClient(cfg, online=True, http_client=http_client)
    result = client.recall_list(agent_id="agent-1", query="lint")
    # 远端失败 + 本地匹配 → degraded=True
    assert result.degraded is True
    # 本地资产命中
    ids = {it.asset_id for it in result.items}
    assert "rule-global-lint" in ids


# ---------------------------------------------------------------------------
# manifest 缓存
# ---------------------------------------------------------------------------


def test_invalidate_manifest_cache(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    client = RecallClient(cfg)
    # 首次加载
    m1 = client._get_manifest()
    assert m1 is not None
    # 缓存命中（同一对象）
    m2 = client._get_manifest()
    assert m2 is m1
    # 失效后重建
    client.invalidate_manifest_cache()
    m3 = client._get_manifest()
    assert m3 is not m1


# ---------------------------------------------------------------------------
# module_path 推断
# ---------------------------------------------------------------------------


def test_recall_list_explicit_module(tmp_path: Path, monkeypatch):
    wc = WorkingCopy(tmp_path)
    wc.create_asset(
        AssetType.RULE,
        "backend-lint",
        owner="alice",
        body="backend lint",
        module_path="modules/backend",
        scope=Scope.TEAM,
    )
    cfg = ClientConfig(repo_root=str(tmp_path))
    client = RecallClient(cfg)
    # 显式传入 module_path
    result = client.recall_list(
        agent_id="agent-1", query="lint", explicit_module="modules/backend"
    )
    ids = {it.asset_id for it in result.items}
    assert "rule-backend-lint" in ids


# ---------------------------------------------------------------------------
# transport 路径（SyncTransport 注入）
# ---------------------------------------------------------------------------


class MockSyncTransport:
    """mock SyncTransport，记录 deliver 调用并通过 fetch 返回回复。

    responses: action -> response payload dict
    """

    def __init__(
        self,
        *,
        reachable: bool = True,
        responses: dict[str, dict[str, Any]] | None = None,
        deliver_success: bool = True,
    ):
        self.reachable = reachable
        self.responses = responses or {}
        self.deliver_success = deliver_success
        self.delivered: list[tuple[str, list[Message]]] = []
        self.fetched_peer_ids: list[str] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered.append((peer_id, list(messages)))
        if not self.deliver_success:
            return SyncResult(
                success=False, delivered_count=0, failed_count=len(messages)
            )
        return SyncResult(success=True, delivered_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        self.fetched_peer_ids.append(peer_id)
        replies: list[Message] = []
        for delivered_peer, msgs in self.delivered:
            if delivered_peer != peer_id:
                continue
            for msg in msgs:
                action = msg.payload.get("action", "")
                response = self.responses.get(action, {})
                replies.append(
                    Message(
                        message_id=f"reply-{msg.message_id}",
                        event_id=f"reply-{msg.message_id}",
                        sender_id=peer_id,
                        recipient_id=msg.sender_id,
                        msg_type="answer",
                        payload=dict(response),
                        timestamp="2026-01-01T00:00:00Z",
                        in_reply_to=msg.message_id,
                    )
                )
        return replies

    def is_peer_reachable(self, peer_id: str) -> bool:
        return self.reachable

    def discover_peers(self) -> list:
        return []


def test_transport_none_defaults_to_httpx(tmp_path: Path):
    """transport=None（默认）走 httpx 路径（回归测试）。"""
    http_transport = RecallMockTransport()
    http_client = httpx.Client(transport=http_transport)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, http_client=http_client)
    assert client._transport is None
    result = client.recall_list(agent_id="agent-1", query="lint")
    assert result.degraded is False
    assert result.items[0].asset_id == "rule-remote-1"
    # httpx 被调用（非 transport）
    assert any("/v1/recall/list" in str(r.url) for r in http_transport.calls)


def test_transport_recall_list(tmp_path: Path):
    """transport 注入时 recall_list 走 transport 路径。"""
    mock = MockSyncTransport(
        responses={
            "recall_list": {
                "items": [
                    {
                        "asset_id": "rule-transport-1",
                        "type": "rule",
                        "title": "transport rule",
                        "tags": ["lint"],
                        "relevance_score": 0.88,
                        "git_path": "rules/transport-1.md",
                        "module_path": "",
                    }
                ],
                "as_of_commit": "transport-commit",
                "sync_lag_seconds": 2,
                "degraded": False,
            }
        },
    )
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, transport=mock)
    result = client.recall_list(agent_id="agent-1", query="lint")
    assert result.degraded is False
    assert len(result.items) == 1
    assert result.items[0].asset_id == "rule-transport-1"
    assert result.items[0].relevance_score == 0.88
    assert result.as_of_commit == "transport-commit"
    assert result.sync_lag_seconds == 2
    # 验证 transport.deliver 被调用
    assert len(mock.delivered) == 1
    peer_id, msgs = mock.delivered[0]
    assert peer_id == "https://th.example.com"
    assert msgs[0].payload["action"] == "recall_list"
    assert msgs[0].payload["query"] == "lint"
    assert msgs[0].msg_type == "ask"
    assert msgs[0].sender_id == "agent-1"
    # 验证 fetch 被调用
    assert len(mock.fetched_peer_ids) == 1


def test_transport_recall_read(tmp_path: Path):
    """transport 注入时 recall_read 走 transport 路径。"""
    mock = MockSyncTransport(
        responses={
            "recall_read": {
                "content": "# transport content",
                "frontmatter": {"id": "rule-transport-1", "type": "rule"},
            }
        },
    )
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, transport=mock)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-transport-1")
    assert result.gone is False
    assert "transport content" in result.content
    assert result.frontmatter.get("id") == "rule-transport-1"
    # 验证 transport 被调用
    assert len(mock.delivered) == 1
    assert mock.delivered[0][1][0].payload["action"] == "recall_read"


def test_transport_recall_read_gone(tmp_path: Path):
    """transport 返回 gone=True 时 recall_read 返回 gone。"""
    mock = MockSyncTransport(
        responses={
            "recall_read": {
                "gone": True,
                "alternative_asset_ids": ["rule-alt"],
            }
        },
    )
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, transport=mock)
    result = client.recall_read(agent_id="agent-1", asset_id="rule-deleted")
    assert result.gone is True
    assert "rule-alt" in result.alternative_asset_ids


def test_transport_get_sync_status(tmp_path: Path):
    """transport 注入时 get_sync_status 走 transport 路径。"""
    mock = MockSyncTransport(
        responses={
            "sync_status": {
                "last_synced_commit": "transport-head",
                "lag_seconds": 5,
                "sync_source": "transport",
            }
        },
    )
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, transport=mock)
    status = client.get_sync_status()
    assert status.last_synced_commit == "transport-head"
    assert status.lag_seconds == 5
    assert status.sync_source == "transport"


def test_transport_check_network_reachable(tmp_path: Path):
    """transport 注入时 check_network 用 is_peer_reachable（可达）。"""
    mock = MockSyncTransport(reachable=True)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, transport=mock)
    status = client.check_network(force=True)
    assert status.online is True
    assert status.error is None


def test_transport_check_network_unreachable(tmp_path: Path):
    """transport is_peer_reachable=False 时 check_network 返回离线。"""
    mock = MockSyncTransport(reachable=False)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
    )
    client = RecallClient(cfg, transport=mock)
    status = client.check_network(force=True)
    assert status.online is False
    assert "unreachable" in (status.error or "")


def test_transport_deliver_failure_falls_back_to_mock(tmp_path: Path):
    """transport.deliver 失败时降级到 mock（degraded=True）。"""
    mock = MockSyncTransport(deliver_success=False, responses={})
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )
    client = RecallClient(cfg, online=True, transport=mock)
    result = client.recall_list(agent_id="agent-1", query="lint")
    # deliver 失败 → _call_via_transport 返回 None → mock_recall_list (degraded=True)
    assert result.degraded is True
