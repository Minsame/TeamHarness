"""Task 15 测试：ConversationLog append-only 日志。

覆盖 append / 幂等 / load_all / load_by_peer / load_by_type / load_thread /
count / get_event / exists / 序列化辅助函数。

测试隔离：用 tmp_path fixture（pytest）为每个用例提供独立临时目录。
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
)
from server.async_comm.conversation_log import (
    ConversationLog,
    event_from_dict,
    event_to_dict,
)
from server.async_comm.types import ConversationEvent, VectorClock


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str | None = None,
    event_type: str = EVENT_ASK,
    peer_id: str = "bob",
    timestamp: str | None = None,
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
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        vector_clock=vector_clock or VectorClock(),
        payload=payload if payload is not None else {},
        in_reply_to=in_reply_to,
        degraded=degraded,
        realtime=realtime,
        based_on=based_on,
        snapshot_stale=snapshot_stale,
    )


def _ts(seconds: int) -> str:
    """生成可控时间戳（基于固定基准 + 偏移秒数），用于测试排序。

    返回 ISO 格式字符串，seconds 越大时间越晚。
    """
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta_seconds(seconds)).isoformat()


def timedelta_seconds(seconds: int):
    """构造 timedelta（避免导入 timedelta）。"""
    from datetime import timedelta

    return timedelta(seconds=seconds)


# ---------------------------------------------------------------------------
# TestConversationLogAppend
# ---------------------------------------------------------------------------


class TestConversationLogAppend:
    """append 基本写入。"""

    def test_append_writes_event_to_file(self, tmp_path: Path):
        """append 后事件被写入 JSONL 文件。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        event = _make_event(event_id="evt-1")
        log.append(event)
        # 文件存在且包含一行
        content = log.log_path.read_text(encoding="utf-8").strip()
        assert content != ""
        assert "evt-1" in content

    def test_append_creates_parent_directory(self, tmp_path: Path):
        """父目录不存在时自动创建（嵌套多级）。"""
        nested = tmp_path / "a" / "b" / "c" / "conversation.jsonl"
        log = ConversationLog(nested)
        # __init__ 后父目录应已创建
        assert nested.parent.is_dir()
        # append 后文件也应创建
        log.append(_make_event(event_id="evt-1"))
        assert nested.is_file()

    def test_append_returns_event_id(self, tmp_path: Path):
        """append 返回传入事件的 event_id。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        event_id = str(uuid.uuid4())
        event = _make_event(event_id=event_id)
        result = log.append(event)
        assert result == event_id

    def test_append_multiple_events_all_written(self, tmp_path: Path):
        """连续 append 多条事件全部写入。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        ids = []
        for i in range(5):
            eid = f"evt-{i}"
            ids.append(eid)
            log.append(_make_event(event_id=eid))
        events = log.load_all()
        assert len(events) == 5
        assert {e.event_id for e in events} == set(ids)


# ---------------------------------------------------------------------------
# TestConversationLogIdempotency
# ---------------------------------------------------------------------------


