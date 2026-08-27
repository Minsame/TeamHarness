"""Task 22 测试：ClientDaemon 成员 AI 通信调度任务。

覆盖 5 个新增调度任务：
- peer 心跳检测（_run_peer_heartbeat）
- 实时通信会话清理（_run_realtime_session_cleanup）
- 影子联络状态检查（_run_shadow_comm_trigger）
- 上线同步（_run_online_sync）
- peer 快照刷新（_run_snapshot_refresh）

以及 status() 对新任务的包含性验证。

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录，
通过直接设置 daemon._peer_comm_components 注入 Stub 组件，
绕过懒初始化中的真实 transport 创建。
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.sync_protocol import SyncProtocol
from server.async_comm.types import ConversationEvent, VectorClock
from server.client.config import ClientConfig
from server.client.daemon import ClientDaemon
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - ``reachable_peers``：可达 peer 集合
    - ``discoverable_peers``：discover_peers 返回的 peer 列表（默认从 reachable_peers 推导）
    - ``fetch_responses``：按 peer_id 预置 fetch 返回消息
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        discoverable_peers: list[str] | None = None,
        fetch_responses: dict[str, list[Message]] | None = None,
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self._discoverable_peers = discoverable_peers
        self.fetch_responses = fetch_responses or {}
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.reachability_checks: list[str] = []
        self.fetch_calls: list[str] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(
            success=True,
            delivered_count=len(messages),
            delivered_message_ids=[m.event_id for m in messages],
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        self.fetch_calls.append(peer_id)
        return self.fetch_responses.get(peer_id, [])

    def is_peer_reachable(self, peer_id: str) -> bool:
        self.reachability_checks.append(peer_id)
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        if self._discoverable_peers is not None:
            return [PeerInfo(peer_id=p, online=True) for p in self._discoverable_peers]
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, *, member_id: str = "alice") -> ClientConfig:
    """构造测试用 ClientConfig（repo_root 指向 tmp_path，无 server_url）。"""
    return ClientConfig(
        repo_root=str(tmp_path),
        member_id=member_id,
        network_check_interval_seconds=60,
    )


def _make_daemon(
    tmp_path: Path,
    *,
    member_id: str = "alice",
) -> ClientDaemon:
    """构造测试用 ClientDaemon 实例。

    不注入 peer 通信组件（_peer_comm_components 保持 None），
    调用方按需通过 _inject_components 注入 Stub 组件。
    """
    config = _make_config(tmp_path, member_id=member_id)
    return ClientDaemon(config)


def _inject_components(
    daemon: ClientDaemon,
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    member_id: str = "alice",
) -> dict:
    """为 daemon 注入 Stub peer 通信组件，返回组件字典。

    组件存于 daemon._peer_comm_components，使 _build_peer_comm_components 直接返回缓存。
    """
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


def _make_event(
    *,
    event_id: str = "",
    event_type: str = "ask",
    peer_id: str = "bob",
    payload: dict | None = None,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        vector_clock=VectorClock(),
        payload=payload or {},
    )


# ---------------------------------------------------------------------------
# TestDaemonPeerHeartbeat
# ---------------------------------------------------------------------------


class TestDaemonPeerHeartbeat:
    """peer 心跳检测任务测试。"""

    def test_heartbeat_updates_peer_online_cache(self, tmp_path: Path) -> None:
        """心跳检测更新 peer_online_cache。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob", "carol"})
        _inject_components(daemon, tmp_path, transport=transport)

        daemon.trigger_peer_heartbeat()

        status = daemon.get_peer_online_status()
        assert status == {"bob": True, "carol": True}

    def test_heartbeat_records_offline_peers(self, tmp_path: Path) -> None:
        """心跳检测正确记录离线 peer。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(
            reachable_peers={"bob"},
            discoverable_peers=["bob", "dave"],
        )
        _inject_components(daemon, tmp_path, transport=transport)

        daemon.trigger_peer_heartbeat()

        status = daemon.get_peer_online_status()
        assert status == {"bob": True, "dave": False}

    def test_heartbeat_status_change_logged(self, tmp_path: Path, caplog) -> None:
        """在线状态变化记录日志。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        _inject_components(daemon, tmp_path, transport=transport)

        # 模拟上一次心跳结果：bob 离线
        # _run_peer_heartbeat 会将 _peer_online_cache 复制到 _previous_peer_online_cache，
        # 然后用新探测结果覆盖 _peer_online_cache，对比新旧缓存记录状态变化
        daemon._peer_online_cache = {"bob": False}

        import logging

        with caplog.at_level(logging.INFO, logger="server.client.daemon"):
            daemon.trigger_peer_heartbeat()

        # 验证日志中包含状态切换信息
        assert any(
            "bob" in record.message and "状态切换" in record.message
            for record in caplog.records
        ), f"未找到状态切换日志，records: {[r.message for r in caplog.records]}"

    def test_heartbeat_no_peer_no_error(self, tmp_path: Path) -> None:
        """无 peer 时不报错。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers=set())
        _inject_components(daemon, tmp_path, transport=transport)

        # 不应抛异常
        daemon.trigger_peer_heartbeat()

        assert daemon.get_peer_online_status() == {}
        # 任务状态应为 ok
        assert daemon._peer_heartbeat_task.last_status == "ok"

    def test_trigger_peer_heartbeat_manual(self, tmp_path: Path) -> None:
        """trigger_peer_heartbeat 手动触发。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        _inject_components(daemon, tmp_path, transport=transport)

        daemon.trigger_peer_heartbeat()
        assert daemon._peer_heartbeat_task.run_count == 1
        assert daemon._peer_heartbeat_task.last_status == "ok"

        # 再次触发，run_count 递增
        daemon.trigger_peer_heartbeat()
        assert daemon._peer_heartbeat_task.run_count == 2


