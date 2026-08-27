"""Task 13 测试：Mailbox 信箱模块。

覆盖 append_outbox / append_inbox / 幂等去重 / load_outbox / load_inbox /
状态机流转（合法 + 非法）/ get_status / pending_delivery_count /
remove_delivered（原子重写）。

测试隔离：用 tmp_path fixture（pytest）为每个用例提供独立临时目录。
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import pytest

from server.async_comm.constants import (
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_PENDING_DELIVERY,
    STATUS_REVISED,
)
from server.async_comm.mailbox import Mailbox
from server.async_comm.types import ConversationEvent, VectorClock


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str | None = None,
    event_type: str = "ask",
    peer_id: str = "bob",
    timestamp: str = "2026-08-12T10:00:00Z",
    vector_clock: VectorClock | None = None,
    payload: dict | None = None,
    in_reply_to: str = "",
    degraded: bool = False,
    realtime: bool = False,
    based_on: str = "",
    snapshot_stale: bool = False,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=timestamp,
        vector_clock=vector_clock or VectorClock(),
        payload=payload if payload is not None else {},
        in_reply_to=in_reply_to,
        degraded=degraded,
        realtime=realtime,
        based_on=based_on,
        snapshot_stale=snapshot_stale,
    )


# ---------------------------------------------------------------------------
# TestMailboxAppend
# ---------------------------------------------------------------------------


class TestMailboxAppend:
    """append_outbox / append_inbox 基本写入与目录自动创建。"""

    def test_append_outbox_returns_event_id(self, tmp_path: Path):
        """append_outbox 返回传入的 event_id。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-1")
        result = mb.append_outbox(evt)
        assert result == "evt-1"

    def test_append_outbox_creates_files(self, tmp_path: Path):
        """append_outbox 后 outbox.jsonl 与 state.json 存在。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert (tmp_path / "alice" / "outbox.jsonl").is_file()
        assert (tmp_path / "alice" / "state.json").is_file()

    def test_append_inbox_returns_event_id(self, tmp_path: Path):
        """append_inbox 返回传入的 event_id。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-2")
        result = mb.append_inbox(evt)
        assert result == "evt-2"

    def test_append_inbox_creates_files(self, tmp_path: Path):
        """append_inbox 后 inbox.jsonl 与 state.json 存在。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_inbox(_make_event(event_id="evt-2"))
        assert (tmp_path / "alice" / "inbox.jsonl").is_file()
        assert (tmp_path / "alice" / "state.json").is_file()

    def test_init_creates_directory(self, tmp_path: Path):
        """__init__ 自动创建 peer 子目录。"""
        base = tmp_path / "async_comm"
        assert not base.exists()
        Mailbox(base, "alice")
        assert (base / "alice").is_dir()

    def test_init_nested_directory(self, tmp_path: Path):
        """__init__ 支持多层嵌套目录自动创建。"""
        base = tmp_path / "a" / "b" / "c"
        Mailbox(base, "alice")
        assert (base / "alice").is_dir()

    def test_append_outbox_initial_status(self, tmp_path: Path):
        """append_outbox 后初始状态为 pending_delivery。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.get_status("evt-1") == STATUS_PENDING_DELIVERY

    def test_append_inbox_initial_status(self, tmp_path: Path):
        """append_inbox 后初始状态为 delivered。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_inbox(_make_event(event_id="evt-2"))
        assert mb.get_status("evt-2") == STATUS_DELIVERED

    def test_append_outbox_preserves_event_data(self, tmp_path: Path):
        """append_outbox 后 load_outbox 能还原完整事件数据。"""
        mb = Mailbox(tmp_path, "alice")
        vc = VectorClock(counters={"alice": 1, "bob": 2})
        evt = _make_event(
            event_id="evt-1",
            event_type="simulated_answer",
            peer_id="bob",
            timestamp="2026-08-12T10:01:00Z",
            vector_clock=vc,
            payload={"answer": "lint 规则"},
            in_reply_to="evt-0",
            degraded=True,
            based_on="bob_v38",
            snapshot_stale=True,
        )
        mb.append_outbox(evt)
        loaded = mb.load_outbox()
        assert len(loaded) == 1
        assert loaded[0].event_id == "evt-1"
        assert loaded[0].event_type == "simulated_answer"
        assert loaded[0].peer_id == "bob"
        assert loaded[0].timestamp == "2026-08-12T10:01:00Z"
        assert loaded[0].vector_clock.counters == {"alice": 1, "bob": 2}
        assert loaded[0].payload == {"answer": "lint 规则"}
        assert loaded[0].in_reply_to == "evt-0"
        assert loaded[0].degraded is True
        assert loaded[0].based_on == "bob_v38"
        assert loaded[0].snapshot_stale is True


# ---------------------------------------------------------------------------
# TestMailboxIdempotency
# ---------------------------------------------------------------------------


class TestMailboxIdempotency:
    """相同 event_id 重复 append 不重复写入。"""

    def test_append_outbox_duplicate_idempotent(self, tmp_path: Path):
        """相同 event_id 重复 append_outbox，outbox 只有一行。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-1")
        mb.append_outbox(evt)
        mb.append_outbox(evt)
        loaded = mb.load_outbox()
        assert len(loaded) == 1
        assert loaded[0].event_id == "evt-1"

    def test_append_inbox_duplicate_idempotent(self, tmp_path: Path):
        """相同 event_id 重复 append_inbox，inbox 只有一行。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-2")
        mb.append_inbox(evt)
        mb.append_inbox(evt)
        loaded = mb.load_inbox()
        assert len(loaded) == 1
        assert loaded[0].event_id == "evt-2"

    def test_append_outbox_returns_same_id_on_duplicate(self, tmp_path: Path):
        """重复 append_outbox 仍返回相同 event_id。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-1")
        first = mb.append_outbox(evt)
        second = mb.append_outbox(evt)
        assert first == second == "evt-1"

    def test_append_inbox_returns_same_id_on_duplicate(self, tmp_path: Path):
        """重复 append_inbox 仍返回相同 event_id。"""
        mb = Mailbox(tmp_path, "alice")
        evt = _make_event(event_id="evt-2")
        first = mb.append_inbox(evt)
        second = mb.append_inbox(evt)
        assert first == second == "evt-2"

    def test_append_outbox_different_ids_all_written(self, tmp_path: Path):
        """不同 event_id 的消息全部写入。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.append_outbox(_make_event(event_id="evt-2"))
        mb.append_outbox(_make_event(event_id="evt-3"))
        loaded = mb.load_outbox()
        ids = {e.event_id for e in loaded}
        assert ids == {"evt-1", "evt-2", "evt-3"}

    def test_state_json_persists_across_instances(self, tmp_path: Path):
        """state.json 持久化：新 Mailbox 实例能加载已有状态。"""
        mb1 = Mailbox(tmp_path, "alice")
        mb1.append_outbox(_make_event(event_id="evt-1"))
        # 新实例加载同一目录
        mb2 = Mailbox(tmp_path, "alice")
        assert mb2.get_status("evt-1") == STATUS_PENDING_DELIVERY
        # 幂等：新实例也不会重复写入
        mb2.append_outbox(_make_event(event_id="evt-1"))
        assert len(mb2.load_outbox()) == 1


# ---------------------------------------------------------------------------
# TestMailboxLoad
# ---------------------------------------------------------------------------


class TestMailboxLoad:
    """load_outbox / load_inbox 默认加载全部、按 status 过滤、limit 限制。"""

    def test_load_outbox_empty(self, tmp_path: Path):
        """空 outbox 返回空列表。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.load_outbox() == []

    def test_load_inbox_empty(self, tmp_path: Path):
        """空 inbox 返回空列表。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.load_inbox() == []

    def test_load_outbox_all(self, tmp_path: Path):
        """load_outbox 默认加载全部。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(5):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        loaded = mb.load_outbox()
        assert len(loaded) == 5

    def test_load_inbox_all(self, tmp_path: Path):
        """load_inbox 默认加载全部。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(3):
            mb.append_inbox(_make_event(event_id=f"evt-{i}"))
        loaded = mb.load_inbox()
        assert len(loaded) == 3

    def test_load_outbox_filter_by_status(self, tmp_path: Path):
        """load_outbox 按 status 过滤。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))  # pending_delivery
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_DELIVERED)  # delivered
        mb.append_outbox(_make_event(event_id="evt-2"))
        mb.update_status("evt-2", STATUS_CONFIRMED)  # confirmed

        pending = mb.load_outbox(status=STATUS_PENDING_DELIVERY)
        assert len(pending) == 1
        assert pending[0].event_id == "evt-0"

        delivered = mb.load_outbox(status=STATUS_DELIVERED)
        assert len(delivered) == 1
        assert delivered[0].event_id == "evt-1"

        confirmed = mb.load_outbox(status=STATUS_CONFIRMED)
        assert len(confirmed) == 1
        assert confirmed[0].event_id == "evt-2"

    def test_load_inbox_filter_by_status(self, tmp_path: Path):
        """load_inbox 按 status 过滤（inbox 初始为 delivered）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_inbox(_make_event(event_id="evt-0"))  # delivered
        mb.append_inbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_CONFIRMED)  # confirmed

        delivered = mb.load_inbox(status=STATUS_DELIVERED)
        assert len(delivered) == 1
        assert delivered[0].event_id == "evt-0"

        confirmed = mb.load_inbox(status=STATUS_CONFIRMED)
        assert len(confirmed) == 1
        assert confirmed[0].event_id == "evt-1"

    def test_load_outbox_limit(self, tmp_path: Path):
        """load_outbox 限制返回数量。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(10):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        loaded = mb.load_outbox(limit=3)
        assert len(loaded) == 3

    def test_load_inbox_limit(self, tmp_path: Path):
        """load_inbox 限制返回数量。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(10):
            mb.append_inbox(_make_event(event_id=f"evt-{i}"))
        loaded = mb.load_inbox(limit=5)
        assert len(loaded) == 5

    def test_load_outbox_limit_zero(self, tmp_path: Path):
        """limit=0 返回空列表。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        assert mb.load_outbox(limit=0) == []

    def test_load_outbox_status_none_returns_all(self, tmp_path: Path):
        """status=None 返回所有状态的消息。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))  # pending_delivery
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_CONFIRMED)  # confirmed
        loaded = mb.load_outbox(status=None)
        assert len(loaded) == 2

    def test_load_outbox_independent_from_inbox(self, tmp_path: Path):
        """outbox 与 inbox 互不影响。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="out-1"))
        mb.append_inbox(_make_event(event_id="in-1"))
        assert len(mb.load_outbox()) == 1
        assert len(mb.load_inbox()) == 1
        assert mb.load_outbox()[0].event_id == "out-1"
        assert mb.load_inbox()[0].event_id == "in-1"


