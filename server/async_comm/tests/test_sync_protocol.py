"""Task 18 测试：上线同步协议 SyncProtocol。

覆盖：
- _push_outbox：推送 pending_delivery 消息给 peer / 状态更新为 delivered /
  无待投递返回 0 / 只推送目标 peer（过滤其他 peer）
- _pull_inbox：拉取消息写入 inbox / 拉取的消息写入 ConversationLog / 无消息返回 0
- _reconcile_simulated_answers：simulated 与 realtime 匹配调用 conflict_resolver /
  confirmed/revised/needs_human_review 三种决策创建对应事件 /
  conflict_resolver 为 None 跳过 / 无匹配 realtime 跳过
- sync_with_peer：完整同步流程 / 计数正确 / 幂等性

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_REVISED,
    EVENT_SIMULATED_ANSWER,
    STATUS_DELIVERED,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.sync_protocol import (
    SyncProtocol,
    SyncProtocolResult,
)
from server.async_comm.types import ConversationEvent, VectorClock
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - ``reachable_peers``：可达 peer 集合
    - ``fetch_responses``：按 peer_id 预置 fetch 返回消息
    - ``deliver_success``：deliver 是否返回 success=True
    - ``delivered_messages``：记录 deliver 调用历史
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        fetch_responses: dict[str, list[Message]] | None = None,
        deliver_success: bool = True,
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.fetch_responses = fetch_responses or {}
        self.deliver_success = deliver_success
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.fetch_calls: list[str] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        if self.deliver_success:
            return SyncResult(
                success=True,
                delivered_count=len(messages),
                delivered_message_ids=[m.event_id for m in messages],
            )
        return SyncResult(success=False, failed_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        self.fetch_calls.append(peer_id)
        return self.fetch_responses.get(peer_id, [])

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


class StubConflictResolver:
    """Stub 实现 ConflictResolverProtocol，可控的测试冲突解决器。

    扩展点：
    - ``default_decision``：默认返回的决策（confirmed/revised/needs_human_review）
    - ``default_note``：默认返回的说明
    - ``decisions``：按 event_id 预置决策（覆盖 default_decision）
    - ``resolve_calls``：记录 resolve 调用历史
    """

    def __init__(
        self,
        *,
        default_decision: str = "confirmed",
        default_note: str = "匹配",
        decisions: dict[str, str] | None = None,
    ) -> None:
        self.default_decision = default_decision
        self.default_note = default_note
        self.decisions = decisions or {}
        self.resolve_calls: list[tuple[str, str, str]] = []

    def resolve(
        self,
        *,
        simulated_answer: str,
        real_answer: str,
        event_id: str = "",
    ) -> tuple[str, str]:
        self.resolve_calls.append((simulated_answer, real_answer, event_id))
        decision = self.decisions.get(event_id, self.default_decision)
        return (decision, self.default_note)


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_sync_protocol(
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    conflict_resolver: StubConflictResolver | None = None,
    member_id: str = "alice",
) -> tuple[SyncProtocol, StubTransport, Mailbox, ConversationLog, PeerSnapshotManager]:
    """构造 SyncProtocol 测试实例及其依赖。

    返回 (protocol, transport, mailbox, conversation_log, peer_snapshot_manager)，
    所有依赖共享同一 tmp_path 下的子目录，保证测试隔离。
    """
    base_dir = tmp_path / "async_comm"
    transport = transport or StubTransport()
    mailbox = Mailbox(base_dir, member_id)
    conversation_log = ConversationLog(base_dir / "conversation.jsonl")
    peer_snapshot_manager = PeerSnapshotManager(base_dir)
    protocol = SyncProtocol(
        transport=transport,
        mailbox=mailbox,
        conversation_log=conversation_log,
        peer_snapshot_manager=peer_snapshot_manager,
        member_id=member_id,
        conflict_resolver=conflict_resolver,
    )
    return protocol, transport, mailbox, conversation_log, peer_snapshot_manager


def _make_event(
    *,
    event_id: str = "",
    event_type: str = EVENT_ASK,
    peer_id: str = "bob",
    payload: dict | None = None,
    in_reply_to: str = "",
    degraded: bool = False,
    realtime: bool = False,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例（自动生成 event_id 与时间戳）。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        vector_clock=VectorClock(),
        payload=payload or {},
        in_reply_to=in_reply_to,
        degraded=degraded,
        realtime=realtime,
    )


def _make_message(
    *,
    event_id: str = "",
    sender_id: str = "bob",
    recipient_id: str = "alice",
    msg_type: str = "answer",
    payload: dict | None = None,
    in_reply_to: str = "",
) -> Message:
    """构造 Message 测试实例（自动生成 event_id 与时间戳）。"""
    return Message(
        message_id=event_id or str(uuid.uuid4()),
        event_id=event_id or str(uuid.uuid4()),
        sender_id=sender_id,
        recipient_id=recipient_id,
        msg_type=msg_type,
        payload=payload or {},
        timestamp=datetime.now(timezone.utc).isoformat(),
        in_reply_to=in_reply_to,
    )


# ---------------------------------------------------------------------------
# TestSyncProtocolPushOutbox
# ---------------------------------------------------------------------------


class TestSyncProtocolPushOutbox:
    """_push_outbox 推送 outbox 消息。"""

    def test_push_pending_delivery_messages_to_peer(self, tmp_path: Path):
        """推送 pending_delivery 消息给 peer。"""
        protocol, transport, mailbox, _, _ = _make_sync_protocol(tmp_path)

        # 在 outbox 中放置 2 条 pending_delivery 消息给 bob
        event1 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        event2 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        mailbox.append_outbox(event1)
        mailbox.append_outbox(event2)

        count = protocol._push_outbox("bob")

        assert count == 2
        assert len(transport.delivered_messages) == 1
        peer_id, messages = transport.delivered_messages[0]
        assert peer_id == "bob"
        assert len(messages) == 2

    def test_push_updates_status_to_delivered(self, tmp_path: Path):
        """投递后状态更新为 delivered。"""
        protocol, transport, mailbox, _, _ = _make_sync_protocol(tmp_path)

        event = _make_event(event_type=EVENT_ASK, peer_id="bob")
        mailbox.append_outbox(event)
        assert mailbox.get_status(event.event_id) == STATUS_PENDING_DELIVERY

        protocol._push_outbox("bob")

        assert mailbox.get_status(event.event_id) == STATUS_DELIVERED

    def test_push_returns_zero_when_no_pending(self, tmp_path: Path):
        """无待投递消息时返回 0。"""
        protocol, transport, _, _, _ = _make_sync_protocol(tmp_path)

        count = protocol._push_outbox("bob")

        assert count == 0
        assert len(transport.delivered_messages) == 0

    def test_push_filters_other_peers(self, tmp_path: Path):
        """只推送目标 peer 的消息（过滤其他 peer）。"""
        protocol, transport, mailbox, _, _ = _make_sync_protocol(tmp_path)

        # bob 与 carol 各一条 pending_delivery
        bob_event = _make_event(event_type=EVENT_ASK, peer_id="bob")
        carol_event = _make_event(event_type=EVENT_ASK, peer_id="carol")
        mailbox.append_outbox(bob_event)
        mailbox.append_outbox(carol_event)

        count = protocol._push_outbox("bob")

        assert count == 1
        assert len(transport.delivered_messages) == 1
        peer_id, messages = transport.delivered_messages[0]
        assert peer_id == "bob"
        assert len(messages) == 1
        assert messages[0].event_id == bob_event.event_id
        # carol 的消息状态应保持 pending_delivery
        assert mailbox.get_status(carol_event.event_id) == STATUS_PENDING_DELIVERY

    def test_push_skips_delivered_messages(self, tmp_path: Path):
        """已 delivered 的消息不再推送（只推送 pending_delivery）。"""
        protocol, transport, mailbox, _, _ = _make_sync_protocol(tmp_path)

        pending = _make_event(event_type=EVENT_ASK, peer_id="bob")
        delivered = _make_event(event_type=EVENT_ASK, peer_id="bob")
        mailbox.append_outbox(pending)
        mailbox.append_outbox(delivered)
        # 手动将 delivered 标记为已投递
        mailbox.update_status(delivered.event_id, STATUS_DELIVERED)

        count = protocol._push_outbox("bob")

        assert count == 1
        _, messages = transport.delivered_messages[0]
        assert len(messages) == 1
        assert messages[0].event_id == pending.event_id


# ---------------------------------------------------------------------------
# TestSyncProtocolPullInbox
# ---------------------------------------------------------------------------


class TestSyncProtocolPullInbox:
    """_pull_inbox 拉取消息写入 inbox。"""

    def test_pull_writes_messages_to_inbox(self, tmp_path: Path):
        """拉取消息写入 inbox。"""
        msg1 = _make_message(
            event_id="msg-1",
            sender_id="bob",
            msg_type="answer",
            payload={"answer": "答案1"},
        )
        msg2 = _make_message(
            event_id="msg-2",
            sender_id="bob",
            msg_type="answer",
            payload={"answer": "答案2"},
        )
        transport = StubTransport(fetch_responses={"bob": [msg1, msg2]})
        protocol, _, mailbox, _, _ = _make_sync_protocol(tmp_path, transport=transport)

        count = protocol._pull_inbox("bob")

        assert count == 2
        inbox = mailbox.load_inbox()
        assert len(inbox) == 2
        event_ids = {e.event_id for e in inbox}
        assert event_ids == {"msg-1", "msg-2"}

    def test_pull_writes_messages_to_conversation_log(self, tmp_path: Path):
        """拉取的消息写入 ConversationLog。"""
        msg = _make_message(
            event_id="msg-1",
            sender_id="bob",
            msg_type="answer",
            payload={"answer": "答案"},
            in_reply_to="ask-1",
        )
        transport = StubTransport(fetch_responses={"bob": [msg]})
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport
        )

        protocol._pull_inbox("bob")

        # ConversationLog 中应有该事件
        events = conversation_log.load_by_peer("bob")
        assert len(events) == 1
        assert events[0].event_id == "msg-1"
        # msg_type="answer" 应映射为 EVENT_REALTIME_ANSWER
        assert events[0].event_type == EVENT_REALTIME_ANSWER
        assert events[0].in_reply_to == "ask-1"
        assert events[0].realtime is True
        assert events[0].peer_id == "bob"

    def test_pull_returns_zero_when_no_messages(self, tmp_path: Path):
        """无消息时返回 0。"""
        transport = StubTransport(fetch_responses={"bob": []})
        protocol, _, _, _, _ = _make_sync_protocol(tmp_path, transport=transport)

        count = protocol._pull_inbox("bob")

        assert count == 0

    def test_pull_idempotent_on_duplicate_event_id(self, tmp_path: Path):
        """相同 event_id 重复拉取幂等（mailbox 与 log 都去重）。"""
        msg = _make_message(
            event_id="dup-1",
            sender_id="bob",
            msg_type="answer",
            payload={"answer": "答案"},
        )
        transport = StubTransport(fetch_responses={"bob": [msg]})
        protocol, _, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport
        )

        # 第一次拉取
        count1 = protocol._pull_inbox("bob")
        assert count1 == 1

        # 第二次拉取（transport 仍返回同一条）
        count2 = protocol._pull_inbox("bob")

        # 计数仍为 1（按拉取的消息数计数），但 inbox 与 log 不重复
        assert count2 == 1
        assert len(mailbox.load_inbox()) == 1
        assert conversation_log.count(event_type=EVENT_REALTIME_ANSWER) == 1


# ---------------------------------------------------------------------------
# TestSyncProtocolReconcile
# ---------------------------------------------------------------------------


class TestSyncProtocolReconcile:
    """_reconcile_simulated_answers 对账逻辑。"""

    def _setup_reconcile(
        self,
        tmp_path: Path,
        *,
        resolver: StubConflictResolver | None = None,
    ) -> tuple[SyncProtocol, ConversationLog]:
        """构造对账场景：1 个 ask + 1 个 simulated_answer + 1 个 realtime_answer。

        ask_id 是 ask 事件 event_id。
        simulated_answer.in_reply_to = ask_id
        realtime_answer.in_reply_to = ask_id（与 simulated 关联同一 ask）
        """
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=resolver
        )

        # ask 事件（本地发出）
        ask_event = _make_event(event_type=EVENT_ASK, peer_id="bob")
        # simulated_answer 事件（本地影子生成，in_reply_to 指向 ask）
        sim_event = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟答案"},
            in_reply_to=ask_event.event_id,
            degraded=True,
        )
        # realtime_answer 事件（对方发来，in_reply_to 也指向 ask）
        real_event = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实答案"},
            in_reply_to=ask_event.event_id,
            realtime=True,
        )
        conversation_log.append(ask_event)
        conversation_log.append(sim_event)
        conversation_log.append(real_event)
        return protocol, conversation_log

    def test_reconcile_calls_conflict_resolver_when_matched(self, tmp_path: Path):
        """simulated_answer 与 realtime_answer 匹配时调用 conflict_resolver。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, _ = self._setup_reconcile(tmp_path, resolver=resolver)

        protocol._reconcile_simulated_answers("bob")

        assert len(resolver.resolve_calls) == 1
        sim, real, event_id = resolver.resolve_calls[0]
        assert sim == "模拟答案"
        assert real == "真实答案"
        assert event_id != ""

    def test_reconcile_confirmed_creates_confirmed_event(self, tmp_path: Path):
        """confirmed 决策创建 confirmed 事件。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, conversation_log = self._setup_reconcile(tmp_path, resolver=resolver)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 1
        assert revised == 0
        assert needs_review == 0
        # 日志中应有 confirmed 事件
        confirmed_events = [
            e for e in conversation_log.load_by_type(EVENT_CONFIRMED)
            if e.peer_id == "bob"
        ]
        assert len(confirmed_events) == 1
        assert confirmed_events[0].payload.get("decision") == "confirmed"

    def test_reconcile_revised_creates_revised_event(self, tmp_path: Path):
        """revised 决策创建 revised 事件。"""
        resolver = StubConflictResolver(default_decision="revised")
        protocol, conversation_log = self._setup_reconcile(tmp_path, resolver=resolver)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 0
        assert revised == 1
        assert needs_review == 0
        revised_events = [
            e for e in conversation_log.load_by_type(EVENT_REVISED)
            if e.peer_id == "bob"
        ]
        assert len(revised_events) == 1
        assert revised_events[0].payload.get("decision") == "revised"

    def test_reconcile_needs_human_review_creates_event(self, tmp_path: Path):
        """needs_human_review 决策创建 needs_human_review 事件。"""
        resolver = StubConflictResolver(default_decision="needs_human_review")
        protocol, conversation_log = self._setup_reconcile(tmp_path, resolver=resolver)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 0
        assert revised == 0
        assert needs_review == 1
        review_events = [
            e for e in conversation_log.load_by_type(EVENT_NEEDS_HUMAN_REVIEW)
            if e.peer_id == "bob"
        ]
        assert len(review_events) == 1
        assert review_events[0].payload.get("decision") == "needs_human_review"

    def test_reconcile_skipped_when_resolver_none(self, tmp_path: Path):
        """conflict_resolver 为 None 时跳过对账。"""
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=None
        )

        # 构造 simulated + realtime 匹配场景
        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
        )
        real = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实"},
            in_reply_to=ask.event_id,
        )
        conversation_log.append(ask)
        conversation_log.append(sim)
        conversation_log.append(real)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 0
        assert revised == 0
        assert needs_review == 0
        # 不应创建任何对账结果事件
        assert conversation_log.count(event_type=EVENT_CONFIRMED) == 0
        assert conversation_log.count(event_type=EVENT_REVISED) == 0
        assert conversation_log.count(event_type=EVENT_NEEDS_HUMAN_REVIEW) == 0

    def test_reconcile_skipped_when_no_matching_realtime(self, tmp_path: Path):
        """无匹配的 realtime_answer 时跳过（不调用 resolver）。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=resolver
        )

        # ask + simulated（无匹配的 realtime_answer）
        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
        )
        conversation_log.append(ask)
        conversation_log.append(sim)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 0
        assert revised == 0
        assert needs_review == 0
        assert len(resolver.resolve_calls) == 0

    def test_reconcile_skipped_when_simulated_no_in_reply_to(self, tmp_path: Path):
        """simulated_answer 无 in_reply_to 时跳过（无法关联 ask）。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=resolver
        )

        # simulated_answer 无 in_reply_to
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to="",  # 无回复链
        )
        conversation_log.append(sim)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 0
        assert revised == 0
        assert needs_review == 0
        assert len(resolver.resolve_calls) == 0

    def test_reconcile_multiple_simulated_events(self, tmp_path: Path):
        """多个 simulated_answer 各自匹配对应的 realtime_answer。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=resolver
        )

        # 两对独立的 ask + simulated + realtime
        ask1 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim1 = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟1"},
            in_reply_to=ask1.event_id,
        )
        real1 = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实1"},
            in_reply_to=ask1.event_id,
        )
        ask2 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim2 = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟2"},
            in_reply_to=ask2.event_id,
        )
        real2 = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实2"},
            in_reply_to=ask2.event_id,
        )
        for e in [ask1, sim1, real1, ask2, sim2, real2]:
            conversation_log.append(e)

        confirmed, revised, needs_review = protocol._reconcile_simulated_answers("bob")

        assert confirmed == 2
        assert len(resolver.resolve_calls) == 2

    def test_reconcile_resolution_event_links_to_simulated(self, tmp_path: Path):
        """对账结果事件 in_reply_to 指向 simulated_answer 事件 ID。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        protocol, _, _, conversation_log, _ = _make_sync_protocol(
            tmp_path, conflict_resolver=resolver
        )

        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
        )
        real = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实"},
            in_reply_to=ask.event_id,
        )
        conversation_log.append(ask)
        conversation_log.append(sim)
        conversation_log.append(real)

        protocol._reconcile_simulated_answers("bob")

        confirmed_events = [
            e for e in conversation_log.load_by_type(EVENT_CONFIRMED)
            if e.peer_id == "bob"
        ]
        assert len(confirmed_events) == 1
        # 对账事件 in_reply_to 应指向 simulated_answer 事件
        assert confirmed_events[0].in_reply_to == sim.event_id


# ---------------------------------------------------------------------------
# TestSyncProtocolSyncWithPeer
# ---------------------------------------------------------------------------


class TestSyncProtocolSyncWithPeer:
    """sync_with_peer 完整同步流程。"""

    def test_full_sync_push_pull_reconcile(self, tmp_path: Path):
        """完整同步流程：push + pull + reconcile。"""
        # 准备：outbox 1 条 pending_delivery + fetch 1 条 realtime_answer + 模拟场景
        resolver = StubConflictResolver(default_decision="confirmed")
        transport = StubTransport(reachable_peers={"bob"})
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport, conflict_resolver=resolver
        )

        # outbox 中放 1 条 ask 给 bob（pending_delivery）
        ask = _make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "Q?"})
        mailbox.append_outbox(ask)

        # 同时在 conversation_log 中构造 simulated_answer（in_reply_to 指向 ask）
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
            degraded=True,
        )
        conversation_log.append(ask)
        conversation_log.append(sim)

        # transport.fetch 返回匹配的 realtime_answer（in_reply_to 指向同一 ask）
        real_msg = _make_message(
            event_id="real-1",
            sender_id="bob",
            recipient_id="alice",
            msg_type="answer",
            payload={"answer": "真实"},
            in_reply_to=ask.event_id,
        )
        transport.fetch_responses["bob"] = [real_msg]

        result = protocol.sync_with_peer("bob")

        assert isinstance(result, SyncProtocolResult)
        assert result.peer_id == "bob"
        assert result.pushed_count == 1
        assert result.received_count == 1
        assert result.confirmed_count == 1
        assert result.revised_count == 0
        assert result.needs_review_count == 0
        assert result.errors == []

    def test_sync_returns_correct_result_counts(self, tmp_path: Path):
        """返回 SyncProtocolResult 各计数正确（多消息场景）。"""
        resolver = StubConflictResolver(default_decision="revised")
        transport = StubTransport(reachable_peers={"bob"})
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport, conflict_resolver=resolver
        )

        # outbox 放 2 条 pending_delivery 给 bob
        ask1 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        ask2 = _make_event(event_type=EVENT_ASK, peer_id="bob")
        mailbox.append_outbox(ask1)
        mailbox.append_outbox(ask2)

        # conversation_log 中构造 2 对 simulated + realtime
        sim1 = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "s1"},
            in_reply_to=ask1.event_id,
        )
        real1 = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "r1"},
            in_reply_to=ask1.event_id,
        )
        sim2 = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "s2"},
            in_reply_to=ask2.event_id,
        )
        real2 = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "r2"},
            in_reply_to=ask2.event_id,
        )
        for e in [ask1, sim1, real1, ask2, sim2, real2]:
            conversation_log.append(e)

        result = protocol.sync_with_peer("bob")

        assert result.pushed_count == 2
        assert result.received_count == 0  # fetch 无消息
        assert result.confirmed_count == 0
        assert result.revised_count == 2
        assert result.needs_review_count == 0

    def test_sync_idempotent_no_side_effects(self, tmp_path: Path):
        """幂等性：重复同步不产生副作用。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        transport = StubTransport(reachable_peers={"bob"})
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport, conflict_resolver=resolver
        )

        # 构造场景：1 条 outbox pending + 1 对 simulated/realtime
        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
        )
        real = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实"},
            in_reply_to=ask.event_id,
        )
        mailbox.append_outbox(ask)
        conversation_log.append(ask)
        conversation_log.append(sim)
        conversation_log.append(real)

        # 第一次同步
        result1 = protocol.sync_with_peer("bob")
        assert result1.pushed_count == 1
        assert result1.confirmed_count == 1

        # 第二次同步
        result2 = protocol.sync_with_peer("bob")

        # outbox 中 ask 已 delivered，第二次推送应为 0
        assert result2.pushed_count == 0
        # simulated_answer 仍在日志中，对账会再次匹配并创建新的 confirmed 事件
        # （这是预期行为：对账不修改 simulated_answer 本身）
        # 但 mailbox 中 ask 状态不变（已 delivered）
        assert mailbox.get_status(ask.event_id) == STATUS_DELIVERED
        # 第二次仍会创建 1 个 confirmed 事件（resolver 返回 confirmed）
        assert result2.confirmed_count == 1

    def test_sync_idempotent_inbox_no_duplicates(self, tmp_path: Path):
        """幂等性：重复拉取相同 event_id 的消息不产生重复。"""
        transport = StubTransport(reachable_peers={"bob"})
        msg = _make_message(
            event_id="msg-1",
            sender_id="bob",
            msg_type="answer",
            payload={"answer": "答案"},
        )
        transport.fetch_responses["bob"] = [msg]
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport
        )

        # 第一次同步
        protocol.sync_with_peer("bob")
        inbox_count_1 = len(mailbox.load_inbox())
        log_count_1 = conversation_log.count(event_type=EVENT_REALTIME_ANSWER)

        # 第二次同步（fetch 仍返回同一条）
        protocol.sync_with_peer("bob")
        inbox_count_2 = len(mailbox.load_inbox())
        log_count_2 = conversation_log.count(event_type=EVENT_REALTIME_ANSWER)

        # inbox 与 log 都基于 event_id 幂等去重，不产生重复
        assert inbox_count_1 == inbox_count_2 == 1
        assert log_count_1 == log_count_2 == 1

    def test_sync_collects_errors_on_stage_failure(self, tmp_path: Path):
        """单阶段失败收集到 result.errors，不阻断其他阶段。"""
        resolver = StubConflictResolver(default_decision="confirmed")
        transport_arg = StubTransport(reachable_peers={"bob"})
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport_arg, conflict_resolver=resolver
        )

        # outbox 中放 1 条 pending_delivery 消息，确保 _push_outbox 会调用 deliver
        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        mailbox.append_outbox(ask)

        # 让 deliver 抛异常模拟 push 阶段失败
        def boom_deliver(peer_id: str, messages: list[Message]) -> SyncResult:
            raise RuntimeError("deliver boom")
        transport.deliver = boom_deliver  # type: ignore[method-assign]

        # pull 阶段仍正常（fetch 返回空）
        result = protocol.sync_with_peer("bob")

        # push 失败收集到 errors
        assert any("push_outbox" in err for err in result.errors)
        # pull 仍执行（received_count=0）
        assert result.received_count == 0

    def test_sync_with_no_resolver_skips_reconcile(self, tmp_path: Path):
        """无 conflict_resolver 时跳过对账（计数都为 0）。"""
        transport = StubTransport(reachable_peers={"bob"})
        protocol, transport, mailbox, conversation_log, _ = _make_sync_protocol(
            tmp_path, transport=transport, conflict_resolver=None
        )

        ask = _make_event(event_type=EVENT_ASK, peer_id="bob")
        sim = _make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟"},
            in_reply_to=ask.event_id,
        )
        real = _make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "真实"},
            in_reply_to=ask.event_id,
        )
        mailbox.append_outbox(ask)
        conversation_log.append(ask)
        conversation_log.append(sim)
        conversation_log.append(real)

        result = protocol.sync_with_peer("bob")

        assert result.pushed_count == 1
        assert result.confirmed_count == 0
        assert result.revised_count == 0
        assert result.needs_review_count == 0