# ---------------------------------------------------------------------------
# TestDaemonRealtimeSessionCleanup
# ---------------------------------------------------------------------------


class TestDaemonRealtimeSessionCleanup:
    """实时通信会话清理任务测试。"""

    def test_cleanup_expired_sessions(self, tmp_path: Path) -> None:
        """清理超时会话。"""
        daemon = _make_daemon(tmp_path)
        now = time.time()

        # 添加一个超时会话（700s 前）和一个未超时会话（100s 前）
        daemon._active_sessions["session-old"] = now - 700
        daemon._active_sessions["session-fresh"] = now - 100

        daemon._run_realtime_session_cleanup()

        # 超时的被清理，未超时的保留
        assert "session-old" not in daemon._active_sessions
        assert "session-fresh" in daemon._active_sessions
        assert daemon._realtime_session_task.last_status == "ok"

    def test_cleanup_keeps_unexpired_sessions(self, tmp_path: Path) -> None:
        """未超时会话保留。"""
        daemon = _make_daemon(tmp_path)
        now = time.time()

        daemon._active_sessions["s1"] = now - 50
        daemon._active_sessions["s2"] = now - 200
        daemon._active_sessions["s3"] = now - 599  # 刚好未超时

        daemon._run_realtime_session_cleanup()

        assert len(daemon._active_sessions) == 3

    def test_cleanup_no_session_no_error(self, tmp_path: Path) -> None:
        """无会话时不报错。"""
        daemon = _make_daemon(tmp_path)

        daemon._run_realtime_session_cleanup()

        assert daemon._active_sessions == {}
        assert daemon._realtime_session_task.last_status == "ok"
        assert daemon._realtime_session_task.run_count == 1


# ---------------------------------------------------------------------------
# TestDaemonShadowCommTrigger
# ---------------------------------------------------------------------------


