"""Task 17 测试：ShadowComm 影子联络模块。

覆盖 ask_peer 流程 / 无快照处理 / 快照过期检测 / answer_generator 注入 /
事件字段标记等场景。使用 tmp_path fixture 做测试隔离。
"""

from __future__ import annotations

from pathlib import Path

from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_SIMULATED_ANSWER,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.shadow_comm import (
    ShadowComm,
    default_answer_generator,
)
from server.async_comm.types import PeerSnapshot, VectorClock


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_shadow_comm(
    tmp_path: Path,
    *,
    member_id: str = "alice",
    answer_generator=None,
    ttl_days: int = 30,
) -> tuple[ShadowComm, Mailbox, PeerSnapshotManager, ConversationLog]:
    """构造 ShadowComm 测试实例及其依赖。

    返回 (shadow_comm, mailbox, peer_snapshot_manager, conversation_log)，
    所有依赖共享同一 tmp_path 下的子目录，保证测试隔离。
    """
    base_dir = tmp_path / "async_comm"
    mailbox = Mailbox(base_dir, member_id)
    peer_snapshot_manager = PeerSnapshotManager(base_dir, ttl_days=ttl_days)
    conversation_log = ConversationLog(base_dir / "conversation.jsonl")
    shadow_comm = ShadowComm(
        mailbox=mailbox,
        peer_snapshot_manager=peer_snapshot_manager,
        conversation_log=conversation_log,
        member_id=member_id,
        answer_generator=answer_generator,
        snapshot_ttl_days=ttl_days,
    )
    return shadow_comm, mailbox, peer_snapshot_manager, conversation_log


# ---------------------------------------------------------------------------
# TestShadowCommAskPeer
# ---------------------------------------------------------------------------


