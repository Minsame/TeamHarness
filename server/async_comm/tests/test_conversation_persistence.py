"""Task 27 测试：对话持久化与恢复。

覆盖：
- ConversationLog 对话状态管理（set / get / list_paused / list_active / clear）
- 状态流转：active → paused → resumed / active → timeout_disconnect → resumed
- resume_conversation 基于 in_reply_to 链重建上下文
- 原子写与并发安全（多线程 set_conversation_state）
- 向后兼容（旧数据缺失 conversation_state 字段 → 默认 active）
- PeerComm._offline_ask 标记 paused
- PeerComm.resume_conversation 委托
- daemon 超时检测标记 timeout_disconnect
- daemon peer 上线自动恢复

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录。
"""

from __future__ import annotations

import json
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.async_comm.constants import (
    CONV_STATE_ACTIVE,
    CONV_STATE_PAUSED,
    CONV_STATE_RESUMED,
    CONV_STATE_TIMEOUT_DISCONNECT,
    DEFAULT_REALTIME_SESSION_TIMEOUT,
    EVENT_ASK,
    EVENT_REALTIME_ANSWER,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_comm import PeerComm
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent, VectorClock
from server.client.config import ClientConfig
from server.client.daemon import ClientDaemon
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str | None = None,
    event_type: str = EVENT_ASK,
    peer_id: str = "bob",
    timestamp: str | None = None,
    in_reply_to: str = "",
    conversation_state: str = CONV_STATE_ACTIVE,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        vector_clock=VectorClock(),
        payload={},
        in_reply_to=in_reply_to,
        conversation_state=conversation_state,
    )


def _make_log(tmp_path: Path) -> ConversationLog:
    """构造 ConversationLog 测试实例。"""
    return ConversationLog(tmp_path / "conversation.jsonl")