# ---------------------------------------------------------------------------
# TestMailboxStatusMachine
# ---------------------------------------------------------------------------


class TestMailboxStatusMachine:
    """状态机：合法流转、非法流转抛 ValueError、get_status。"""

    def test_pending_to_delivered(self, tmp_path: Path):
        """pending_delivery → delivered 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.update_status("evt-1", STATUS_DELIVERED) is True
        assert mb.get_status("evt-1") == STATUS_DELIVERED

    def test_pending_to_confirmed(self, tmp_path: Path):
        """pending_delivery → confirmed 合法（跨级流转）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.update_status("evt-1", STATUS_CONFIRMED) is True
        assert mb.get_status("evt-1") == STATUS_CONFIRMED

    def test_pending_to_revised(self, tmp_path: Path):
        """pending_delivery → revised 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.update_status("evt-1", STATUS_REVISED) is True

    def test_pending_to_needs_human_review(self, tmp_path: Path):
        """pending_delivery → needs_human_review 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.update_status("evt-1", STATUS_NEEDS_HUMAN_REVIEW) is True

    def test_delivered_to_confirmed(self, tmp_path: Path):
        """delivered → confirmed 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.update_status("evt-1", STATUS_CONFIRMED) is True
        assert mb.get_status("evt-1") == STATUS_CONFIRMED

    def test_delivered_to_revised(self, tmp_path: Path):
        """delivered → revised 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.update_status("evt-1", STATUS_REVISED) is True

    def test_delivered_to_needs_human_review(self, tmp_path: Path):
        """delivered → needs_human_review 合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.update_status("evt-1", STATUS_NEEDS_HUMAN_REVIEW) is True

    def test_confirmed_to_delivered_raises(self, tmp_path: Path):
        """confirmed → delivered 非法（终态不可回退）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_CONFIRMED)
        with pytest.raises(ValueError, match="非法状态流转"):
            mb.update_status("evt-1", STATUS_DELIVERED)

    def test_confirmed_to_pending_raises(self, tmp_path: Path):
        """confirmed → pending_delivery 非法（终态不可回退）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_CONFIRMED)
        with pytest.raises(ValueError):
            mb.update_status("evt-1", STATUS_PENDING_DELIVERY)

    def test_revised_to_delivered_raises(self, tmp_path: Path):
        """revised → delivered 非法（终态不可回退）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_REVISED)
        with pytest.raises(ValueError):
            mb.update_status("evt-1", STATUS_DELIVERED)

    def test_needs_human_review_to_confirmed_raises(self, tmp_path: Path):
        """needs_human_review → confirmed 非法（终态不可回退）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_NEEDS_HUMAN_REVIEW)
        with pytest.raises(ValueError):
            mb.update_status("evt-1", STATUS_CONFIRMED)

    def test_pending_to_pending_idempotent(self, tmp_path: Path):
        """pending_delivery → pending_delivery 幂等合法（无操作返回 True）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.update_status("evt-1", STATUS_PENDING_DELIVERY) is True
        assert mb.get_status("evt-1") == STATUS_PENDING_DELIVERY

    def test_delivered_to_delivered_idempotent(self, tmp_path: Path):
        """delivered → delivered 幂等合法。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.update_status("evt-1", STATUS_DELIVERED) is True
        assert mb.get_status("evt-1") == STATUS_DELIVERED

    def test_confirmed_to_confirmed_idempotent(self, tmp_path: Path):
        """confirmed → confirmed 幂等合法（终态自身更新不报错）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.update_status("evt-1", STATUS_CONFIRMED)
        assert mb.update_status("evt-1", STATUS_CONFIRMED) is True

    def test_update_status_unknown_event_returns_false(self, tmp_path: Path):
        """更新不存在 event_id 的状态返回 False。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.update_status("nonexistent", STATUS_DELIVERED) is False

    def test_update_status_unknown_status_raises(self, tmp_path: Path):
        """传入未知状态抛 ValueError。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        with pytest.raises(ValueError, match="未知状态"):
            mb.update_status("evt-1", "unknown_status")

    def test_get_status_returns_none_for_unknown(self, tmp_path: Path):
        """查询不存在的 event_id 返回 None。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.get_status("nonexistent") is None

    def test_get_status_after_transition(self, tmp_path: Path):
        """流转后 get_status 返回最新状态。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.get_status("evt-1") == STATUS_PENDING_DELIVERY
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.get_status("evt-1") == STATUS_DELIVERED
        mb.update_status("evt-1", STATUS_REVISED)
        assert mb.get_status("evt-1") == STATUS_REVISED

    def test_status_persists_across_instances(self, tmp_path: Path):
        """状态持久化：新 Mailbox 实例能加载已保存的状态。"""
        mb1 = Mailbox(tmp_path, "alice")
        mb1.append_outbox(_make_event(event_id="evt-1"))
        mb1.update_status("evt-1", STATUS_DELIVERED)

        mb2 = Mailbox(tmp_path, "alice")
        assert mb2.get_status("evt-1") == STATUS_DELIVERED

    def test_full_lifecycle_pending_to_confirmed(self, tmp_path: Path):
        """完整生命周期：pending → delivered → confirmed。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-1"))
        assert mb.get_status("evt-1") == STATUS_PENDING_DELIVERY
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.get_status("evt-1") == STATUS_DELIVERED
        mb.update_status("evt-1", STATUS_CONFIRMED)
        assert mb.get_status("evt-1") == STATUS_CONFIRMED
        # 终态不可再流转
        with pytest.raises(ValueError):
            mb.update_status("evt-1", STATUS_DELIVERED)


# ---------------------------------------------------------------------------
# TestMailboxRemoveDelivered
# ---------------------------------------------------------------------------


class TestMailboxRemoveDelivered:
    """remove_delivered：删除已投递消息、原子重写、删除后 load 不再返回。"""

    def test_remove_delivered_basic(self, tmp_path: Path):
        """删除指定 event_id 后 outbox 不再包含该消息。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.append_outbox(_make_event(event_id="evt-2"))

        removed = mb.remove_delivered(event_ids={"evt-1"})
        assert removed == 1
        loaded = mb.load_outbox()
        ids = {e.event_id for e in loaded}
        assert ids == {"evt-0", "evt-2"}

    def test_remove_multiple(self, tmp_path: Path):
        """一次删除多条消息。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(5):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        removed = mb.remove_delivered(event_ids={"evt-0", "evt-2", "evt-4"})
        assert removed == 3
        loaded = mb.load_outbox()
        ids = {e.event_id for e in loaded}
        assert ids == {"evt-1", "evt-3"}

    def test_remove_nonexistent_returns_zero(self, tmp_path: Path):
        """删除不存在的 event_id 返回 0，不影响已有消息。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        removed = mb.remove_delivered(event_ids={"nonexistent"})
        assert removed == 0
        assert len(mb.load_outbox()) == 1

    def test_remove_empty_set(self, tmp_path: Path):
        """空 set 删除返回 0。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        removed = mb.remove_delivered(event_ids=set())
        assert removed == 0
        assert len(mb.load_outbox()) == 1

    def test_remove_no_outbox_file(self, tmp_path: Path):
        """outbox.jsonl 不存在时返回 0。"""
        mb = Mailbox(tmp_path, "alice")
        removed = mb.remove_delivered(event_ids={"evt-0"})
        assert removed == 0

    def test_remove_atomic_rewrite_no_tmp_left(self, tmp_path: Path):
        """原子重写后 .tmp 文件被清理。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.remove_delivered(event_ids={"evt-0"})
        tmp_file = tmp_path / "alice" / "outbox.tmp"
        assert not tmp_file.exists()

    def test_remove_keeps_unremoved_events_data(self, tmp_path: Path):
        """删除后保留的事件数据完整。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(
            _make_event(
                event_id="evt-0",
                event_type="ask",
                payload={"question": "如何配置 lint?"},
            )
        )
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.remove_delivered(event_ids={"evt-1"})
        loaded = mb.load_outbox()
        assert len(loaded) == 1
        assert loaded[0].event_id == "evt-0"
        assert loaded[0].payload == {"question": "如何配置 lint?"}

    def test_remove_all_then_outbox_empty(self, tmp_path: Path):
        """删除全部后 outbox 为空（文件存在但无内容）。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        mb.append_outbox(_make_event(event_id="evt-1"))
        removed = mb.remove_delivered(event_ids={"evt-0", "evt-1"})
        assert removed == 2
        assert mb.load_outbox() == []
        # 文件仍存在（只是空内容）
        assert (tmp_path / "alice" / "outbox.jsonl").is_file()

    def test_remove_does_not_affect_inbox(self, tmp_path: Path):
        """remove_delivered 只影响 outbox，不影响 inbox。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="out-1"))
        mb.append_inbox(_make_event(event_id="in-1"))
        mb.remove_delivered(event_ids={"out-1", "in-1"})
        # inbox 中的 in-1 不受影响
        assert len(mb.load_inbox()) == 1
        assert mb.load_inbox()[0].event_id == "in-1"
        # outbox 中的 out-1 被删除
        assert len(mb.load_outbox()) == 0

    def test_remove_then_append_new(self, tmp_path: Path):
        """删除后仍能追加新消息。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        mb.remove_delivered(event_ids={"evt-0"})
        mb.append_outbox(_make_event(event_id="evt-1"))
        loaded = mb.load_outbox()
        assert len(loaded) == 1
        assert loaded[0].event_id == "evt-1"


