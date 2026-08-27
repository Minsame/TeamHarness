"""Task 16 测试：PeerComm 通信核心入口。

覆盖：
- ask_peer 在线路径（deliver + fetch → realtime_answer）
- ask_peer 离线路径（shadow_comm 委托 / RuntimeError / outbox 写入）
- 可达性缓存（有效期内不重复探测 / 过期重新探测 / 不同 peer 独立）
- share_asset（可达实时推送 / 不可达写 outbox / SyncResult 正确）
- list_peers（discover_peers → peer_id 列表）

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_REALTIME_ANSWER,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_comm import PeerComm
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - ``reachable_peers``：可达 peer 集合
    - ``fetch_responses``：按 peer_id 预置 fetch 返回消息
    - ``auto_answer``：非空时 fetch 自动为已 deliver 的消息生成回答
      （in_reply_to 匹配 event_id，payload={"answer": auto_answer}）
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        fetch_responses: dict[str, list[Message]] | None = None,
        auto_answer: str = "",
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.fetch_responses = fetch_responses or {}
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.reachability_checks: list[str] = []  # 记录 is_peer_reachable 调用
        self.auto_answer = auto_answer

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(success=True, delivered_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        # 自动回答模式：为已投递的消息生成匹配的回答
        if self.auto_answer:
            responses: list[Message] = []
            for delivered_peer, messages in self.delivered_messages:
                if delivered_peer != peer_id:
                    continue
                for msg in messages:
                    responses.append(
                        Message(
                            message_id=str(uuid.uuid4()),
                            event_id=str(uuid.uuid4()),
                            sender_id=peer_id,
                            recipient_id=msg.sender_id,
                            msg_type="answer",
                            payload={"answer": self.auto_answer},
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            in_reply_to=msg.event_id,
                        )
                    )
            return responses
        return self.fetch_responses.get(peer_id, [])

    def is_peer_reachable(self, peer_id: str) -> bool:
        self.reachability_checks.append(peer_id)
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


class StubShadowComm:
    """Stub 实现 ShadowCommProtocol。"""

    def __init__(self) -> None:
        self.ask_calls: list[tuple[str, str, str]] = []

    def ask_peer(
        self, peer_id: str, question: str, *, in_reply_to: str = ""
    ) -> ConversationEvent:
        self.ask_calls.append((peer_id, question, in_reply_to))
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type="simulated_answer",
            peer_id=peer_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            degraded=True,
            realtime=False,
        )


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_comm(
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    shadow_comm: StubShadowComm | None = None,
    member_id: str = "alice",
    network_check_interval_seconds: int = 60,
) -> tuple[PeerComm, StubTransport, StubShadowComm]:
    """构造 PeerComm 测试实例，返回 (comm, transport, shadow_comm)。"""
    transport = transport or StubTransport()
    shadow_comm = shadow_comm or StubShadowComm()
    comm = PeerComm(
        transport=transport,
        mailbox=Mailbox(tmp_path / "mb", member_id),
        conversation_log=ConversationLog(tmp_path / "conversation.jsonl"),
        peer_snapshot_manager=PeerSnapshotManager(tmp_path / "snapshots"),
        member_id=member_id,
        network_check_interval_seconds=network_check_interval_seconds,
        shadow_comm=shadow_comm,
    )
    return comm, transport, shadow_comm


# ---------------------------------------------------------------------------
# TestPeerCommAskPeerOnline
# ---------------------------------------------------------------------------


class TestPeerCommAskPeerOnline:
    """ask_peer 在线实时路径。"""

    def test_online_calls_transport_deliver(self, tmp_path: Path):
        """peer 可达时走实时路径，transport.deliver 被调用。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "如何处理 X？")

        assert len(transport.delivered_messages) == 1
        peer_id, messages = transport.delivered_messages[0]
        assert peer_id == "bob"
        assert len(messages) == 1
        assert messages[0].msg_type == "ask"
        assert messages[0].payload["question"] == "如何处理 X？"

    def test_online_fetch_answer_creates_realtime_answer_event(self, tmp_path: Path):
        """fetch 返回回答时创建 realtime_answer 事件（realtime=True）。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="答案是 42")
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("bob", "问题？")

        assert event.event_type == EVENT_REALTIME_ANSWER
        assert event.realtime is True
        assert event.degraded is False
        assert event.payload.get("answer") == "答案是 42"
        assert event.peer_id == "bob"

    def test_online_writes_ask_and_answer_to_conversation_log(self, tmp_path: Path):
        """ask 事件和 answer 事件都写入 ConversationLog。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("bob", "问题？")

        # ConversationLog 应包含 1 个 ask + 1 个 realtime_answer
        assert comm.conversation_log.count(event_type=EVENT_ASK) == 1
        assert comm.conversation_log.count(event_type=EVENT_REALTIME_ANSWER) == 1

        # answer 事件的 in_reply_to 应指向 ask 事件的 event_id
        ask_events = comm.conversation_log.load_by_type(EVENT_ASK)
        assert len(ask_events) == 1
        assert event.in_reply_to == ask_events[0].event_id

    def test_online_no_answer_returns_pending_event(self, tmp_path: Path):
        """fetch 无回答时返回 pending 状态的回答事件（仍 realtime=True）。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="")
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("bob", "问题？")

        assert event.event_type == EVENT_REALTIME_ANSWER
        assert event.realtime is True
        assert event.payload.get("status") == "pending"

    def test_online_ask_event_carries_vector_clock(self, tmp_path: Path):
        """ask 事件携带递增后的本地 VectorClock，Message.payload 包含 vector_clock。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "问题？")

        ask_events = comm.conversation_log.load_by_type(EVENT_ASK)
        assert len(ask_events) == 1
        # 本地 member_id="alice" 的 counter 应已递增到 1
        assert ask_events[0].vector_clock.counters.get("alice") == 1

        # Message.payload 中应包含 vector_clock
        _, messages = transport.delivered_messages[0]
        assert "vector_clock" in messages[0].payload
        assert messages[0].payload["vector_clock"] == ask_events[0].vector_clock.to_dict()