class StubTransport:
    """Stub 传输层。"""

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        auto_answer: str = "",
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.auto_answer = auto_answer
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.fetch_calls: list[str] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(success=True, delivered_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        self.fetch_calls.append(peer_id)
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
        return []

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


class StubShadowComm:
    """Stub 影子联络。"""

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


def _make_comm(
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    shadow_comm: StubShadowComm | None = None,
    member_id: str = "alice",
) -> tuple[PeerComm, StubTransport, StubShadowComm, ConversationLog]:
    """构造 PeerComm 测试实例。"""
    transport = transport or StubTransport()
    shadow_comm = shadow_comm or StubShadowComm()
    log = ConversationLog(tmp_path / "conversation.jsonl")
    comm = PeerComm(
        transport=transport,
        mailbox=Mailbox(tmp_path / "mb", member_id),
        conversation_log=log,
        peer_snapshot_manager=PeerSnapshotManager(tmp_path / "snapshots"),
        member_id=member_id,
        shadow_comm=shadow_comm,
    )
    return comm, transport, shadow_comm, log


def _make_daemon_config(tmp_path: Path, *, member_id: str = "alice") -> ClientConfig:
    """构造测试用 ClientConfig。"""
    return ClientConfig(
        repo_root=str(tmp_path),
        member_id=member_id,
        network_check_interval_seconds=60,
    )


def _make_daemon(tmp_path: Path, *, member_id: str = "alice") -> ClientDaemon:
    """构造测试用 ClientDaemon。"""
    config = _make_daemon_config(tmp_path, member_id=member_id)
    return ClientDaemon(config)


def _inject_daemon_components(
    daemon: ClientDaemon,
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    member_id: str = "alice",
) -> dict:
    """为 daemon 注入 Stub peer 通信组件。"""
    from server.async_comm.sync_protocol import SyncProtocol

    base_dir = tmp_path / "async_comm"
    transport = transport or StubTransport()
    mailbox = Mailbox(base_dir, member_id)
    conversation_log = ConversationLog(base_dir / "conversation.jsonl")
    peer_snapshot_manager = PeerSnapshotManager(base_dir)
    sync_protocol = SyncProtocol(
        transport=transport,
        mailbox=mailbox,
        conversation_log=conversation_log,
        peer_snapshot_manager=peer_snapshot_manager,
        member_id=member_id,
    )
    components = {
        "transport": transport,
        "mailbox": mailbox,
        "conversation_log": conversation_log,
        "peer_snapshot_manager": peer_snapshot_manager,
        "sync_protocol": sync_protocol,
    }
    daemon._peer_comm_components = components
    return components


# ---------------------------------------------------------------------------
# TestConversationStateManagement：状态设置与读取
# ---------------------------------------------------------------------------


class TestConversationStateManagement:
    """对话状态管理基础测试。"""

    def test_set_and_get_state(self, tmp_path: Path) -> None:
        """设置状态后可正确读取。"""
        log = _make_log(tmp_path)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1", reason="peer_offline"
        )
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["peer_id"] == "bob"
        assert state["state"] == CONV_STATE_PAUSED
        assert state["last_event_id"] == "evt-1"
        assert state["reason"] == "peer_offline"
        assert "updated_at" in state

    def test_get_state_none_when_not_set(self, tmp_path: Path) -> None:
        """未设置状态的 peer 返回 None。"""
        log = _make_log(tmp_path)
        assert log.get_conversation_state("bob") is None

    def test_set_state_idempotent(self, tmp_path: Path) -> None:
        """相同状态 + 相同 last_event_id 重复设置无副作用。"""
        log = _make_log(tmp_path)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1", reason="offline"
        )
        state1 = log.get_conversation_state("bob")
        # 幂等设置
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1", reason="offline"
        )
        state2 = log.get_conversation_state("bob")
        assert state1["updated_at"] == state2["updated_at"]

    def test_set_state_different_last_event_id_updates(self, tmp_path: Path) -> None:
        """不同 last_event_id 视为状态变更，更新记录。"""
        log = _make_log(tmp_path)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="evt-1", reason="start"
        )
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="evt-2", reason="new_event"
        )
        state = log.get_conversation_state("bob")
        assert state["last_event_id"] == "evt-2"

    def test_clear_state(self, tmp_path: Path) -> None:
        """清除状态后 get 返回 None。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="evt-1")
        log.clear_conversation_state("bob")
        assert log.get_conversation_state("bob") is None

    def test_clear_nonexistent_state_no_error(self, tmp_path: Path) -> None:
        """清除不存在的状态不报错。"""
        log = _make_log(tmp_path)
        log.clear_conversation_state("bob")  # 不应抛异常

    def test_state_persisted_across_instances(self, tmp_path: Path) -> None:
        """状态文件持久化：新实例能读取旧实例写入的状态。"""
        log1 = _make_log(tmp_path)
        log1.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1", reason="offline"
        )
        # 新实例读取同一文件
        log2 = ConversationLog(tmp_path / "conversation.jsonl")
        state = log2.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_PAUSED


# ---------------------------------------------------------------------------
# TestConversationStateListing：列表查询
# ---------------------------------------------------------------------------


class TestConversationStateListing:
    """对话状态列表查询测试。"""

    def test_list_paused_conversations(self, tmp_path: Path) -> None:
        """list_paused_conversations 返回 paused + timeout_disconnect。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="e1")
        log.set_conversation_state("carol", CONV_STATE_ACTIVE, last_event_id="e2")
        log.set_conversation_state(
            "dave", CONV_STATE_TIMEOUT_DISCONNECT, last_event_id="e3"
        )
        log.set_conversation_state("eve", CONV_STATE_RESUMED, last_event_id="e4")

        paused = log.list_paused_conversations()
        peer_ids = {e["peer_id"] for e in paused}
        assert peer_ids == {"bob", "dave"}

    def test_list_paused_empty(self, tmp_path: Path) -> None:
        """无 paused/timeout_disconnect 时返回空列表。"""
        log = _make_log(tmp_path)
        assert log.list_paused_conversations() == []

    def test_list_active_conversations(self, tmp_path: Path) -> None:
        """list_active_conversations 返回 active 状态。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_ACTIVE, last_event_id="e1")
        log.set_conversation_state("carol", CONV_STATE_PAUSED, last_event_id="e2")

        active = log.list_active_conversations()
        peer_ids = {e["peer_id"] for e in active}
        assert peer_ids == {"bob"}

    def test_list_paused_sorted_by_updated_at(self, tmp_path: Path) -> None:
        """list_paused_conversations 按 updated_at 升序。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="e1")
        time.sleep(0.01)  # 确保 updated_at 不同
        log.set_conversation_state(
            "carol", CONV_STATE_TIMEOUT_DISCONNECT, last_event_id="e2"
        )

        paused = log.list_paused_conversations()
        assert paused[0]["peer_id"] == "bob"
        assert paused[1]["peer_id"] == "carol"