class TestDaemonShadowCommTrigger:
    """影子联络状态检查任务测试。"""

    def test_pending_delivery_positive_logs(self, tmp_path: Path, caplog) -> None:
        """pending_delivery > 0 时记录日志。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_components(daemon, tmp_path)
        mailbox: Mailbox = components["mailbox"]

        # 向 outbox 写入 2 条 pending_delivery 消息
        mailbox.append_outbox(_make_event(peer_id="bob"))
        mailbox.append_outbox(_make_event(peer_id="carol"))

        import logging

        with caplog.at_level(logging.INFO, logger="server.client.daemon"):
            daemon._run_shadow_comm_trigger()

        assert any(
            "影子联络待投递消息" in record.message and "2" in record.message
            for record in caplog.records
        ), f"未找到待投递日志，records: {[r.message for r in caplog.records]}"
        assert daemon._shadow_comm_task.last_status == "ok"

    def test_pending_delivery_zero_returns_normally(self, tmp_path: Path) -> None:
        """pending_delivery = 0 时正常返回。"""
        daemon = _make_daemon(tmp_path)
        _inject_components(daemon, tmp_path)

        # mailbox 为空，pending_delivery_count = 0
        daemon._run_shadow_comm_trigger()

        assert daemon._shadow_comm_task.last_status == "ok"
        assert daemon._shadow_comm_task.run_count == 1


# ---------------------------------------------------------------------------
# TestDaemonOnlineSync
# ---------------------------------------------------------------------------


class TestDaemonOnlineSync:
    """上线同步任务测试。"""

    def test_offline_to_online_triggers_sync(self, tmp_path: Path) -> None:
        """peer offline → online 触发同步。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        components = _inject_components(daemon, tmp_path, transport=transport)

        # 模拟上一次 bob 离线，当前 bob 在线
        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": True}

        daemon._run_online_sync()

        # sync_with_peer 调用了 transport.fetch(peer_id)
        assert "bob" in transport.fetch_calls
        assert daemon._online_sync_task.last_status == "ok"

    def test_no_status_change_no_sync(self, tmp_path: Path) -> None:
        """peer 无变化时不触发同步。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        _inject_components(daemon, tmp_path, transport=transport)

        # 两次缓存一致 → 无变化
        daemon._previous_peer_online_cache = {"bob": True}
        daemon._peer_online_cache = {"bob": True}

        daemon._run_online_sync()

        # sync_with_peer 不应被调用 → fetch_calls 为空
        assert transport.fetch_calls == []
        assert daemon._online_sync_task.last_status == "ok"

    def test_stays_offline_no_sync(self, tmp_path: Path) -> None:
        """peer 持续离线时不触发同步。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers=set())
        _inject_components(daemon, tmp_path, transport=transport)

        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": False}

        daemon._run_online_sync()

        assert transport.fetch_calls == []

    def test_trigger_online_sync_manual(self, tmp_path: Path) -> None:
        """trigger_online_sync 手动触发。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        _inject_components(daemon, tmp_path, transport=transport)

        daemon._previous_peer_online_cache = {"bob": False}
        daemon._peer_online_cache = {"bob": True}

        daemon.trigger_online_sync()

        assert daemon._online_sync_task.run_count == 1
        assert "bob" in transport.fetch_calls


# ---------------------------------------------------------------------------
# TestDaemonSnapshotRefresh
# ---------------------------------------------------------------------------


class TestDaemonSnapshotRefresh:
    """peer 快照刷新任务测试。"""

    def test_expire_stale_snapshots(self, tmp_path: Path) -> None:
        """清理过期快照。"""
        daemon = _make_daemon(tmp_path)
        components = _inject_components(daemon, tmp_path)
        snapshot_mgr: PeerSnapshotManager = components["peer_snapshot_manager"]

        # 用 ttl_days=0 创建快照（立即过期）
        # 需要替换为 ttl_days=0 的 manager
        base_dir = tmp_path / "async_comm"
        stale_mgr = PeerSnapshotManager(base_dir, ttl_days=0)
        components["peer_snapshot_manager"] = stale_mgr

        # 创建 2 个快照（会立即过期，因为 ttl_days=0）
        stale_mgr.refresh_snapshot("bob")
        stale_mgr.refresh_snapshot("carol")

        assert len(stale_mgr.list_snapshots()) == 2

        daemon._run_snapshot_refresh()

        # 过期快照被清理
        assert stale_mgr.list_snapshots() == []
        assert daemon._snapshot_refresh_task.last_status == "ok"

    def test_no_snapshot_no_error(self, tmp_path: Path) -> None:
        """无快照时不报错。"""
        daemon = _make_daemon(tmp_path)
        _inject_components(daemon, tmp_path)

        daemon._run_snapshot_refresh()

        assert daemon._snapshot_refresh_task.last_status == "ok"
        assert daemon._snapshot_refresh_task.run_count == 1


# ---------------------------------------------------------------------------
# TestDaemonStatus
# ---------------------------------------------------------------------------


class TestDaemonStatus:
    """status() 对新任务的包含性验证。"""

    def test_status_contains_all_new_tasks(self, tmp_path: Path) -> None:
        """status() 包含 5 个新任务。"""
        daemon = _make_daemon(tmp_path)

        status = daemon.status()
        task_names = set(status.tasks.keys())

        expected_new_tasks = {
            "peer_heartbeat",
            "realtime_session_cleanup",
            "shadow_comm_trigger",
            "online_sync",
            "snapshot_refresh",
        }
        assert expected_new_tasks.issubset(task_names), (
            f"缺少任务: {expected_new_tasks - task_names}"
        )

    def test_status_contains_all_eight_tasks(self, tmp_path: Path) -> None:
        """status() 包含全部 8 个任务（3 个原有 + 5 个新增）。"""
        daemon = _make_daemon(tmp_path)

        status = daemon.status()
        task_names = set(status.tasks.keys())

        expected_all = {
            "network_check",
            "adoption_flush",
            "distill",
            "peer_heartbeat",
            "realtime_session_cleanup",
            "shadow_comm_trigger",
            "online_sync",
            "snapshot_refresh",
        }
        assert task_names == expected_all, (
            f"任务集合不匹配，实际: {task_names}"
        )

    def test_task_status_recorded_correctly(self, tmp_path: Path) -> None:
        """任务状态正确记录（run_count / last_status）。"""
        daemon = _make_daemon(tmp_path)
        transport = StubTransport(reachable_peers={"bob"})
        _inject_components(daemon, tmp_path, transport=transport)

        # 触发一次心跳
        daemon.trigger_peer_heartbeat()

        status = daemon.status()
        heartbeat_status = status.tasks["peer_heartbeat"]

        assert heartbeat_status["last_status"] == "ok"
        assert heartbeat_status["run_count"] == 1
        assert heartbeat_status["error_count"] == 0
        assert heartbeat_status["last_run_at"] > 0

    def test_task_interval_correct(self, tmp_path: Path) -> None:
        """任务周期正确记录。"""
        daemon = _make_daemon(tmp_path)

        status = daemon.status()

        assert status.tasks["peer_heartbeat"]["interval_seconds"] == 60
        assert status.tasks["realtime_session_cleanup"]["interval_seconds"] == 600
        assert status.tasks["shadow_comm_trigger"]["interval_seconds"] == 60
        assert status.tasks["online_sync"]["interval_seconds"] == 60
        assert status.tasks["snapshot_refresh"]["interval_seconds"] == 3600