# ---------------------------------------------------------------------------
# TestPeerCommAskPeerOffline
# ---------------------------------------------------------------------------


class TestPeerCommAskPeerOffline:
    """ask_peer 离线影子路径。"""

    def test_offline_delegates_to_shadow_comm(self, tmp_path: Path):
        """peer 不可达时委托给 shadow_comm。"""
        transport = StubTransport(reachable_peers=set())  # bob 不可达
        shadow = StubShadowComm()
        comm, _, shadow = _make_comm(tmp_path, transport=transport, shadow_comm=shadow)

        comm.ask_peer("bob", "问题？")

        assert len(shadow.ask_calls) == 1
        peer_id, question, in_reply_to = shadow.ask_calls[0]
        assert peer_id == "bob"
        assert question == "问题？"
        # in_reply_to 应为 ask 事件的 event_id（非空）
        assert in_reply_to != ""

    def test_offline_returns_simulated_answer_degraded(self, tmp_path: Path):
        """shadow_comm 返回 simulated_answer 事件（degraded=True）。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("bob", "问题？")

        assert event.event_type == "simulated_answer"
        assert event.degraded is True
        assert event.realtime is False

    def test_offline_no_shadow_comm_raises_runtime_error(self, tmp_path: Path):
        """shadow_comm 为 None 时抛 RuntimeError。"""
        transport = StubTransport(reachable_peers=set())
        comm = PeerComm(
            transport=transport,
            mailbox=Mailbox(tmp_path / "mb", "alice"),
            conversation_log=ConversationLog(tmp_path / "conversation.jsonl"),
            peer_snapshot_manager=PeerSnapshotManager(tmp_path / "snapshots"),
            member_id="alice",
            shadow_comm=None,
        )

        with pytest.raises(RuntimeError, match="shadow_comm not configured"):
            comm.ask_peer("bob", "问题？")

    def test_offline_writes_ask_event_to_outbox(self, tmp_path: Path):
        """ask 事件写入 outbox（pending_delivery 状态）。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "问题？")

        outbox = comm.mailbox.load_outbox()
        assert len(outbox) == 1
        ask_in_outbox = outbox[0]
        assert ask_in_outbox.event_type == EVENT_ASK
        assert comm.mailbox.get_status(ask_in_outbox.event_id) == STATUS_PENDING_DELIVERY

    def test_offline_ask_event_also_in_conversation_log(self, tmp_path: Path):
        """离线路径下 ask 事件同样写入 ConversationLog。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "问题？")

        assert comm.conversation_log.count(event_type=EVENT_ASK) == 1


# ---------------------------------------------------------------------------
# TestPeerCommReachabilityCache
# ---------------------------------------------------------------------------


class TestPeerCommReachabilityCache:
    """可达性缓存逻辑。"""

    def test_cache_valid_does_not_reprobe(self, tmp_path: Path):
        """缓存有效期内不重复探测。"""
        transport = StubTransport(reachable_peers={"bob"})
        comm, transport, _ = _make_comm(
            tmp_path, transport=transport, network_check_interval_seconds=60
        )

        # 第一次调用 — 探测
        assert comm._is_peer_reachable("bob") is True
        assert len(transport.reachability_checks) == 1

        # 第二次调用 — 缓存命中，不探测
        assert comm._is_peer_reachable("bob") is True
        assert len(transport.reachability_checks) == 1

    def test_cache_expired_reprobes(self, tmp_path: Path):
        """缓存过期后重新探测。"""
        transport = StubTransport(reachable_peers={"bob"})
        comm, transport, _ = _make_comm(
            tmp_path, transport=transport, network_check_interval_seconds=60
        )

        # 第一次调用 — 探测
        comm._is_peer_reachable("bob")
        assert len(transport.reachability_checks) == 1

        # 模拟缓存过期：将缓存时间戳设为很久以前
        comm._reachability_cache["bob"] = (True, time.time() - 1000)

        # 第二次调用 — 缓存已过期，重新探测
        comm._is_peer_reachable("bob")
        assert len(transport.reachability_checks) == 2

    def test_cache_independent_per_peer(self, tmp_path: Path):
        """不同 peer 的缓存独立。"""
        transport = StubTransport(reachable_peers={"bob", "carol"})
        comm, transport, _ = _make_comm(
            tmp_path, transport=transport, network_check_interval_seconds=60
        )

        # 分别探测两个 peer
        assert comm._is_peer_reachable("bob") is True
        assert comm._is_peer_reachable("carol") is True
        assert len(transport.reachability_checks) == 2

        # 再次调用 — 两个 peer 都应命中缓存
        assert comm._is_peer_reachable("bob") is True
        assert comm._is_peer_reachable("carol") is True
        assert len(transport.reachability_checks) == 2  # 无新增探测

    def test_cache_zero_interval_always_reprobes(self, tmp_path: Path):
        """interval=0 时缓存立即过期，每次都重新探测。"""
        transport = StubTransport(reachable_peers={"bob"})
        comm, transport, _ = _make_comm(
            tmp_path, transport=transport, network_check_interval_seconds=0
        )

        comm._is_peer_reachable("bob")
        comm._is_peer_reachable("bob")
        assert len(transport.reachability_checks) == 2


# ---------------------------------------------------------------------------
# TestPeerCommShareAsset
# ---------------------------------------------------------------------------


class TestPeerCommShareAsset:
    """share_asset 资产定向共享。"""

    def test_share_asset_online_calls_deliver(self, tmp_path: Path):
        """peer 可达时实时推送（transport.deliver 调用）。"""
        transport = StubTransport(reachable_peers={"bob"})
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        result = comm.share_asset("asset-1", "bob", asset_content={"key": "value"})

        assert result.success is True
        assert result.delivered_count == 1
        assert len(transport.delivered_messages) == 1
        peer_id, messages = transport.delivered_messages[0]
        assert peer_id == "bob"
        assert messages[0].msg_type == "share_asset"
        assert messages[0].payload["asset_id"] == "asset-1"
        assert messages[0].payload["content"] == {"key": "value"}

    def test_share_asset_offline_writes_outbox(self, tmp_path: Path):
        """peer 不可达时写入 outbox。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        result = comm.share_asset("asset-1", "bob", asset_content={"key": "value"})

        assert result.success is False
        assert result.pending_count == 1
        assert len(transport.delivered_messages) == 0

        # outbox 中应有 1 条 share_asset 消息
        outbox = comm.mailbox.load_outbox()
        assert len(outbox) == 1
        assert outbox[0].event_type == "share_asset"
        assert outbox[0].payload["asset_id"] == "asset-1"
        assert comm.mailbox.get_status(outbox[0].event_id) == STATUS_PENDING_DELIVERY

    def test_share_asset_returns_correct_sync_result(self, tmp_path: Path):
        """返回 SyncResult 正确反映投递结果。"""
        # 在线 — success=True, delivered_count=1
        transport_online = StubTransport(reachable_peers={"bob"})
        comm_online, _, _ = _make_comm(tmp_path, transport=transport_online)
        result_online = comm_online.share_asset("a1", "bob")
        assert result_online.success is True
        assert result_online.delivered_count == 1
        assert result_online.pending_count == 0

        # 离线 — success=False, pending_count=1
        transport_offline = StubTransport(reachable_peers=set())
        comm_offline, _, _ = _make_comm(
            tmp_path / "offline", transport=transport_offline
        )
        result_offline = comm_offline.share_asset("a2", "bob")
        assert result_offline.success is False
        assert result_offline.delivered_count == 0
        assert result_offline.pending_count == 1


# ---------------------------------------------------------------------------
# TestPeerCommListPeers
# ---------------------------------------------------------------------------


class TestPeerCommListPeers:
    """list_peers 列出已知 peer。"""

    def test_list_peers_returns_peer_ids(self, tmp_path: Path):
        """返回 discover_peers 的 peer_id 列表。"""
        transport = StubTransport(reachable_peers={"bob", "carol", "dave"})
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        peers = comm.list_peers()

        assert sorted(peers) == ["bob", "carol", "dave"]

    def test_list_peers_empty(self, tmp_path: Path):
        """无 peer 时返回空列表。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _ = _make_comm(tmp_path, transport=transport)

        assert comm.list_peers() == []