# ---------------------------------------------------------------------------
# TestStateFileAtomicWrite：原子写与文件格式
# ---------------------------------------------------------------------------


class TestStateFileAtomicWrite:
    """状态文件原子写测试。"""

    def test_state_file_format(self, tmp_path: Path) -> None:
        """状态文件为合法 JSON，按 peer_id 索引。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="e1")

        state_file = tmp_path / "conversation_state.json"
        assert state_file.is_file()
        data = json.loads(state_file.read_text(encoding="utf-8"))
        assert "bob" in data
        assert data["bob"]["state"] == CONV_STATE_PAUSED

    def test_no_tmp_file_left(self, tmp_path: Path) -> None:
        """原子写后不留 .tmp 文件。"""
        log = _make_log(tmp_path)
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="e1")
        tmp_file = tmp_path / "conversation_state.tmp"
        assert not tmp_file.exists()

    def test_corrupted_state_file_returns_empty(self, tmp_path: Path) -> None:
        """损坏的状态文件不崩溃，返回空 dict。"""
        state_file = tmp_path / "conversation_state.json"
        state_file.write_text("not valid json", encoding="utf-8")
        # 重新创建 log 实例会尝试加载
        log = ConversationLog(tmp_path / "conversation.jsonl")
        assert log.get_conversation_state("bob") is None


# ---------------------------------------------------------------------------
# TestConcurrentStateAccess：并发安全
# ---------------------------------------------------------------------------


class TestConcurrentStateAccess:
    """多线程并发访问状态测试。"""

    def test_concurrent_set_different_peers(self, tmp_path: Path) -> None:
        """多线程并发设置不同 peer 的状态，最终一致。"""
        log = _make_log(tmp_path)
        peers = [f"peer_{i}" for i in range(10)]
        threads: list[threading.Thread] = []

        def set_state(peer_id: str) -> None:
            log.set_conversation_state(
                peer_id, CONV_STATE_PAUSED, last_event_id=f"evt-{peer_id}"
            )

        for p in peers:
            t = threading.Thread(target=set_state, args=(p,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        for p in peers:
            state = log.get_conversation_state(p)
            assert state is not None
            assert state["state"] == CONV_STATE_PAUSED

    def test_concurrent_set_same_peer(self, tmp_path: Path) -> None:
        """多线程并发设置同一 peer 的状态，不崩溃且最终有值。"""
        log = _make_log(tmp_path)
        threads: list[threading.Thread] = []

        def set_state(i: int) -> None:
            log.set_conversation_state(
                "bob", CONV_STATE_PAUSED, last_event_id=f"evt-{i}"
            )

        for i in range(20):
            t = threading.Thread(target=set_state, args=(i,))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()

        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_PAUSED


# ---------------------------------------------------------------------------
# TestBackwardCompatibility：向后兼容
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """向后兼容测试：旧数据缺失 conversation_state 字段。"""

    def test_old_event_without_state_field(self, tmp_path: Path) -> None:
        """旧 JSONL 数据缺失 conversation_state → 默认 active。"""
        log_path = tmp_path / "conversation.jsonl"
        # 手动写入一条不含 conversation_state 的旧格式数据
        old_data = {
            "event_id": "old-evt-1",
            "event_type": EVENT_ASK,
            "peer_id": "bob",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "vector_clock": {},
            "payload": {},
            "in_reply_to": "",
            "degraded": False,
            "realtime": False,
            "based_on": "",
            "snapshot_stale": False,
            # 注意：没有 conversation_state 字段
        }
        with log_path.open("w", encoding="utf-8") as f:
            f.write(json.dumps(old_data) + "\n")

        log = ConversationLog(log_path)
        events = log.load_all()
        assert len(events) == 1
        assert events[0].conversation_state == CONV_STATE_ACTIVE

    def test_new_event_serializes_state(self, tmp_path: Path) -> None:
        """新事件序列化时包含 conversation_state 字段。"""
        log = _make_log(tmp_path)
        event = _make_event(
            event_id="evt-1", conversation_state=CONV_STATE_PAUSED
        )
        log.append(event)

        # 读取原始 JSONL 验证字段存在
        with log.log_path.open("r", encoding="utf-8") as f:
            data = json.loads(f.readline())
        assert "conversation_state" in data
        assert data["conversation_state"] == CONV_STATE_PAUSED


# ---------------------------------------------------------------------------
# TestResumeConversation：基于 in_reply_to 链重建上下文
# ---------------------------------------------------------------------------


class TestResumeConversation:
    """resume_conversation 基于 in_reply_to 链重建上下文测试。"""

    def test_resume_paused_returns_thread(self, tmp_path: Path) -> None:
        """paused 状态的对话恢复时返回完整回复链。"""
        log = _make_log(tmp_path)

        # 构建回复链：ask1 → answer1 → ask2(in_reply_to=answer1) → answer2(in_reply_to=ask2)
        ask1 = _make_event(event_id="ask1", peer_id="bob")
        log.append(ask1)
        answer1 = _make_event(
            event_id="answer1",
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            in_reply_to="ask1",
        )
        log.append(answer1)
        ask2 = _make_event(
            event_id="ask2", peer_id="bob", in_reply_to="answer1"
        )
        log.append(ask2)
        answer2 = _make_event(
            event_id="answer2",
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            in_reply_to="ask2",
        )
        log.append(answer2)

        # 标记为 paused，last_event_id 指向 answer2
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="answer2", reason="offline"
        )

        events = log.resume_conversation("bob")
        # 应返回完整回复链（4 条事件）
        assert len(events) == 4
        event_ids = {e.event_id for e in events}
        assert event_ids == {"ask1", "answer1", "ask2", "answer2"}
        # 按时间排序
        timestamps = [e.timestamp for e in events]
        assert timestamps == sorted(timestamps)

    def test_resume_marks_resumed(self, tmp_path: Path) -> None:
        """恢复后状态标记为 resumed。"""
        log = _make_log(tmp_path)
        event = _make_event(event_id="evt-1", peer_id="bob")
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1"
        )

        log.resume_conversation("bob")
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_RESUMED

    def test_resume_timeout_disconnect(self, tmp_path: Path) -> None:
        """timeout_disconnect 状态也可恢复。"""
        log = _make_log(tmp_path)
        event = _make_event(event_id="evt-1", peer_id="bob")
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_TIMEOUT_DISCONNECT, last_event_id="evt-1"
        )

        events = log.resume_conversation("bob")
        assert len(events) == 1
        assert events[0].event_id == "evt-1"
        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_RESUMED

    def test_resume_active_returns_empty(self, tmp_path: Path) -> None:
        """active 状态无需恢复，返回空列表。"""
        log = _make_log(tmp_path)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="evt-1"
        )
        assert log.resume_conversation("bob") == []

    def test_resume_no_state_returns_empty(self, tmp_path: Path) -> None:
        """无状态记录时返回空列表。"""
        log = _make_log(tmp_path)
        assert log.resume_conversation("bob") == []

    def test_resume_resumed_returns_empty(self, tmp_path: Path) -> None:
        """已 resumed 状态不再恢复，返回空列表。"""
        log = _make_log(tmp_path)
        log.set_conversation_state(
            "bob", CONV_STATE_RESUMED, last_event_id="evt-1"
        )
        assert log.resume_conversation("bob") == []

    def test_resume_without_last_event_id_fallback(self, tmp_path: Path) -> None:
        """无 last_event_id 时退化到按 peer 加载全部事件。"""
        log = _make_log(tmp_path)
        e1 = _make_event(event_id="e1", peer_id="bob")
        e2 = _make_event(event_id="e2", peer_id="carol")
        log.append(e1)
        log.append(e2)
        # paused 状态但不带 last_event_id
        log.set_conversation_state("bob", CONV_STATE_PAUSED, last_event_id="")

        events = log.resume_conversation("bob")
        # 退化到 load_by_peer("bob")
        assert len(events) == 1
        assert events[0].event_id == "e1"

    def test_resume_partial_thread(self, tmp_path: Path) -> None:
        """恢复时基于链中间节点也能重建完整链。"""
        log = _make_log(tmp_path)
        # 链：root → mid → leaf
        root = _make_event(event_id="root", peer_id="bob")
        log.append(root)
        mid = _make_event(event_id="mid", peer_id="bob", in_reply_to="root")
        log.append(mid)
        leaf = _make_event(event_id="leaf", peer_id="bob", in_reply_to="mid")
        log.append(leaf)

        # 标记 paused，last_event_id 指向中间节点 mid
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="mid"
        )

        events = log.resume_conversation("bob")
        # load_thread 从 mid 出发，向前找 root，向后找 leaf
        assert len(events) == 3
        assert {e.event_id for e in events} == {"root", "mid", "leaf"}


# ---------------------------------------------------------------------------
# TestPeerCommOfflineMarksPaused：PeerComm 离线标记 paused
# ---------------------------------------------------------------------------


class TestPeerCommOfflineMarksPaused:
    """PeerComm._offline_ask 标记对话为 paused 测试。"""

    def test_offline_ask_marks_paused(self, tmp_path: Path) -> None:
        """peer 离线时 ask_peer 标记对话为 paused。"""
        transport = StubTransport(reachable_peers=set())  # bob 不可达
        comm, _, shadow, log = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "如何处理 X？")

        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_PAUSED
        assert state["reason"] == "peer_offline"
        # last_event_id 指向 ask 事件
        assert state["last_event_id"] != ""

    def test_online_ask_marks_active(self, tmp_path: Path) -> None:
        """peer 在线时 ask_peer 标记对话为 active。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        comm, _, _, log = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("bob", "如何处理 X？")

        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_ACTIVE
        assert state["reason"] == "realtime_active"

    def test_resume_via_peer_comm(self, tmp_path: Path) -> None:
        """PeerComm.resume_conversation 委托 ConversationLog。"""
        transport = StubTransport(reachable_peers=set())
        comm, _, _, log = _make_comm(tmp_path, transport=transport)

        # 先离线 ask 标记 paused
        comm.ask_peer("bob", "问题")
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_PAUSED

        # 通过 PeerComm 恢复
        events = comm.resume_conversation("bob")
        assert len(events) >= 1
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_RESUMED