class TestShadowCommAskPeer:
    """ask_peer 基本流程：有快照时生成 simulated_answer 事件。"""

    def test_returns_simulated_answer_event(self, tmp_path: Path):
        """ask_peer 返回 simulated_answer 类型事件。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "如何配置 lint?")
        assert event.event_type == EVENT_SIMULATED_ANSWER

    def test_simulated_answer_degraded_true(self, tmp_path: Path):
        """有快照时生成的 simulated_answer 事件 degraded=True。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "如何配置 lint?")
        assert event.degraded is True

    def test_based_on_set_to_snapshot_version(self, tmp_path: Path):
        """based_on 设为快照版本号。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")  # v1
        mgr.refresh_snapshot("bob")  # v2
        event = sc.ask_peer("bob", "如何配置 lint?")
        assert event.based_on == "v2"

    def test_ask_and_simulated_answer_written_to_log(self, tmp_path: Path):
        """ask 和 simulated_answer 事件都写入 ConversationLog。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "如何配置 lint?")
        events = log.load_by_peer("bob")
        assert len(events) == 2
        types = {e.event_type for e in events}
        assert types == {EVENT_ASK, EVENT_SIMULATED_ANSWER}

    def test_ask_event_written_to_outbox_pending(self, tmp_path: Path):
        """ask 事件写入 outbox，状态为 pending_delivery。"""
        sc, mb, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "如何配置 lint?")
        outbox = mb.load_outbox(status=STATUS_PENDING_DELIVERY)
        assert len(outbox) == 1
        assert outbox[0].event_type == EVENT_ASK

    def test_in_reply_to_links_simulated_to_ask(self, tmp_path: Path):
        """simulated_answer.in_reply_to == ask.event_id。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "如何配置 lint?")
        events = log.load_by_peer("bob")
        ask_events = [e for e in events if e.event_type == EVENT_ASK]
        answer_events = [e for e in events if e.event_type == EVENT_SIMULATED_ANSWER]
        assert len(ask_events) == 1
        assert len(answer_events) == 1
        assert answer_events[0].in_reply_to == ask_events[0].event_id


# ---------------------------------------------------------------------------
# TestShadowCommNoSnapshot
# ---------------------------------------------------------------------------


class TestShadowCommNoSnapshot:
    """无快照时仍生成 simulated_answer 事件，但标记异常。"""

    def test_returns_simulated_answer_when_no_snapshot(self, tmp_path: Path):
        """无快照时仍生成 simulated_answer 事件。"""
        sc, _, _, _ = _make_shadow_comm(tmp_path)
        event = sc.ask_peer("ghost", "问题?")
        assert event.event_type == EVENT_SIMULATED_ANSWER

    def test_based_on_empty_when_no_snapshot(self, tmp_path: Path):
        """无快照时 based_on 为空字符串。"""
        sc, _, _, _ = _make_shadow_comm(tmp_path)
        event = sc.ask_peer("ghost", "问题?")
        assert event.based_on == ""

    def test_snapshot_stale_true_when_no_snapshot(self, tmp_path: Path):
        """无快照时 snapshot_stale=True。"""
        sc, _, _, _ = _make_shadow_comm(tmp_path)
        event = sc.ask_peer("ghost", "问题?")
        assert event.snapshot_stale is True

    def test_payload_contains_warning_when_no_snapshot(self, tmp_path: Path):
        """无快照时 payload 含 warning 说明。"""
        sc, _, _, _ = _make_shadow_comm(tmp_path)
        event = sc.ask_peer("ghost", "问题?")
        assert "warning" in event.payload
        assert "no local snapshot" in event.payload["warning"]

    def test_degraded_true_when_no_snapshot(self, tmp_path: Path):
        """无快照时 degraded 仍为 True（影子联络标记）。"""
        sc, _, _, _ = _make_shadow_comm(tmp_path)
        event = sc.ask_peer("ghost", "问题?")
        assert event.degraded is True


# ---------------------------------------------------------------------------
# TestShadowCommStaleSnapshot
# ---------------------------------------------------------------------------


class TestShadowCommStaleSnapshot:
    """快照过期检测：snapshot_stale 标记。"""

    def test_stale_snapshot_marks_snapshot_stale_true(self, tmp_path: Path):
        """快照过期时（is_stale=True）snapshot_stale=True。"""
        # ttl_days=0 使新快照立即过期
        sc, _, mgr, _ = _make_shadow_comm(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        assert mgr.is_stale("bob") is True
        event = sc.ask_peer("bob", "问题?")
        assert event.snapshot_stale is True

    def test_fresh_snapshot_marks_snapshot_stale_false(self, tmp_path: Path):
        """快照未过期时 snapshot_stale=False。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")
        assert mgr.is_stale("bob") is False
        event = sc.ask_peer("bob", "问题?")
        assert event.snapshot_stale is False

    def test_stale_snapshot_still_generates_answer(self, tmp_path: Path):
        """快照过期仍生成回答（但标记 stale）。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "问题?")
        # 仍生成回答
        assert event.event_type == EVENT_SIMULATED_ANSWER
        assert "answer" in event.payload
        assert event.payload["answer"] != ""
        # 标记 stale
        assert event.snapshot_stale is True

    def test_stale_snapshot_based_on_version(self, tmp_path: Path):
        """快照过期时 based_on 仍设为快照版本号。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "问题?")
        assert event.based_on == "v1"


# ---------------------------------------------------------------------------
# TestShadowCommAnswerGenerator
# ---------------------------------------------------------------------------


