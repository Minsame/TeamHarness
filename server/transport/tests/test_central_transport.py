"""Task 6 测试：CentralSyncTransport（httpx 同步客户端）。

覆盖：
- deliver: 成功 / 空消息 / HTTP 错误 / 网络异常 / 部分失败
- fetch: 成功 / 空 / HTTP 错误 / 网络异常
- is_peer_reachable: online=True/False / 网络异常
- discover_peers: 正常返回 / 网络异常
- 鉴权 header（Bearer api_key）
- 响应体状态标志聚合（success 汇总多路信号，非硬编码 False）
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from server.transport.central_transport import CentralSyncTransport
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# 辅助：构造 mock httpx.Client
# ---------------------------------------------------------------------------


def make_transport(handler) -> CentralSyncTransport:
    """用 httpx.MockTransport 构造 CentralSyncTransport。"""
    mock_transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=mock_transport)
    return CentralSyncTransport(
        server_url="https://th.example.com",
        api_key="sk-test-abc",
        timeout=10,
        http_client=http_client,
    )


def make_msg(mid: str = "msg-1", **kwargs: Any) -> Message:
    return Message(message_id=mid, **kwargs)


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------


class TestDeliver:
    def test_success(self):
        """服务端 200 + accepted=True + delivered_count=2 → success=True。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["headers"] = dict(request.headers)
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "accepted": True,
                    "delivered_count": 2,
                    "failed_count": 0,
                    "delivered_message_ids": ["msg-1", "msg-2"],
                },
            )

        transport = make_transport(handler)
        result = transport.deliver(
            "alice",
            [make_msg("msg-1"), make_msg("msg-2")],
        )
        assert isinstance(result, SyncResult)
        assert result.success is True
        assert result.delivered_count == 2
        assert result.failed_count == 0
        assert result.delivered_message_ids == ["msg-1", "msg-2"]
        # URL / 方法 / 鉴权
        assert captured["url"] == "https://th.example.com/v1/comm/deliver"
        assert captured["method"] == "POST"
        assert captured["headers"].get("authorization") == "Bearer sk-test-abc"
        # 请求体
        assert captured["body"]["peer_id"] == "alice"
        assert len(captured["body"]["messages"]) == 2
        assert captured["body"]["messages"][0]["message_id"] == "msg-1"

    def test_empty_messages_returns_success(self):
        """空消息列表 → success=True, delivered_count=0（不保留待定语义）。"""
        transport = make_transport(lambda r: httpx.Response(200, json={}))
        result = transport.deliver("alice", [])
        assert result.success is True
        assert result.delivered_count == 0
        assert result.failed_count == 0
        assert result.pending_count == 0

    def test_http_error_returns_failure(self):
        """服务端 500 → success=False, failed_count 计入。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        transport = make_transport(handler)
        result = transport.deliver("alice", [make_msg("msg-1"), make_msg("msg-2")])
        assert result.success is False
        assert result.failed_count == 2
        assert "HTTP 500" in result.error

    def test_network_error_returns_failure(self):
        """网络异常 → success=False, failed_count 计入，不抛异常。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        transport = make_transport(handler)
        result = transport.deliver("alice", [make_msg("msg-1")])
        assert result.success is False
        assert result.failed_count == 1
        assert "network error" in result.error

    def test_partial_failure_aggregates_success_false(self):
        """响应体状态标志聚合：服务端返回 failed_count=1 → success=False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={
                    "accepted": True,
                    "delivered_count": 1,
                    "failed_count": 1,
                    "delivered_message_ids": ["msg-1"],
                },
            )

        transport = make_transport(handler)
        result = transport.deliver("alice", [make_msg("msg-1"), make_msg("msg-2")])
        # 聚合：HTTP 200 + accepted=True，但 failed_count=1 → success=False
        assert result.success is False
        assert result.delivered_count == 1
        assert result.failed_count == 1

    def test_server_not_accepted(self):
        """服务端 accepted=False → success=False（即使 HTTP 200）。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"accepted": False, "delivered_count": 0, "error": "rate limited"},
            )

        transport = make_transport(handler)
        result = transport.deliver("alice", [make_msg("msg-1")])
        assert result.success is False
        assert result.delivered_count == 0
        assert "rate limited" in result.error

    def test_message_serialization(self):
        """deliver 请求体序列化 Message 全字段。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"accepted": True, "delivered_count": 1})

        transport = make_transport(handler)
        msg = make_msg(
            "msg-full",
            event_id="evt-1",
            sender_id="alice",
            recipient_id="bob",
            msg_type="ask",
            payload={"q": "lint?"},
            timestamp="2026-08-12T10:00:00Z",
            in_reply_to="msg-prev",
            sender_key_hash="abc",
            signature="sig",
        )
        transport.deliver("bob", [msg])
        serialized = captured["body"]["messages"][0]
        assert serialized["message_id"] == "msg-full"
        assert serialized["event_id"] == "evt-1"
        assert serialized["sender_id"] == "alice"
        assert serialized["recipient_id"] == "bob"
        assert serialized["msg_type"] == "ask"
        assert serialized["payload"] == {"q": "lint?"}
        assert serialized["timestamp"] == "2026-08-12T10:00:00Z"
        assert serialized["in_reply_to"] == "msg-prev"
        assert serialized["sender_key_hash"] == "abc"
        assert serialized["signature"] == "sig"


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class TestFetch:
    def test_success(self):
        """POST /v1/comm/fetch 返回 list[Message]。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            captured["body"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"message_id": "m1", "sender_id": "bob", "msg_type": "answer"},
                        {"message_id": "m2", "sender_id": "bob", "msg_type": "share_asset"},
                    ]
                },
            )

        transport = make_transport(handler)
        msgs = transport.fetch("bob", since_vector_clock={"alice": 5})
        assert isinstance(msgs, list)
        assert len(msgs) == 2
        assert all(isinstance(m, Message) for m in msgs)
        assert msgs[0].message_id == "m1"
        assert msgs[0].sender_id == "bob"
        assert msgs[0].msg_type == "answer"
        # URL / 方法
        assert captured["url"] == "https://th.example.com/v1/comm/fetch"
        assert captured["method"] == "POST"
        # 请求体含 since_vector_clock
        assert captured["body"]["peer_id"] == "bob"
        assert captured["body"]["since_vector_clock"] == {"alice": 5}

    def test_empty_messages(self):
        """fetch 返回空列表。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"messages": []})

        transport = make_transport(handler)
        assert transport.fetch("alice") == []

    def test_http_error_returns_empty(self):
        """HTTP 404 → 返回空列表，不抛异常。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "not found"})

        transport = make_transport(handler)
        assert transport.fetch("alice") == []

    def test_network_error_returns_empty(self):
        """网络异常 → 返回空列表。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("network down")

        transport = make_transport(handler)
        assert transport.fetch("alice") == []

    def test_without_vector_clock(self):
        """since_vector_clock 缺省时不发送该字段。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": []})

        transport = make_transport(handler)
        transport.fetch("alice")
        assert "since_vector_clock" not in captured["body"]
        assert captured["body"]["peer_id"] == "alice"