# ---------------------------------------------------------------------------
# TestDaemonSessionTimeoutCheck：daemon 超时检测
# ---------------------------------------------------------------------------


class TestDaemonSessionTimeoutCheck:
    """daemon 超时检测标记 timeout_disconnect 测试。"""

    def test_timeout_marks_timeout_disconnect(self, tmp_path: Path) -> None:
        """active 对话超过 timeout → 标记 timeout_disconnect。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 创建一个很久以前的事件
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        event = _make_event(
            event_id="old-evt", peer_id="bob", timestamp=old_time
        )
        log.append(event)
        # 标记为 active
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="old-evt"
        )

        # 运行超时检测（timeout=600s，事件在 900s 前 → 超时）
        daemon._run_session_timeout_check(600)

        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_TIMEOUT_DISCONNECT

    def test_recent_active_not_marked(self, tmp_path: Path) -> None:
        """active 对话在 timeout 内 → 不标记。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 最近的事件（100s 前）
        recent_time = (datetime.now(timezone.utc) - timedelta(seconds=100)).isoformat()
        event = _make_event(
            event_id="recent-evt", peer_id="bob", timestamp=recent_time
        )
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="recent-evt"
        )

        daemon._run_session_timeout_check(600)

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_ACTIVE

    def test_no_active_conversations_no_error(self, tmp_path: Path) -> None:
        """无 active 对话时不报错。"""
        daemon = _make_daemon(tmp_path)
        _inject_daemon_components(daemon, tmp_path)

        daemon._run_session_timeout_check(600)

    def test_paused_not_checked(self, tmp_path: Path) -> None:
        """paused 状态的对话不参与超时检测。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        old_time = (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat()
        event = _make_event(
            event_id="old-evt", peer_id="bob", timestamp=old_time
        )
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="old-evt"
        )

        daemon._run_session_timeout_check(600)

        # paused 状态不变
        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_PAUSED

    def test_realtime_session_cleanup_uses_config_timeout(self, tmp_path: Path) -> None:
        """_run_realtime_session_cleanup 使用 config 中的 realtime_session_timeout。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 700s 前的事件（超过默认 600s）
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=700)).isoformat()
        event = _make_event(
            event_id="old-evt", peer_id="bob", timestamp=old_time
        )
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="old-evt"
        )

        daemon._run_realtime_session_cleanup()

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_TIMEOUT_DISCONNECT

    def test_custom_timeout_from_config(self, tmp_path: Path) -> None:
        """自定义 realtime_session_timeout 生效。"""
        config = ClientConfig(
            repo_root=str(tmp_path),
            member_id="alice",
            async_comm={"realtime_session_timeout": 100},
        )
        daemon = ClientDaemon(config)
        components = _inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 150s 前的事件（超过自定义 100s 但小于默认 600s）
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=150)).isoformat()
        event = _make_event(
            event_id="evt", peer_id="bob", timestamp=old_time
        )
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="evt"
        )

        daemon._run_realtime_session_cleanup()

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_TIMEOUT_DISCONNECT