# ---------------------------------------------------------------------------
# TestMailboxPendingCount
# ---------------------------------------------------------------------------


class TestMailboxPendingCount:
    """pending_delivery_count 计数。"""

    def test_count_zero_empty(self, tmp_path: Path):
        """空 outbox 返回 0。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.pending_delivery_count() == 0

    def test_count_all_pending(self, tmp_path: Path):
        """全部 pending_delivery 时计数正确。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(5):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        assert mb.pending_delivery_count() == 5

    def test_count_after_status_change(self, tmp_path: Path):
        """部分消息状态变更后计数正确。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(5):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        # 2 条变为 delivered
        mb.update_status("evt-0", STATUS_DELIVERED)
        mb.update_status("evt-1", STATUS_DELIVERED)
        assert mb.pending_delivery_count() == 3

    def test_count_after_all_delivered(self, tmp_path: Path):
        """全部变为 delivered 后计数为 0。"""
        mb = Mailbox(tmp_path, "alice")
        for i in range(3):
            mb.append_outbox(_make_event(event_id=f"evt-{i}"))
        for i in range(3):
            mb.update_status(f"evt-{i}", STATUS_DELIVERED)
        assert mb.pending_delivery_count() == 0

    def test_count_excludes_inbox(self, tmp_path: Path):
        """pending_delivery_count 只统计 outbox，不统计 inbox。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="out-1"))
        mb.append_inbox(_make_event(event_id="in-1"))
        # inbox 消息初始为 delivered，不应计入 pending_delivery
        assert mb.pending_delivery_count() == 1

    def test_count_after_remove(self, tmp_path: Path):
        """删除消息后计数更新。"""
        mb = Mailbox(tmp_path, "alice")
        mb.append_outbox(_make_event(event_id="evt-0"))
        mb.append_outbox(_make_event(event_id="evt-1"))
        mb.remove_delivered(event_ids={"evt-0"})
        assert mb.pending_delivery_count() == 1

    def test_count_no_outbox_file(self, tmp_path: Path):
        """outbox.jsonl 不存在时返回 0。"""
        mb = Mailbox(tmp_path, "alice")
        assert mb.pending_delivery_count() == 0