# ---------------------------------------------------------------------------
# is_peer_reachable
# ---------------------------------------------------------------------------


class TestIsPeerReachable:
    def test_online_true(self):
        """GET /v1/comm/peer/{id}/status online=True → True。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(200, json={"online": True, "peer_id": "alice"})

        transport = make_transport(handler)
        assert transport.is_peer_reachable("alice") is True
        assert captured["url"] == "https://th.example.com/v1/comm/peer/alice/status"
        assert captured["method"] == "GET"

    def test_online_false(self):
        """online=False → False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"online": False})

        transport = make_transport(handler)
        assert transport.is_peer_reachable("bob") is False

    def test_http_error_returns_false(self):
        """HTTP 404 → False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"error": "unknown peer"})

        transport = make_transport(handler)
        assert transport.is_peer_reachable("carol") is False

    def test_network_error_returns_false(self):
        """网络异常 → False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("unreachable")

        transport = make_transport(handler)
        assert transport.is_peer_reachable("alice") is False


# ---------------------------------------------------------------------------
# discover_peers
# ---------------------------------------------------------------------------


class TestDiscoverPeers:
    def test_returns_peer_list(self):
        """GET /v1/comm/peers 返回 list[PeerInfo]。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            captured["method"] = request.method
            return httpx.Response(
                200,
                json={
                    "peers": [
                        {
                            "peer_id": "alice",
                            "agent_id": "agent-1",
                            "endpoint": "host:1",
                            "online": True,
                            "last_seen": "2026-08-12T10:00:00Z",
                            "capabilities": ["ask", "share_asset"],
                        },
                        {
                            "peer_id": "bob",
                            "agent_id": "agent-2",
                            "endpoint": "host:2",
                            "online": False,
                        },
                    ]
                },
            )

        transport = make_transport(handler)
        peers = transport.discover_peers()
        assert isinstance(peers, list)
        assert len(peers) == 2
        assert all(isinstance(p, PeerInfo) for p in peers)
        assert peers[0].peer_id == "alice"
        assert peers[0].agent_id == "agent-1"
        assert peers[0].endpoint == "host:1"
        assert peers[0].online is True
        assert peers[0].last_seen == "2026-08-12T10:00:00Z"
        assert peers[0].capabilities == ["ask", "share_asset"]
        assert peers[1].peer_id == "bob"
        assert peers[1].online is False
        # URL / 方法
        assert captured["url"] == "https://th.example.com/v1/comm/peers"
        assert captured["method"] == "GET"

    def test_empty_peers(self):
        """无 peer → 空列表。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"peers": []})

        transport = make_transport(handler)
        assert transport.discover_peers() == []

    def test_http_error_returns_empty(self):
        """HTTP 500 → 空列表。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        transport = make_transport(handler)
        assert transport.discover_peers() == []

    def test_network_error_returns_empty(self):
        """网络异常 → 空列表。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("down")

        transport = make_transport(handler)
        assert transport.discover_peers() == []