class TestShadowCommAnswerGenerator:
    """answer_generator 注入与默认生成器。"""

    def test_custom_answer_generator_called(self, tmp_path: Path):
        """自定义 answer_generator 被调用。"""
        called_args: list = []

        def custom_gen(question: str, snapshot: PeerSnapshot) -> str:
            called_args.append((question, snapshot))
            return "自定义回答"

        sc, _, mgr, _ = _make_shadow_comm(tmp_path, answer_generator=custom_gen)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "如何配置 lint?")
        assert len(called_args) == 1
        assert event.payload["answer"] == "自定义回答"

    def test_default_answer_generator_returns_placeholder(self, tmp_path: Path):
        """默认 answer_generator 返回占位文本。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "如何配置 lint?")
        answer = event.payload["answer"]
        assert "影子联络模拟回答" in answer
        assert "如何配置 lint?" in answer

    def test_answer_generator_receives_correct_question(self, tmp_path: Path):
        """answer_generator 接收正确的 question 参数。"""
        received_question: list[str] = []

        def gen(question: str, snapshot: PeerSnapshot) -> str:
            received_question.append(question)
            return "回答"

        sc, _, mgr, _ = _make_shadow_comm(tmp_path, answer_generator=gen)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "我的问题内容")
        assert received_question == ["我的问题内容"]

    def test_answer_generator_receives_correct_snapshot(self, tmp_path: Path):
        """answer_generator 接收正确的 snapshot 参数（含版本号）。"""
        received_snapshot: list[PeerSnapshot] = []

        def gen(question: str, snapshot: PeerSnapshot) -> str:
            received_snapshot.append(snapshot)
            return "回答"

        sc, _, mgr, _ = _make_shadow_comm(tmp_path, answer_generator=gen)
        mgr.refresh_snapshot("bob")
        mgr.refresh_snapshot("bob")  # v2
        sc.ask_peer("bob", "问题?")
        assert len(received_snapshot) == 1
        assert received_snapshot[0].peer_id == "bob"
        assert received_snapshot[0].snapshot_version == "v2"

    def test_default_answer_generator_directly(self, tmp_path: Path):
        """直接调用 default_answer_generator 返回含快照版本的占位文本。"""
        snapshot = PeerSnapshot(peer_id="bob", snapshot_version="v38")
        answer = default_answer_generator("问题?", snapshot)
        assert "v38" in answer
        assert "问题?" in answer


# ---------------------------------------------------------------------------
# TestShadowCommEventFields
# ---------------------------------------------------------------------------


class TestShadowCommEventFields:
    """事件字段标记：degraded / realtime / payload。"""

    def test_simulated_answer_degraded_true(self, tmp_path: Path):
        """simulated_answer 事件 degraded=True。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "问题?")
        assert event.degraded is True

    def test_simulated_answer_realtime_false(self, tmp_path: Path):
        """simulated_answer 事件 realtime=False。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "问题?")
        assert event.realtime is False

    def test_ask_event_degraded_false(self, tmp_path: Path):
        """ask 事件 degraded=False。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "问题?")
        ask_events = [
            e for e in log.load_by_type(EVENT_ASK) if e.peer_id == "bob"
        ]
        assert len(ask_events) == 1
        assert ask_events[0].degraded is False

    def test_ask_event_realtime_false(self, tmp_path: Path):
        """ask 事件 realtime=False。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "问题?")
        ask_events = [
            e for e in log.load_by_type(EVENT_ASK) if e.peer_id == "bob"
        ]
        assert len(ask_events) == 1
        assert ask_events[0].realtime is False

    def test_ask_payload_contains_question(self, tmp_path: Path):
        """ask 事件 payload 包含 question 字段。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "如何配置 lint?")
        ask_events = [
            e for e in log.load_by_type(EVENT_ASK) if e.peer_id == "bob"
        ]
        assert len(ask_events) == 1
        assert "question" in ask_events[0].payload
        assert ask_events[0].payload["question"] == "如何配置 lint?"

    def test_simulated_answer_payload_contains_answer(self, tmp_path: Path):
        """simulated_answer 事件 payload 包含 answer 字段。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "如何配置 lint?")
        assert "answer" in event.payload
        assert isinstance(event.payload["answer"], str)
        assert event.payload["answer"] != ""

    def test_simulated_answer_event_type(self, tmp_path: Path):
        """simulated_answer 事件 event_type 正确。"""
        sc, _, mgr, _ = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        event = sc.ask_peer("bob", "问题?")
        assert event.event_type == EVENT_SIMULATED_ANSWER

    def test_ask_event_type(self, tmp_path: Path):
        """ask 事件 event_type 正确。"""
        sc, _, mgr, log = _make_shadow_comm(tmp_path)
        mgr.refresh_snapshot("bob")
        sc.ask_peer("bob", "问题?")
        ask_events = [
            e for e in log.load_by_type(EVENT_ASK) if e.peer_id == "bob"
        ]
        assert len(ask_events) == 1
        assert ask_events[0].event_type == EVENT_ASK
