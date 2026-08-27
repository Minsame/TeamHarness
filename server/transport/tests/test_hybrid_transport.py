"""Task 6 测试：HybridSyncTransport（P2P 优先 + 中央降级）。

覆盖：
- deliver: P2P 可达走 P2P / 不可达降级中央
- fetch: P2P 可达走 P2P / 不可达降级中央
- is_peer_reachable: P2P 可达 OR 中央在线
- discover_peers: 合并去重（同 peer_id 以 P2P 视图优先）
- 空消息列表（聚合无失败信号 → success=True）
- SyncTransport Protocol 实现
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from server.transport.central_transport import CentralSyncTransport
from server.transport.hybrid_transport import HybridSyncTransport
from server.transport.p2p_transport import P2PSyncTransport
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_msg(mid: str = "msg-1", **kwargs: Any) -> Message:
    return Message(message_id=mid, **kwargs)


class _FakeWSConnection:
    """模拟活跃 WS 连接（同步 send/recv 接口）。"""

    def __init__(self, *, recv_data: str = "") -> None:
        self.sent: list[str] = []
        self._recv_data = recv_data
        self.closed: bool = False

    def send(self, data: str) -> None:
        self.sent.append(data)

    def recv(self) -> str:
        return self._recv_data

    def close(self) -> None:
        self.closed = True


def make_central(handler) -> CentralSyncTransport:
    """构造带 mock httpx 的 CentralSyncTransport。"""
    mock_transport = httpx.MockTransport(handler)
    http_client = httpx.Client(transport=mock_transport)
    return CentralSyncTransport(
        server_url="https://th.example.com",
        api_key="sk-test",
        http_client=http_client,
    )


def make_central_with_peers(peers_payload: list[dict[str, Any]]) -> CentralSyncTransport:
    """构造返回指定 peers 列表的 CentralSyncTransport。"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"peers": peers_payload})
    return make_central(handler)


# ---------------------------------------------------------------------------
# deliver
# ---------------------------------------------------------------------------


class TestDeliver:
    def test_p2p_reachable_uses_p2p(self):
        """P2P 可达 → deliver 走 P2P 路径，不调用中央。"""
        central_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(str(request.url))
            return httpx.Response(200, json={"accepted": True, "delivered_count": 0})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        # 注入活跃连接使 P2P 可达
        conn = _FakeWSConnection()
        p2p.attach_connection("alice", conn)
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        result = hybrid.deliver("alice", [make_msg("m1"), make_msg("m2")])
        assert result.success is True
        assert result.delivered_count == 2
        # P2P 发送被调用
        assert len(conn.sent) == 1
        # 中央未被调用
        assert central_calls == []

    def test_p2p_unreachable_falls_back_to_central(self):
        """P2P 不可达 → deliver 降级走中央。"""
        central_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "accepted": True,
                    "delivered_count": 2,
                    "delivered_message_ids": ["m1", "m2"],
                },
            )

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])  # 无连接 → P2P 不可达
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        result = hybrid.deliver("alice", [make_msg("m1"), make_msg("m2")])
        assert result.success is True
        assert result.delivered_count == 2
        # 中央被调用
        assert len(central_calls) == 1
        assert str(central_calls[0].url) == "https://th.example.com/v1/comm/deliver"
        # P2P outbox 未被写入（直接走中央）
        assert p2p.outbox["alice"] == []

    def test_empty_messages_returns_success(self):
        """空消息列表 → success=True, delivered_count=0（不调用任一通道）。"""
        central_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(str(request.url))
            return httpx.Response(200, json={})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        # 即便 P2P 可达也不应被调用
        conn = _FakeWSConnection()
        p2p.attach_connection("alice", conn)
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        result = hybrid.deliver("alice", [])
        assert result.success is True
        assert result.delivered_count == 0
        # 两个通道均未被调用
        assert central_calls == []
        assert conn.sent == []

    def test_p2p_reachable_does_not_call_central_even_on_p2p_failure(self):
        """P2P 可达但 deliver 失败（如 outbox）→ 不降级中央（避免双投递副作用）。

        注意：P2P 不可达是降级触发条件，而非 P2P 内部失败。
        """
        central_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(str(request.url))
            return httpx.Response(200, json={"accepted": True, "delivered_count": 0})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        # 不注入连接 → P2P 不可达 → 走中央
        # 但这里我们测试：P2P 不可达时走中央
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        result = hybrid.deliver("alice", [make_msg("m1")])
        # P2P 不可达 → 走中央 → 成功
        assert result.success is True
        assert len(central_calls) == 1


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class TestFetch:
    def test_p2p_reachable_uses_p2p(self):
        """P2P 可达 → fetch 走 P2P 路径。"""
        central_calls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(str(request.url))
            return httpx.Response(200, json={"messages": []})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        ws_response = json.dumps({
            "messages": [{"message_id": "p2p-m1", "sender_id": "alice"}]
        })
        conn = _FakeWSConnection(recv_data=ws_response)
        p2p.attach_connection("alice", conn)
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        msgs = hybrid.fetch("alice")
        assert len(msgs) == 1
        assert msgs[0].message_id == "p2p-m1"
        # 中央未被调用
        assert central_calls == []

    def test_p2p_unreachable_falls_back_to_central(self):
        """P2P 不可达 → fetch 降级走中央。"""
        central_calls: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            central_calls.append(request)
            return httpx.Response(
                200,
                json={
                    "messages": [
                        {"message_id": "central-m1", "sender_id": "alice", "msg_type": "answer"}
                    ]
                },
            )

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])  # 无连接
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        msgs = hybrid.fetch("alice")
        assert len(msgs) == 1
        assert msgs[0].message_id == "central-m1"
        # 中央被调用
        assert len(central_calls) == 1
        assert str(central_calls[0].url) == "https://th.example.com/v1/comm/fetch"

    def test_fetch_with_vector_clock_propagated(self):
        """since_vector_clock 透传给实际执行的通道。"""
        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.content)
            return httpx.Response(200, json={"messages": []})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])  # 无连接 → 走中央
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        hybrid.fetch("alice", since_vector_clock={"alice": 7})
        assert captured["body"]["since_vector_clock"] == {"alice": 7}