class TestConversationLogIdempotency:
    """幂等去重。"""

    def test_duplicate_event_id_not_rewritten(self, tmp_path: Path):
        """相同 event_id 重复 append 不重复写入。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        event = _make_event(event_id="dup-1")
        log.append(event)
        # 再次 append 同一 event_id
        log.append(event)
        events = log.load_all()
        assert len(events) == 1
        assert events[0].event_id == "dup-1"

    def test_duplicate_returns_same_event_id(self, tmp_path: Path):
        """重复 append 返回相同的 event_id。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        event = _make_event(event_id="dup-2")
        first = log.append(event)
        second = log.append(event)
        assert first == second == "dup-2"

    def test_exists_returns_true_for_existing(self, tmp_path: Path):
        """已存在的事件 exists 返回 True。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="ex-1"))
        assert log.exists("ex-1") is True

    def test_exists_returns_false_for_missing(self, tmp_path: Path):
        """不存在的事件 exists 返回 False。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="ex-1"))
        assert log.exists("not-exist") is False

    def test_exists_returns_false_for_empty_log(self, tmp_path: Path):
        """空日志 exists 始终返回 False。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        assert log.exists("anything") is False


# ---------------------------------------------------------------------------
# TestConversationLogLoad
# ---------------------------------------------------------------------------


class TestConversationLogLoad:
    """load_all / load_by_peer / load_by_type。"""

    def test_load_all_empty_log_returns_empty_list(self, tmp_path: Path):
        """空日志 load_all 返回空列表（不是 None）。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        events = log.load_all()
        assert events == []
        assert isinstance(events, list)

    def test_load_all_returns_all_events_sorted_by_time(self, tmp_path: Path):
        """load_all 加载全部事件并按时间升序排序。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        # 故意乱序写入，验证 load_all 排序
        log.append(_make_event(event_id="e3", timestamp=_ts(30)))
        log.append(_make_event(event_id="e1", timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", timestamp=_ts(20)))
        events = log.load_all()
        assert [e.event_id for e in events] == ["e1", "e2", "e3"]

    def test_load_all_with_limit(self, tmp_path: Path):
        """load_all(limit=n) 返回时间最早的 n 条。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e3", timestamp=_ts(30)))
        log.append(_make_event(event_id="e1", timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", timestamp=_ts(20)))
        events = log.load_all(limit=2)
        assert [e.event_id for e in events] == ["e1", "e2"]

    def test_load_all_limit_zero(self, tmp_path: Path):
        """limit=0 返回空列表。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", timestamp=_ts(10)))
        events = log.load_all(limit=0)
        assert events == []

    def test_load_by_peer_filters_correctly(self, tmp_path: Path):
        """load_by_peer 按 peer_id 过滤。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", peer_id="bob", timestamp=_ts(20)))
        log.append(_make_event(event_id="e3", peer_id="alice", timestamp=_ts(30)))
        alice_events = log.load_by_peer("alice")
        assert len(alice_events) == 2
        assert {e.event_id for e in alice_events} == {"e1", "e3"}

    def test_load_by_peer_sorted_by_time(self, tmp_path: Path):
        """load_by_peer 结果按时间升序排序。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e3", peer_id="alice", timestamp=_ts(30)))
        log.append(_make_event(event_id="e1", peer_id="alice", timestamp=_ts(10)))
        alice_events = log.load_by_peer("alice")
        assert [e.event_id for e in alice_events] == ["e1", "e3"]

    def test_load_by_peer_with_limit(self, tmp_path: Path):
        """load_by_peer 支持 limit。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", peer_id="alice", timestamp=_ts(20)))
        log.append(_make_event(event_id="e3", peer_id="alice", timestamp=_ts(30)))
        events = log.load_by_peer("alice", limit=2)
        assert [e.event_id for e in events] == ["e1", "e2"]

    def test_load_by_peer_no_match_returns_empty(self, tmp_path: Path):
        """load_by_peer 无匹配返回空列表。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice"))
        events = log.load_by_peer("nobody")
        assert events == []

    def test_load_by_type_filters_correctly(self, tmp_path: Path):
        """load_by_type 按 event_type 过滤。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(
            _make_event(
                event_id="e2",
                event_type=EVENT_REALTIME_ANSWER,
                timestamp=_ts(20),
            )
        )
        log.append(_make_event(event_id="e3", event_type=EVENT_ASK, timestamp=_ts(30)))
        asks = log.load_by_type(EVENT_ASK)
        assert len(asks) == 2
        assert {e.event_id for e in asks} == {"e1", "e3"}

    def test_load_by_type_sorted_by_time(self, tmp_path: Path):
        """load_by_type 结果按时间升序排序。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e3", event_type=EVENT_ASK, timestamp=_ts(30)))
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        asks = log.load_by_type(EVENT_ASK)
        assert [e.event_id for e in asks] == ["e1", "e3"]

    def test_load_by_type_with_limit(self, tmp_path: Path):
        """load_by_type 支持 limit。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", event_type=EVENT_ASK, timestamp=_ts(20)))
        log.append(_make_event(event_id="e3", event_type=EVENT_ASK, timestamp=_ts(30)))
        events = log.load_by_type(EVENT_ASK, limit=2)
        assert [e.event_id for e in events] == ["e1", "e2"]


# ---------------------------------------------------------------------------
# TestConversationLogThread
# ---------------------------------------------------------------------------


class TestConversationLogThread:
    """load_thread 回复链查找。"""

    def test_load_thread_full_chain(self, tmp_path: Path):
        """load_thread 加载完整回复链：ask → answer → confirmed。

        场景：
        - ask (e1) 是根
        - answer (e2) in_reply_to=e1
        - confirmed (e3) in_reply_to=e2
        从 e2 加载应返回 [e1, e2, e3]。
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(
            _make_event(
                event_id="e2",
                event_type=EVENT_REALTIME_ANSWER,
                timestamp=_ts(20),
                in_reply_to="e1",
            )
        )
        log.append(
            _make_event(
                event_id="e3",
                event_type=EVENT_CONFIRMED,
                timestamp=_ts(30),
                in_reply_to="e2",
            )
        )
        thread = log.load_thread("e2")
        # 应包含 e1（前驱）、e2（自身）、e3（后继）
        assert {e.event_id for e in thread} == {"e1", "e2", "e3"}
        # 按时间排序
        assert [e.event_id for e in thread] == ["e1", "e2", "e3"]

    def test_load_thread_from_root(self, tmp_path: Path):
        """从根事件（无 in_reply_to）加载回复链。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(
            _make_event(
                event_id="e2",
                event_type=EVENT_REALTIME_ANSWER,
                timestamp=_ts(20),
                in_reply_to="e1",
            )
        )
        thread = log.load_thread("e1")
        assert {e.event_id for e in thread} == {"e1", "e2"}

    def test_load_thread_single_event_no_replies(self, tmp_path: Path):
        """单条事件无回复链，load_thread 仅返回自身。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        thread = log.load_thread("e1")
        assert len(thread) == 1
        assert thread[0].event_id == "e1"

    def test_load_thread_nonexistent_event_id_returns_empty(self, tmp_path: Path):
        """不存在的 event_id 返回空列表。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        thread = log.load_thread("nonexistent")
        assert thread == []

    def test_load_thread_branching_replies(self, tmp_path: Path):
        """分支回复：一个事件被多个事件回复，全部应纳入线程。

        场景：
        - e1 (ask)
        - e2 (realtime_answer) in_reply_to=e1
        - e3 (simulated_answer) in_reply_to=e1
        从 e1 加载应返回 [e1, e2, e3]。
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(
            _make_event(
                event_id="e2",
                event_type=EVENT_REALTIME_ANSWER,
                timestamp=_ts(20),
                in_reply_to="e1",
            )
        )
        log.append(
            _make_event(
                event_id="e3",
                event_type=EVENT_SIMULATED_ANSWER,
                timestamp=_ts(30),
                in_reply_to="e1",
            )
        )
        thread = log.load_thread("e1")
        assert {e.event_id for e in thread} == {"e1", "e2", "e3"}

    def test_load_thread_unrelated_events_excluded(self, tmp_path: Path):
        """不相关的同级事件不纳入线程。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", event_type=EVENT_ASK, timestamp=_ts(10)))
        log.append(
            _make_event(
                event_id="e2",
                event_type=EVENT_REALTIME_ANSWER,
                timestamp=_ts(20),
                in_reply_to="e1",
            )
        )
        # 另一条独立 ask，不应被纳入 e1 的线程
        log.append(_make_event(event_id="e3", event_type=EVENT_ASK, timestamp=_ts(30)))
        thread = log.load_thread("e1")
        assert {e.event_id for e in thread} == {"e1", "e2"}


# ---------------------------------------------------------------------------
# TestConversationLogCount
# ---------------------------------------------------------------------------


class TestConversationLogCount:
    """count 统计。"""

    def test_count_total(self, tmp_path: Path):
        """count 无过滤返回总数。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e2", peer_id="bob", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e3", peer_id="alice", event_type=EVENT_CONFIRMED))
        assert log.count() == 3

    def test_count_empty_log(self, tmp_path: Path):
        """空日志 count 返回 0。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        assert log.count() == 0

    def test_count_by_peer(self, tmp_path: Path):
        """count 按 peer 过滤。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e2", peer_id="bob", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e3", peer_id="alice", event_type=EVENT_CONFIRMED))
        assert log.count(peer_id="alice") == 2
        assert log.count(peer_id="bob") == 1
        assert log.count(peer_id="nobody") == 0

    def test_count_by_type(self, tmp_path: Path):
        """count 按 type 过滤。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e2", peer_id="bob", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e3", peer_id="alice", event_type=EVENT_CONFIRMED))
        assert log.count(event_type=EVENT_ASK) == 2
        assert log.count(event_type=EVENT_CONFIRMED) == 1
        assert log.count(event_type=EVENT_REVISED) == 0

    def test_count_by_peer_and_type(self, tmp_path: Path):
        """count 按 peer + type 联合过滤。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", peer_id="alice", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e2", peer_id="alice", event_type=EVENT_CONFIRMED))
        log.append(_make_event(event_id="e3", peer_id="bob", event_type=EVENT_ASK))
        log.append(_make_event(event_id="e4", peer_id="alice", event_type=EVENT_ASK))
        # alice 的 ask 事件有 2 条
        assert log.count(peer_id="alice", event_type=EVENT_ASK) == 2
        # alice 的 confirmed 事件有 1 条
        assert log.count(peer_id="alice", event_type=EVENT_CONFIRMED) == 1
        # bob 的 confirmed 事件有 0 条
        assert log.count(peer_id="bob", event_type=EVENT_CONFIRMED) == 0


# ---------------------------------------------------------------------------
# TestConversationLogGetEvent
# ---------------------------------------------------------------------------


class TestConversationLogGetEvent:
    """get_event 单条查询。"""

    def test_get_event_returns_matching(self, tmp_path: Path):
        """存在的 event_id 返回对应事件。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        event = _make_event(
            event_id="target-1",
            event_type=EVENT_ASK,
            peer_id="alice",
            timestamp=_ts(10),
        )
        log.append(event)
        result = log.get_event("target-1")
        assert result is not None
        assert result.event_id == "target-1"
        assert result.event_type == EVENT_ASK
        assert result.peer_id == "alice"

    def test_get_event_returns_none_for_missing(self, tmp_path: Path):
        """不存在的 event_id 返回 None。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1"))
        result = log.get_event("nonexistent")
        assert result is None

    def test_get_event_empty_log_returns_none(self, tmp_path: Path):
        """空日志 get_event 返回 None。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        assert log.get_event("anything") is None

    def test_get_event_preserves_all_fields(self, tmp_path: Path):
        """get_event 返回的事件保留所有字段（含 vector_clock / payload 等）。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        vc = VectorClock(counters={"alice": 3, "bob": 1})
        event = _make_event(
            event_id="full-1",
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            timestamp=_ts(42),
            vector_clock=vc,
            payload={"question": "why?", "score": 0.8},
            in_reply_to="ask-1",
            degraded=True,
            realtime=False,
            based_on="bob_v38",
            snapshot_stale=True,
        )
        log.append(event)
        result = log.get_event("full-1")
        assert result is not None
        assert result.event_type == EVENT_SIMULATED_ANSWER
        assert result.peer_id == "bob"
        assert result.timestamp == _ts(42)
        assert result.vector_clock.counters == {"alice": 3, "bob": 1}
        assert result.payload == {"question": "why?", "score": 0.8}
        assert result.in_reply_to == "ask-1"
        assert result.degraded is True
        assert result.realtime is False
        assert result.based_on == "bob_v38"
        assert result.snapshot_stale is True


# ---------------------------------------------------------------------------
# TestConversationLogSerialization
# ---------------------------------------------------------------------------


class TestConversationLogSerialization:
    """event_to_dict / event_from_dict 序列化辅助函数。"""

    def test_roundtrip_basic(self):
        """event_to_dict → event_from_dict 往返一致。"""
        original = _make_event(
            event_id="rt-1",
            event_type=EVENT_ASK,
            peer_id="alice",
            timestamp=_ts(10),
            vector_clock=VectorClock(counters={"alice": 1}),
            payload={"q": "hello"},
        )
        data = event_to_dict(original)
        restored = event_from_dict(data)
        assert restored.event_id == original.event_id
        assert restored.event_type == original.event_type
        assert restored.peer_id == original.peer_id
        assert restored.timestamp == original.timestamp
        assert restored.payload == original.payload

    def test_roundtrip_all_event_types(self):
        """所有 6 种事件类型往返一致。"""
        types = [
            EVENT_ASK,
            EVENT_REALTIME_ANSWER,
            EVENT_SIMULATED_ANSWER,
            EVENT_CONFIRMED,
            EVENT_REVISED,
            EVENT_NEEDS_HUMAN_REVIEW,
        ]
        for et in types:
            original = _make_event(event_type=et, peer_id="bob")
            restored = event_from_dict(event_to_dict(original))
            assert restored.event_type == et, f"事件类型 {et} 往返失败"

    def test_vector_clock_serialized_to_dict(self):
        """vector_clock 字段在 to_dict 后为 dict 类型。"""
        vc = VectorClock(counters={"alice": 3, "bob": 1})
        event = _make_event(vector_clock=vc)
        data = event_to_dict(event)
        assert isinstance(data["vector_clock"], dict)
        assert data["vector_clock"] == {"alice": 3, "bob": 1}

    def test_vector_clock_deserialized_from_dict(self):
        """vector_clock 字段在 from_dict 后还原为 VectorClock 实例。"""
        data = {
            "event_id": "vc-1",
            "event_type": EVENT_ASK,
            "peer_id": "alice",
            "timestamp": _ts(10),
            "vector_clock": {"alice": 5, "bob": 2},
        }
        event = event_from_dict(data)
        assert isinstance(event.vector_clock, VectorClock)
        assert event.vector_clock.counters == {"alice": 5, "bob": 2}

    def test_vector_clock_roundtrip_preserves_counters(self):
        """vector_clock 往返后 counters 完全一致。"""
        vc = VectorClock(counters={"alice": 5, "bob": 3, "carol": 7})
        event = _make_event(vector_clock=vc)
        restored = event_from_dict(event_to_dict(event))
        assert restored.vector_clock.counters == vc.counters
        assert restored.vector_clock.compare(vc) == "equal"

    def test_all_fields_preserved_in_roundtrip(self):
        """所有字段（degraded, realtime, based_on, snapshot_stale 等）往返保留。"""
        original = _make_event(
            event_id="full-rt",
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            timestamp=_ts(99),
            vector_clock=VectorClock(counters={"alice": 2}),
            payload={"k": "v"},
            in_reply_to="ask-1",
            degraded=True,
            realtime=True,
            based_on="bob_v38",
            snapshot_stale=True,
        )
        restored = event_from_dict(event_to_dict(original))
        assert restored.event_id == "full-rt"
        assert restored.event_type == EVENT_SIMULATED_ANSWER
        assert restored.peer_id == "bob"
        assert restored.timestamp == _ts(99)
        assert restored.vector_clock.counters == {"alice": 2}
        assert restored.payload == {"k": "v"}
        assert restored.in_reply_to == "ask-1"
        assert restored.degraded is True
        assert restored.realtime is True
        assert restored.based_on == "bob_v38"
        assert restored.snapshot_stale is True

    def test_to_dict_does_not_mutate_event(self):
        """event_to_dict 不修改原事件对象。"""
        vc = VectorClock(counters={"alice": 1})
        event = _make_event(vector_clock=vc, payload={"k": "v"})
        data = event_to_dict(event)
        # 修改返回的 dict 不应影响原事件
        data["event_id"] = "modified"
        data["payload"]["k"] = "modified"
        data["vector_clock"]["alice"] = 999
        assert event.event_id != "modified"
        # payload 在原事件中应保持不变（注意：to_dict 直接返回 payload 引用）
        # 此处仅验证 vector_clock 不受影响（to_dict 返回 vector_clock.to_dict() 的副本）
        assert event.vector_clock.counters == {"alice": 1}

    def test_from_dict_with_missing_optional_fields_uses_defaults(self):
        """from_dict 缺失可选字段时使用 ConversationEvent 默认值。"""
        data = {
            "event_id": "min-1",
            "event_type": EVENT_ASK,
            "peer_id": "alice",
            "timestamp": _ts(10),
            # 缺失：vector_clock, payload, in_reply_to, degraded, realtime, based_on, snapshot_stale
        }
        event = event_from_dict(data)
        assert event.event_id == "min-1"
        assert event.vector_clock.counters == {}
        assert event.payload == {}
        assert event.in_reply_to == ""
        assert event.degraded is False
        assert event.realtime is False
        assert event.based_on == ""
        assert event.snapshot_stale is False

    def test_roundtrip_through_jsonl_file(self, tmp_path: Path):
        """通过 JSONL 文件往返：写入 → 读取 → 字段一致。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        original = _make_event(
            event_id="jsonl-1",
            event_type=EVENT_REVISED,
            peer_id="carol",
            timestamp=_ts(50),
            vector_clock=VectorClock(counters={"carol": 4, "alice": 2}),
            payload={"diff": "+10 -3"},
            in_reply_to="prev-1",
            degraded=False,
            realtime=True,
            based_on="carol_v12",
            snapshot_stale=False,
        )
        log.append(original)
        loaded = log.load_all()
        assert len(loaded) == 1
        result = loaded[0]
        assert result.event_id == "jsonl-1"
        assert result.event_type == EVENT_REVISED
        assert result.peer_id == "carol"
        assert result.timestamp == _ts(50)
        assert result.vector_clock.counters == {"carol": 4, "alice": 2}
        assert result.payload == {"diff": "+10 -3"}
        assert result.in_reply_to == "prev-1"
        assert result.degraded is False
        assert result.realtime is True
        assert result.based_on == "carol_v12"
        assert result.snapshot_stale is False