# ---------------------------------------------------------------------------
# TestDaemonOnlineSyncResume：peer 上线自动恢复
# ---------------------------------------------------------------------------


class TestDaemonOnlineSyncResume:
    """daemon peer 上线自动恢复对话测试。"""

    def test_online_sync_resumes_paused_conversation(self, tmp_path: Path) -> None:
        """peer offline → online 时自动恢复 paused 对话。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        components = _inject_daemon_components(daemon, tmp_path, transport=transport)
        log: ConversationLog = components["conversation_log"]

        # 预置一条 paused 对话
        event = _make_event(event_id="evt-1", peer_id="bob")
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1"
        )

        # 模拟 bob 从离线变在线
        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": True}

        daemon._run_online_sync()

        # 对话应被恢复，状态变为 resumed
        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_RESUMED
        assert daemon._online_sync_task.last_status == "ok"

    def test_online_sync_resumes_timeout_disconnect(self, tmp_path: Path) -> None:
        """peer offline → online 时自动恢复 timeout_disconnect 对话。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        components = _inject_daemon_components(daemon, tmp_path, transport=transport)
        log: ConversationLog = components["conversation_log"]

        event = _make_event(event_id="evt-1", peer_id="bob")
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_TIMEOUT_DISCONNECT, last_event_id="evt-1"
        )

        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": True}

        daemon._run_online_sync()

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_RESUMED

    def test_online_sync_no_paused_no_resume(self, tmp_path: Path) -> None:
        """peer 上线但无 paused 对话 → 正常完成，无恢复。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        components = _inject_daemon_components(daemon, tmp_path, transport=transport)
        log: ConversationLog = components["conversation_log"]

        # 无 paused 对话
        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": True}

        daemon._run_online_sync()

        assert daemon._online_sync_task.last_status == "ok"
        # 无状态记录
        assert log.get_conversation_state("bob") is None

    def test_online_sync_stays_offline_no_resume(self, tmp_path: Path) -> None:
        """peer 持续离线 → 不触发恢复。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers=set())
        components = _inject_daemon_components(daemon, tmp_path, transport=transport)
        log: ConversationLog = components["conversation_log"]

        event = _make_event(event_id="evt-1", peer_id="bob")
        log.append(event)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="evt-1"
        )

        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": False}

        daemon._run_online_sync()

        # 仍为 paused
        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_PAUSED