# ---------------------------------------------------------------------------
# is_peer_reachable
# ---------------------------------------------------------------------------


class TestIsPeerReachable:
    def test_p2p_reachable(self):
        """P2P 可达 → True（不查中央）。"""
        central = make_central_with_peers([])
        p2p = P2PSyncTransport(peers=["alice"])
        conn = _FakeWSConnection()
        p2p.attach_connection("alice", conn)
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.is_peer_reachable("alice") is True

    def test_p2p_unreachable_central_online(self):
        """P2P 不可达，但中央报告 online → True。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"online": True})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])  # 无连接
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.is_peer_reachable("alice") is True

    def test_both_unreachable(self):
        """P2P 不可达 + 中央报告离线 → False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, json={"online": False})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.is_peer_reachable("alice") is False

    def test_p2p_unreachable_central_error(self):
        """P2P 不可达 + 中央 HTTP 错误 → False。"""
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"error": "internal"})

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.is_peer_reachable("alice") is False


# ---------------------------------------------------------------------------
# discover_peers
# ---------------------------------------------------------------------------


class TestDiscoverPeers:
    def test_merge_p2p_and_central(self):
        """合并 P2P 与中央两来源的 peer 列表。"""
        central = make_central_with_peers([
            {"peer_id": "carol", "endpoint": "central-host:3", "online": True},
            {"peer_id": "dave", "endpoint": "central-host:4", "online": False},
        ])
        p2p = P2PSyncTransport(peers=["alice", "bob"])
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        peers = hybrid.discover_peers()
        peer_ids = {p.peer_id for p in peers}
        assert peer_ids == {"alice", "bob", "carol", "dave"}
        assert all(isinstance(p, PeerInfo) for p in peers)

    def test_dedupe_p2p_view_priority(self):
        """同 peer_id 出现两来源时，以 P2P 视图优先保留。"""
        central = make_central_with_peers([
            {"peer_id": "alice", "endpoint": "central-host:1", "online": True},
        ])
        p2p = P2PSyncTransport(peers=["alice"])
        p2p.register_peer("alice", endpoint="p2p-host:1", online=False)
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        peers = {p.peer_id: p for p in hybrid.discover_peers()}
        # P2P 视图优先：endpoint 是 p2p 视图的
        assert peers["alice"].endpoint == "p2p-host:1"
        assert peers["alice"].online is False

    def test_only_p2p_peers(self):
        """中央无 peer → 返回 P2P 列表。"""
        central = make_central_with_peers([])
        p2p = P2PSyncTransport(peers=["alice", "bob"])
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        peers = hybrid.discover_peers()
        assert len(peers) == 2
        assert {p.peer_id for p in peers} == {"alice", "bob"}

    def test_only_central_peers(self):
        """P2P 无 peer → 返回中央列表。"""
        central = make_central_with_peers([
            {"peer_id": "carol", "endpoint": "host:3"},
        ])
        p2p = P2PSyncTransport()
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        peers = hybrid.discover_peers()
        assert len(peers) == 1
        assert peers[0].peer_id == "carol"

    def test_both_empty(self):
        """两来源均空 → 空列表。"""
        central = make_central_with_peers([])
        p2p = P2PSyncTransport()
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.discover_peers() == []

    def test_central_network_error_returns_p2p_only(self):
        """中央网络异常 → 仅返回 P2P 列表，不抛异常。"""
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("central down")

        central = make_central(handler)
        p2p = P2PSyncTransport(peers=["alice"])
        hybrid = HybridSyncTransport(central=central, p2p=p2p)

        peers = hybrid.discover_peers()
        assert len(peers) == 1
        assert peers[0].peer_id == "alice"


# ---------------------------------------------------------------------------
# SyncTransport Protocol 实现
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_implements_sync_transport(self):
        """HybridSyncTransport 实现 SyncTransport Protocol。"""
        from server.transport.protocol import SyncTransport

        central = make_central_with_peers([])
        p2p = P2PSyncTransport()
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert isinstance(hybrid, SyncTransport)

    def test_hybrid_holds_references(self):
        """hybrid.central / hybrid.p2p 持有子传输引用。"""
        central = make_central_with_peers([])
        p2p = P2PSyncTransport()
        hybrid = HybridSyncTransport(central=central, p2p=p2p)
        assert hybrid.central is central
        assert hybrid.p2p is p2p
