"""Task 6 测试：P2PSyncTransport（WebSocket + Stub 降级）。

覆盖：
- Stub 降级：websockets 未安装时（当前环境）所有连接尝试返回 Stub
- outbox 存储：无连接 deliver → 暂存 outbox + pending_count
- peer 状态管理：register_peer / discover_peers / is_peer_reachable
- 注入 mock 连接（attach_connection）：实时投递 + fetch 拉取
- 空消息列表（不保留待定语义）
- Stub 类构造签名兼容（`__init__(self, *args, **kwargs): pass`）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.transport.p2p_transport import (
    P2PSyncTransport,
    _HAS_WEBSOCKETS,
    _StubWSConnection,
)
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def make_msg(mid: str = "msg-1", **kwargs: Any) -> Message:
    return Message(message_id=mid, **kwargs)


class _FakeWSConnection:
    """模拟活跃的同步 WS 连接（用于测试实时投递路径）。

    提供 send / recv / close 接口（同步），可预设 recv 返回值。
    """

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


class _BrokenWSConnection:
    """连接已关闭（closed=True）。"""

    def __init__(self) -> None:
        self.closed: bool = True

    def send(self, data: str) -> None:
        raise OSError("connection closed")

    def recv(self) -> str:
        raise OSError("connection closed")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Stub 降级
# ---------------------------------------------------------------------------


class TestStubFallback:
    def test_has_websockets_property(self):
        """当前环境 websockets 是否可用（用于断言测试前提）。"""
        transport = P2PSyncTransport()
        assert transport.has_websockets is _HAS_WEBSOCKETS

    def test_open_connection_returns_stub_when_no_websockets(self):
        """websockets 未安装时 open_connection 返回 _StubWSConnection 实例。"""
        if _HAS_WEBSOCKETS:
            pytest.skip("websockets 已安装，跳过 Stub 降级测试")
        transport = P2PSyncTransport()
        conn = transport.open_connection("alice", "127.0.0.1", 7421)
        assert isinstance(conn, _StubWSConnection)

    def test_stub_connection_not_alive(self):
        """Stub 连接被判定为非活跃。"""
        transport = P2PSyncTransport()
        stub = _StubWSConnection("host", 1234)
        assert transport._is_connection_alive(stub) is False

    def test_stub_class_init_accepts_any_args(self):
        """Stub 类 __init__(self, *args, **kwargs): pass - 签名兼容。"""
        # 接受任意位置参数
        s1 = _StubWSConnection("host", 1234, "extra")
        assert isinstance(s1, _StubWSConnection)
        # 接受任意关键字参数
        s2 = _StubWSConnection(host="host", port=1234, foo="bar")
        assert isinstance(s2, _StubWSConnection)
        # 混合
        s3 = _StubWSConnection("host", port=1234)
        assert isinstance(s3, _StubWSConnection)
        # 无参数
        s4 = _StubWSConnection()
        assert isinstance(s4, _StubWSConnection)

    def test_stub_methods_are_noop(self):
        """Stub 各方法 no-op 返回。"""
        stub = _StubWSConnection()
        # send / recv / close 不抛异常
        assert stub.send("data") is None
        assert stub.recv() == ""
        assert stub.close() is None
        # closed 永远 True
        assert stub.closed is True

    def test_open_connection_stub_not_stored(self):
        """Stub 连接不写入 _connections（避免误判可达）。"""
        if _HAS_WEBSOCKETS:
            pytest.skip("websockets 已安装")
        transport = P2PSyncTransport()
        transport.open_connection("alice", "127.0.0.1", 7421)
        assert "alice" not in transport._connections
        assert transport.is_peer_reachable("alice") is False


# ---------------------------------------------------------------------------
# outbox 存储
# ---------------------------------------------------------------------------


class TestOutboxStorage:
    def test_deliver_no_connection_stores_outbox(self):
        """无活跃连接 → 消息存入 outbox, success=False, pending_count 计入。"""
        transport = P2PSyncTransport(peers=["alice"])
        result = transport.deliver("alice", [make_msg("m1"), make_msg("m2")])
        assert isinstance(result, SyncResult)
        assert result.success is False
        assert result.pending_count == 2
        assert result.delivered_count == 0
        # outbox 暂存
        assert len(transport.outbox["alice"]) == 2
        assert transport.outbox["alice"][0].message_id == "m1"

    def test_deliver_unknown_peer_stores_outbox(self):
        """未知 peer 也会暂存 outbox（自动创建条目）。"""
        transport = P2PSyncTransport()
        result = transport.deliver("unknown", [make_msg("m1")])
        assert result.success is False
        assert result.pending_count == 1
        assert "unknown" in transport.outbox
        assert len(transport.outbox["unknown"]) == 1

    def test_deliver_empty_messages_returns_success(self):
        """空消息列表 → success=True, delivered_count=0（不保留待定语义）。"""
        transport = P2PSyncTransport(peers=["alice"])
        result = transport.deliver("alice", [])
        assert result.success is True
        assert result.delivered_count == 0
        assert result.pending_count == 0
        assert result.failed_count == 0

    def test_deliver_multiple_accumulates_in_outbox(self):
        """多次 deliver 累积到同一 peer 的 outbox。"""
        transport = P2PSyncTransport(peers=["alice"])
        transport.deliver("alice", [make_msg("m1")])
        transport.deliver("alice", [make_msg("m2"), make_msg("m3")])
        assert len(transport.outbox["alice"]) == 3
        ids = [m.message_id for m in transport.outbox["alice"]]
        assert ids == ["m1", "m2", "m3"]


# ---------------------------------------------------------------------------
# peer 状态管理
# ---------------------------------------------------------------------------


class TestPeerRegistry:
    def test_init_with_peers_list(self):
        """构造时传入 peers 列表 → 全部注册到 PeerRegistry。"""
        transport = P2PSyncTransport(peers=["alice", "bob", "carol"])
        peers = transport.discover_peers()
        assert len(peers) == 3
        peer_ids = {p.peer_id for p in peers}
        assert peer_ids == {"alice", "bob", "carol"}

    def test_init_without_peers(self):
        """无 peers 参数 → 空 PeerRegistry。"""
        transport = P2PSyncTransport()
        assert transport.discover_peers() == []

    def test_register_peer(self):
        """register_peer 添加 peer。"""
        transport = P2PSyncTransport()
        transport.register_peer("alice", endpoint="host:1", online=False)
        peers = transport.discover_peers()
        assert len(peers) == 1
        assert peers[0].peer_id == "alice"
        assert peers[0].endpoint == "host:1"
        assert peers[0].online is False

    def test_register_peer_default_endpoint(self):
        """register_peer 未传 endpoint → 用 listen_host:listen_port。"""
        transport = P2PSyncTransport(listen_host="0.0.0.0", listen_port=9000)
        transport.register_peer("alice")
        peers = {p.peer_id: p for p in transport.discover_peers()}
        assert peers["alice"].endpoint == "0.0.0.0:9000"

    def test_is_peer_reachable_no_connection(self):
        """无连接 → False。"""
        transport = P2PSyncTransport(peers=["alice"])
        assert transport.is_peer_reachable("alice") is False

    def test_is_peer_reachable_unknown_peer(self):
        """未知 peer → False。"""
        transport = P2PSyncTransport()
        assert transport.is_peer_reachable("nobody") is False

    def test_peer_registry_returns_peerinfo_instances(self):
        """discover_peers 返回 PeerInfo 实例。"""
        transport = P2PSyncTransport(peers=["alice"])
        peers = transport.discover_peers()
        assert all(isinstance(p, PeerInfo) for p in peers)


# ---------------------------------------------------------------------------
# 注入 mock 连接（实时投递 / fetch）
# ---------------------------------------------------------------------------


class TestMockConnection:
    def test_attach_connection_makes_peer_reachable(self):
        """attach_connection 注入活跃连接 → is_peer_reachable True。"""
        transport = P2PSyncTransport(peers=["alice"])
        assert transport.is_peer_reachable("alice") is False
        conn = _FakeWSConnection()
        transport.attach_connection("alice", conn)
        assert transport.is_peer_reachable("alice") is True
        # peer online 标志同步更新
        peers = {p.peer_id: p for p in transport.discover_peers()}
        assert peers["alice"].online is True

    def test_deliver_with_active_connection(self):
        """有活跃连接 → 实时发送，success=True, delivered_count 计入。"""
        transport = P2PSyncTransport(peers=["alice"])
        conn = _FakeWSConnection()
        transport.attach_connection("alice", conn)
        result = transport.deliver("alice", [make_msg("m1"), make_msg("m2")])
        assert result.success is True
        assert result.delivered_count == 2
        assert result.delivered_message_ids == ["m1", "m2"]
        # 验证 WS send 被调用
        assert len(conn.sent) == 1
        payload = json.loads(conn.sent[0])
        assert payload["type"] == "deliver"
        assert len(payload["messages"]) == 2
        assert payload["messages"][0]["message_id"] == "m1"

    def test_deliver_with_active_connection_no_outbox(self):
        """有活跃连接 → 实时发送，不写入 outbox。"""
        transport = P2PSyncTransport(peers=["alice"])
        conn = _FakeWSConnection()
        transport.attach_connection("alice", conn)
        transport.deliver("alice", [make_msg("m1")])
        assert transport.outbox["alice"] == []

    def test_deliver_broken_connection_falls_back_to_outbox(self):
        """连接故障（send 抛异常）→ 暂存 outbox, pending_count 计入。"""
        transport = P2PSyncTransport(peers=["alice"])
        broken = _BrokenWSConnection()
        # 注入"已关闭"连接 - 但 _is_connection_alive 会判定为 False
        # 因此会直接落入 outbox 路径
        transport.attach_connection("alice", broken)
        result = transport.deliver("alice", [make_msg("m1")])
        assert result.success is False
        assert result.pending_count == 1
        assert len(transport.outbox["alice"]) == 1

    def test_deliver_send_raises_falls_back_to_outbox(self):
        """活跃连接 send 抛异常 → 暂存 outbox（覆盖 _send_via_ws 异常路径）。"""
        transport = P2PSyncTransport(peers=["alice"])

        # 构造一个"活跃但 send 抛异常"的连接：closed=False 但 send 失败
        class _FlakyConnection:
            def __init__(self) -> None:
                self.closed: bool = False

            def send(self, data: str) -> None:
                raise OSError("flaky")

            def recv(self) -> str:
                return ""

            def close(self) -> None:
                pass

        transport.attach_connection("alice", _FlakyConnection())
        result = transport.deliver("alice", [make_msg("m1"), make_msg("m2")])
        assert result.success is False
        assert result.pending_count == 2
        assert "WS send failed" in result.error
        # 暂存到 outbox
        assert len(transport.outbox["alice"]) == 2

    def test_detach_connection(self):
        """detach_connection 移除连接，peer online 变 False。"""
        transport = P2PSyncTransport(peers=["alice"])
        conn = _FakeWSConnection()
        transport.attach_connection("alice", conn)
        assert transport.is_peer_reachable("alice") is True
        transport.detach_connection("alice")
        assert transport.is_peer_reachable("alice") is False
        # peer 仍在 registry，但 online=False
        peers = {p.peer_id: p for p in transport.discover_peers()}
        assert peers["alice"].online is False
        # 连接被关闭
        assert conn.closed is True

    def test_detach_unknown_peer_no_error(self):
        """detach 未知 peer 不抛异常。"""
        transport = P2PSyncTransport()
        transport.detach_connection("nobody")  # 不抛异常


# ---------------------------------------------------------------------------
# fetch
# ---------------------------------------------------------------------------


class TestFetch:
    def test_fetch_no_connection_returns_incoming_cache(self):
        """无连接 → 返回本地 incoming 缓存。"""
        transport = P2PSyncTransport(peers=["alice"])
        # 空 incoming
        assert transport.fetch("alice") == []
        # push 后返回 incoming
        transport.push_incoming("alice", [make_msg("m1"), make_msg("m2")])
        msgs = transport.fetch("alice")
        assert len(msgs) == 2
        assert msgs[0].message_id == "m1"

    def test_fetch_unknown_peer_returns_empty(self):
        """fetch 未知 peer → 空列表。"""
        transport = P2PSyncTransport()
        assert transport.fetch("nobody") == []

    def test_fetch_with_active_connection_returns_ws_data(self):
        """有活跃连接 → 通过 WS recv 拉取消息。"""
        transport = P2PSyncTransport(peers=["alice"])
        ws_response = json.dumps({
            "messages": [
                {"message_id": "ws-1", "sender_id": "alice", "msg_type": "answer"},
                {"message_id": "ws-2", "sender_id": "alice", "msg_type": "share_asset"},
            ]
        })
        conn = _FakeWSConnection(recv_data=ws_response)
        transport.attach_connection("alice", conn)
        msgs = transport.fetch("alice", since_vector_clock={"alice": 3})
        assert len(msgs) == 2
        assert msgs[0].message_id == "ws-1"
        assert msgs[0].sender_id == "alice"
        # 验证 WS send 了 fetch 请求
        assert len(conn.sent) == 1
        req = json.loads(conn.sent[0])
        assert req["type"] == "fetch"
        assert req["since_vector_clock"] == {"alice": 3}

    def test_fetch_with_active_connection_invalid_json_returns_incoming(self):
        """活跃连接 recv 返回非法 JSON → 回退本地 incoming 缓存。"""
        transport = P2PSyncTransport(peers=["alice"])
        transport.push_incoming("alice", [make_msg("local-1")])
        conn = _FakeWSConnection(recv_data="not-json")
        transport.attach_connection("alice", conn)
        msgs = transport.fetch("alice")
        # 回退到 incoming
        assert len(msgs) == 1
        assert msgs[0].message_id == "local-1"

    def test_fetch_with_broken_connection_returns_incoming(self):
        """已关闭连接 → 回退 incoming 缓存。"""
        transport = P2PSyncTransport(peers=["alice"])
        transport.push_incoming("alice", [make_msg("local-1")])
        transport.attach_connection("alice", _BrokenWSConnection())
        msgs = transport.fetch("alice")
        assert len(msgs) == 1
        assert msgs[0].message_id == "local-1"

    def test_fetch_with_vector_clock(self):
        """fetch 接受 since_vector_clock 参数。"""
        transport = P2PSyncTransport(peers=["alice"])
        # 无连接情况下也接受参数
        msgs = transport.fetch("alice", since_vector_clock={"alice": 10})
        assert isinstance(msgs, list)


# ---------------------------------------------------------------------------
# SyncTransport Protocol 实现
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    def test_implements_sync_transport(self):
        """P2PSyncTransport 实现 SyncTransport Protocol。"""
        from server.transport.protocol import SyncTransport

        transport = P2PSyncTransport()
        assert isinstance(transport, SyncTransport)

    def test_default_listen_port(self):
        """默认 listen_port=7421。"""
        transport = P2PSyncTransport()
        assert transport.listen_port == 7421

    def test_default_listen_host(self):
        """默认 listen_host='0.0.0.0'。"""
        transport = P2PSyncTransport()
        assert transport.listen_host == "0.0.0.0"

    def test_custom_listen_config(self):
        """自定义 listen_host / listen_port。"""
        transport = P2PSyncTransport(listen_host="127.0.0.1", listen_port=9000)
        assert transport.listen_host == "127.0.0.1"
        assert transport.listen_port == 9000
