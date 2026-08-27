"""Task 5 测试：SyncTransport Protocol 接口契约与拓扑常量。"""

from __future__ import annotations

from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
    VALID_TOPOLOGIES,
    SyncTransport,
)
from server.transport.types import Message, PeerInfo, SyncResult


class TestTopologyConstants:
    """拓扑类型常量。"""

    def test_central_constant(self):
        assert TOPOLOGY_CENTRAL == "central"

    def test_p2p_constant(self):
        assert TOPOLOGY_P2P == "p2p"

    def test_hybrid_constant(self):
        assert TOPOLOGY_HYBRID == "hybrid"

    def test_valid_topologies_set(self):
        """VALID_TOPOLOGIES 包含全部三种拓扑。"""
        assert VALID_TOPOLOGIES == {"central", "p2p", "hybrid"}

    def test_valid_topologies_contains_each(self):
        assert TOPOLOGY_CENTRAL in VALID_TOPOLOGIES
        assert TOPOLOGY_P2P in VALID_TOPOLOGIES
        assert TOPOLOGY_HYBRID in VALID_TOPOLOGIES

    def test_invalid_topology_not_in_set(self):
        assert "mesh" not in VALID_TOPOLOGIES
        assert "" not in VALID_TOPOLOGIES


class _MockSyncTransport:
    """SyncTransport 的 mock 实现（用于验证接口契约）。"""

    def __init__(self) -> None:
        self._peers: dict[str, PeerInfo] = {
            "alice": PeerInfo(peer_id="alice", online=True, endpoint="host:1"),
            "bob": PeerInfo(peer_id="bob", online=False, endpoint="host:2"),
        }
        self._outbox: dict[str, list[Message]] = {"alice": [], "bob": []}

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        if peer_id not in self._peers:
            return SyncResult(success=False, failed_count=len(messages), error="unknown peer")
        if self._peers[peer_id].online:
            self._outbox[peer_id].extend(messages)
            return SyncResult(
                success=True,
                delivered_count=len(messages),
                delivered_message_ids=[m.message_id for m in messages],
            )
        # peer 离线 → 暂存待投递
        self._outbox[peer_id].extend(messages)
        return SyncResult(
            success=False,
            pending_count=len(messages),
            error="peer offline, pending delivery",
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        return list(self._outbox.get(peer_id, []))

    def is_peer_reachable(self, peer_id: str) -> bool:
        info = self._peers.get(peer_id)
        return info is not None and info.online

    def discover_peers(self) -> list[PeerInfo]:
        return list(self._peers.values())


class _IncompleteTransport:
    """缺少 discover_peers 方法，不满足 SyncTransport 契约。"""

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        return SyncResult()

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        return []

    def is_peer_reachable(self, peer_id: str) -> bool:
        return False


class TestSyncTransportProtocol:
    """SyncTransport Protocol 接口契约。"""

    def test_mock_is_sync_transport(self):
        """完整实现的 mock 被识别为 SyncTransport。"""
        transport = _MockSyncTransport()
        assert isinstance(transport, SyncTransport)

    def test_incomplete_is_not_sync_transport(self):
        """缺少方法的实现不被识别为 SyncTransport。"""
        transport = _IncompleteTransport()
        assert not isinstance(transport, SyncTransport)

    def test_deliver_returns_sync_result(self):
        """deliver 返回 SyncResult。"""
        transport = _MockSyncTransport()
        result = transport.deliver("alice", [Message(message_id="m1")])
        assert isinstance(result, SyncResult)
        assert result.success is True
        assert result.delivered_count == 1

    def test_deliver_offline_peer_pending(self):
        """deliver 给离线 peer → pending_count 计入。"""
        transport = _MockSyncTransport()
        result = transport.deliver("bob", [Message(message_id="m1"), Message(message_id="m2")])
        assert isinstance(result, SyncResult)
        assert result.success is False
        assert result.pending_count == 2
        assert result.delivered_count == 0

    def test_deliver_unknown_peer_failed(self):
        """deliver 给未知 peer → failed_count 计入。"""
        transport = _MockSyncTransport()
        result = transport.deliver("carol", [Message(message_id="m1")])
        assert result.success is False
        assert result.failed_count == 1

    def test_deliver_empty_messages(self):
        """deliver 空消息列表 → success=True, delivered_count=0。"""
        transport = _MockSyncTransport()
        result = transport.deliver("alice", [])
        assert result.success is True
        assert result.delivered_count == 0

    def test_fetch_returns_list_of_message(self):
        """fetch 返回 list[Message]。"""
        transport = _MockSyncTransport()
        transport.deliver("alice", [Message(message_id="m1")])
        msgs = transport.fetch("alice")
        assert isinstance(msgs, list)
        assert all(isinstance(m, Message) for m in msgs)
        assert len(msgs) == 1

    def test_fetch_with_vector_clock(self):
        """fetch 接受 since_vector_clock 参数（增量语义由实现定义）。"""
        transport = _MockSyncTransport()
        msgs = transport.fetch("alice", since_vector_clock={"alice": 5})
        assert isinstance(msgs, list)

    def test_fetch_empty_peer(self):
        """fetch 未知 peer 返回空列表。"""
        transport = _MockSyncTransport()
        assert transport.fetch("nobody") == []

    def test_is_peer_reachable_returns_bool(self):
        """is_peer_reachable 返回 bool。"""
        transport = _MockSyncTransport()
        assert isinstance(transport.is_peer_reachable("alice"), bool)
        assert transport.is_peer_reachable("alice") is True
        assert transport.is_peer_reachable("bob") is False
        assert transport.is_peer_reachable("nobody") is False

    def test_discover_peers_returns_list_of_peer_info(self):
        """discover_peers 返回 list[PeerInfo]。"""
        transport = _MockSyncTransport()
        peers = transport.discover_peers()
        assert isinstance(peers, list)
        assert all(isinstance(p, PeerInfo) for p in peers)
        assert len(peers) == 2