# ---------------------------------------------------------------------------
# TestStateFlowIntegration：状态流转集成测试
# ---------------------------------------------------------------------------


class TestStateFlowIntegration:
    """完整状态流转测试：active → paused → resumed → active。"""

    def test_full_flow_active_paused_resumed_active(self, tmp_path: Path) -> None:
        """完整状态流转：active → paused → resumed → active。"""
        log = _make_log(tmp_path)

        # 1. active：在线交互
        e1 = _make_event(event_id="e1", peer_id="bob")
        log.append(e1)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="e1", reason="realtime"
        )
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_ACTIVE

        # 2. paused：peer 下线
        e2 = _make_event(event_id="e2", peer_id="bob", in_reply_to="e1")
        log.append(e2)
        log.set_conversation_state(
            "bob", CONV_STATE_PAUSED, last_event_id="e2", reason="peer_offline"
        )
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_PAUSED
        assert "bob" in {e["peer_id"] for e in log.list_paused_conversations()}

        # 3. resumed：peer 上线恢复
        events = log.resume_conversation("bob")
        assert len(events) >= 2  # e1 和 e2 都在链中
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_RESUMED
        assert "bob" not in {e["peer_id"] for e in log.list_paused_conversations()}

        # 4. active：恢复后继续对话
        e3 = _make_event(event_id="e3", peer_id="bob", in_reply_to="e2")
        log.append(e3)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="e3", reason="realtime"
        )
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_ACTIVE

    def test_full_flow_active_timeout_resumed(self, tmp_path: Path) -> None:
        """完整状态流转：active → timeout_disconnect → resumed。"""
        log = _make_log(tmp_path)

        # 1. active
        e1 = _make_event(event_id="e1", peer_id="bob")
        log.append(e1)
        log.set_conversation_state(
            "bob", CONV_STATE_ACTIVE, last_event_id="e1"
        )

        # 2. timeout_disconnect
        log.set_conversation_state(
            "bob",
            CONV_STATE_TIMEOUT_DISCONNECT,
            last_event_id="e1",
            reason="session_timeout",
        )
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_TIMEOUT_DISCONNECT
        assert "bob" in {e["peer_id"] for e in log.list_paused_conversations()}

        # 3. resumed
        events = log.resume_conversation("bob")
        assert len(events) == 1
        assert events[0].event_id == "e1"
        assert log.get_conversation_state("bob")["state"] == CONV_STATE_RESUMED


# ---------------------------------------------------------------------------
# TestDaemonRealtimeSessionInterval：daemon interval 从 config 读取
# ---------------------------------------------------------------------------


class TestDaemonRealtimeSessionInterval:
    """daemon _realtime_session_task interval 从 config 读取测试。"""

    def test_default_interval(self, tmp_path: Path) -> None:
        """默认 interval 为 DEFAULT_REALTIME_SESSION_TIMEOUT（600）。"""
        daemon = _make_daemon(tmp_path)
        assert daemon._realtime_session_task.interval_seconds == 600

    def test_custom_interval(self, tmp_path: Path) -> None:
        """自定义 realtime_session_timeout 生效到 task interval。"""
        config = ClientConfig(
            repo_root=str(tmp_path),
            member_id="alice",
            async_comm={"realtime_session_timeout": 300},
        )
        daemon = ClientDaemon(config)
        assert daemon._realtime_session_task.interval_seconds == 300