# ---------------------------------------------------------------------------
# 鉴权 / 配置
# ---------------------------------------------------------------------------


class TestAuthAndConfig:
    def test_bearer_token_in_headers(self):
        """api_key → Authorization: Bearer <api_key>。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"peers": []})

        transport = make_transport(handler)
        transport.discover_peers()
        assert captured["headers"].get("authorization") == "Bearer sk-test-abc"
        assert captured["headers"].get("content-type") == "application/json"

    def test_no_api_key_no_auth_header(self):
        """api_key 为空 → 不发送 Authorization。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["headers"] = dict(request.headers)
            return httpx.Response(200, json={"peers": []})

        mock_transport = httpx.MockTransport(handler)
        http_client = httpx.Client(transport=mock_transport)
        transport = CentralSyncTransport(
            server_url="https://th.example.com",
            api_key="",
            http_client=http_client,
        )
        transport.discover_peers()
        assert "authorization" not in captured["headers"]

    def test_server_url_trailing_slash_stripped(self):
        """server_url 末尾斜杠被剥离，避免双斜杠 URL。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["url"] = str(request.url)
            return httpx.Response(200, json={"peers": []})

        mock_transport = httpx.MockTransport(handler)
        http_client = httpx.Client(transport=mock_transport)
        transport = CentralSyncTransport(
            server_url="https://th.example.com/",  # 末尾斜杠
            api_key="sk",
            http_client=http_client,
        )
        transport.discover_peers()
        assert captured["url"] == "https://th.example.com/v1/comm/peers"

    def test_default_timeout(self):
        """timeout 默认 15。"""
        transport = CentralSyncTransport(
            server_url="https://th.example.com",
            api_key="sk",
        )
        assert transport.timeout == 15

    def test_implements_sync_transport(self):
        """CentralSyncTransport 实现 SyncTransport Protocol。"""
        from server.transport.protocol import SyncTransport

        transport = CentralSyncTransport(server_url="https://th.example.com", api_key="sk")
        assert isinstance(transport, SyncTransport)
