"""Task 11 测试：ConversationEvent / PeerSnapshot 构造与默认值。"""

from __future__ import annotations

from pathlib import Path

from server.async_comm.types import ConversationEvent, PeerSnapshot, VectorClock


class TestConversationEvent:
    """ConversationEvent 构造与字段赋值。"""

    def test_required_fields(self):
        """必填字段：event_id / event_type / peer_id / timestamp。"""
        evt = ConversationEvent(
            event_id="evt-1",
            event_type="ask",
            peer_id="bob",
            timestamp="2026-08-12T10:00:00Z",
        )
        assert evt.event_id == "evt-1"
        assert evt.event_type == "ask"
        assert evt.peer_id == "bob"
        assert evt.timestamp == "2026-08-12T10:00:00Z"

    def test_defaults(self):
        """未提供字段使用默认值。"""
        evt = ConversationEvent(
            event_id="evt-1",
            event_type="ask",
            peer_id="bob",
            timestamp="2026-08-12T10:00:00Z",
        )
        assert isinstance(evt.vector_clock, VectorClock)
        assert evt.vector_clock.counters == {}
        assert evt.payload == {}
        assert evt.in_reply_to == ""
        assert evt.degraded is False
        assert evt.realtime is False
        assert evt.based_on == ""
        assert evt.snapshot_stale is False

    def test_full_construction(self):
        """完整构造（含 vector_clock、回复链、影子联络标记）。"""
        vc = VectorClock(counters={"alice": 1, "bob": 2})
        evt = ConversationEvent(
            event_id="evt-2",
            event_type="simulated_answer",
            peer_id="bob",
            timestamp="2026-08-12T10:01:00Z",
            vector_clock=vc,
            payload={"answer": "lint 规则参考 .eslintrc"},
            in_reply_to="evt-1",
            degraded=True,
            realtime=False,
            based_on="bob_v38",
            snapshot_stale=True,
        )
        assert evt.event_id == "evt-2"
        assert evt.event_type == "simulated_answer"
        assert evt.peer_id == "bob"
        assert evt.timestamp == "2026-08-12T10:01:00Z"
        assert evt.vector_clock.counters == {"alice": 1, "bob": 2}
        assert evt.payload == {"answer": "lint 规则参考 .eslintrc"}
        assert evt.in_reply_to == "evt-1"
        assert evt.degraded is True
        assert evt.realtime is False
        assert evt.based_on == "bob_v38"
        assert evt.snapshot_stale is True

    def test_event_types(self):
        """支持所有事件类型。"""
        for etype in (
            "ask",
            "realtime_answer",
            "simulated_answer",
            "confirmed",
            "revised",
            "needs_human_review",
        ):
            evt = ConversationEvent(
                event_id=f"evt-{etype}",
                event_type=etype,
                peer_id="bob",
                timestamp="2026-08-12T10:00:00Z",
            )
            assert evt.event_type == etype

    def test_payload_independent(self):
        """payload 默认值不共享（mutable default 隔离）。"""
        a = ConversationEvent(
            event_id="a", event_type="ask", peer_id="bob", timestamp="t"
        )
        b = ConversationEvent(
            event_id="b", event_type="ask", peer_id="bob", timestamp="t"
        )
        a.payload["k"] = "v"
        assert b.payload == {}

    def test_vector_clock_independent(self):
        """vector_clock 默认值不共享（mutable default 隔离）。"""
        a = ConversationEvent(
            event_id="a", event_type="ask", peer_id="bob", timestamp="t"
        )
        b = ConversationEvent(
            event_id="b", event_type="ask", peer_id="bob", timestamp="t"
        )
        a.vector_clock.increment("alice")
        assert b.vector_clock.counters == {}


class TestPeerSnapshot:
    """PeerSnapshot 构造与字段赋值。"""

    def test_required_peer_id(self):
        """peer_id 为必填字段。"""
        snap = PeerSnapshot(peer_id="bob")
        assert snap.peer_id == "bob"

    def test_defaults(self):
        """未提供字段使用默认值。"""
        snap = PeerSnapshot(peer_id="bob")
        assert snap.snapshot_version == ""
        assert snap.captured_at == ""
        assert snap.harness_path == Path()
        assert snap.manifest_path == Path()
        assert snap.vector_clock_path == Path()
        assert isinstance(snap.vector_clock, VectorClock)
        assert snap.vector_clock.counters == {}

    def test_full_construction(self):
        """完整构造（含路径与版本向量）。"""
        vc = VectorClock(counters={"alice": 1, "bob": 3})
        snap = PeerSnapshot(
            peer_id="bob",
            snapshot_version="v38",
            captured_at="2026-08-12T10:00:00Z",
            harness_path=Path("/data/snapshots/bob"),
            manifest_path=Path("/data/snapshots/bob/manifest.json"),
            vector_clock_path=Path("/data/snapshots/bob/vector_clock.json"),
            vector_clock=vc,
        )
        assert snap.peer_id == "bob"
        assert snap.snapshot_version == "v38"
        assert snap.captured_at == "2026-08-12T10:00:00Z"
        assert snap.harness_path == Path("/data/snapshots/bob")
        assert snap.manifest_path == Path("/data/snapshots/bob/manifest.json")
        assert snap.vector_clock_path == Path("/data/snapshots/bob/vector_clock.json")
        assert snap.vector_clock.counters == {"alice": 1, "bob": 3}

    def test_harness_path_independent(self):
        """harness_path 默认值不共享（mutable default 隔离）。"""
        a = PeerSnapshot(peer_id="a")
        b = PeerSnapshot(peer_id="b")
        a.harness_path = Path("/tmp/a")
        assert b.harness_path == Path()

    def test_vector_clock_independent(self):
        """vector_clock 默认值不共享（mutable default 隔离）。"""
        a = PeerSnapshot(peer_id="a")
        b = PeerSnapshot(peer_id="b")
        a.vector_clock.increment("alice")
        assert b.vector_clock.counters == {}

    def test_is_stale_property(self):
        """is_stale 属性默认返回 False（实际判断由 PeerSnapshotManager 负责）。"""
        snap = PeerSnapshot(peer_id="bob", captured_at="2026-08-12T10:00:00Z")
        assert snap.is_stale is False

    def test_is_stale_not_constructable(self):
        """is_stale 是 @property，不可通过构造函数传入。"""
        try:
            PeerSnapshot(peer_id="bob", is_stale=True)  # type: ignore[call-arg]
            raise AssertionError("应抛出 TypeError")
        except TypeError:
            pass