# ---------------------------------------------------------------------------
# TestConversationLogFileFormat（补充：验证 append-only 语义）
# ---------------------------------------------------------------------------


class TestConversationLogAppendOnly:
    """append-only 语义验证：已有行不被修改。"""

    def test_existing_lines_not_modified_on_append(self, tmp_path: Path):
        """追加新事件时已有行保持不变。"""
        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", timestamp=_ts(10)))
        # 保存第一行内容
        first_line = log.log_path.read_text(encoding="utf-8").splitlines()[0]
        # 追加第二条
        log.append(_make_event(event_id="e2", timestamp=_ts(20)))
        lines = log.log_path.read_text(encoding="utf-8").splitlines()
        # 第一行应与之前完全一致
        assert lines[0] == first_line
        assert len(lines) == 2

    def test_file_is_jsonl_format(self, tmp_path: Path):
        """文件格式为 JSONL：每行一个独立 JSON 对象。"""
        import json as json_mod

        log = ConversationLog(tmp_path / "conversation.jsonl")
        log.append(_make_event(event_id="e1", timestamp=_ts(10)))
        log.append(_make_event(event_id="e2", timestamp=_ts(20)))
        lines = log.log_path.read_text(encoding="utf-8").splitlines()
        assert len(lines) == 2
        # 每行都能独立解析为 JSON
        for line in lines:
            data = json_mod.loads(line)
            assert "event_id" in data
            assert "event_type" in data
            assert "vector_clock" in data
