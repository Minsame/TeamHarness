"""ClientDaemon 测试（SubTask 6.7 + 6.11）。

覆盖：
- TaskState should_run 逻辑
- Daemon start/stop 生命周期
- trigger_network_check（在线 / 离线）
- trigger_adoption_flush（在线 flush / 离线跳过）
- trigger_distill（注入回调 / noop 默认）
- status 快照
- is_online 查询
- 异常隔离（单个任务失败不影响其他）
- noop_distill 默认行为
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from server.client.adoption import AdoptionReporter
from server.client.config import ClientConfig
from server.client.daemon import (
    ClientDaemon,
    DaemonStatus,
    TaskState,
    noop_distill,
)
from server.client.recall_client import NetworkStatus, RecallClient
from server.common.models import AssetType, Scope
from server.client.working_copy import WorkingCopy


# ---------------------------------------------------------------------------
# TaskState
# ---------------------------------------------------------------------------


def test_task_state_initial_state():
    t = TaskState(name="test", interval_seconds=60)
    assert t.last_status == "pending"
    assert t.run_count == 0
    assert t.error_count == 0
    assert t.last_run_at == 0.0


def test_task_state_should_run_initially():
    t = TaskState(name="test", interval_seconds=60)
    assert t.should_run(time.time()) is True


def test_task_state_should_run_after_interval():
    t = TaskState(name="test", interval_seconds=1)
    t.last_run_at = time.time()
    t.last_status = "ok"
    # 立即检查不应运行（间隔未到）
    assert t.should_run(time.time()) is False
    # 等待间隔过后应运行
    time.sleep(1.1)
    assert t.should_run(time.time()) is True


def test_task_state_should_not_run_when_running():
    t = TaskState(name="test", interval_seconds=1)
    t.last_status = "running"
    t.last_run_at = 0.0  # 很久以前
    assert t.should_run(time.time()) is False


# ---------------------------------------------------------------------------
# noop_distill
# ---------------------------------------------------------------------------


def test_noop_distill_returns_zero_production():
    cfg = ClientConfig(repo_root="/tmp")
    result = noop_distill(cfg)
    assert result["produced"] == 0
    assert result["skipped"] == 0
    assert "error" in result


# ---------------------------------------------------------------------------
# ClientDaemon 生命周期
# ---------------------------------------------------------------------------


@pytest.fixture
def daemon_config(tmp_path: Path) -> ClientConfig:
    """离线配置（无 server_url），用于守护进程测试。"""
    return ClientConfig(
        repo_root=str(tmp_path),
        agent_id="agent-1",
        member_id="alice",
        network_check_interval_seconds=1,
        adoption_flush_interval_seconds=1,
    )


def test_daemon_start_stop(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    assert daemon.is_running is False
    daemon.start()
    assert daemon.is_running is True
    time.sleep(0.2)
    daemon.stop(timeout=2.0)
    assert daemon.is_running is False


def test_daemon_start_idempotent(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.start()
    # 再次 start 不应创建新线程
    thread1 = daemon._thread
    daemon.start()
    assert daemon._thread is thread1
    daemon.stop(timeout=2.0)


def test_daemon_stop_without_start(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    # stop 未启动的守护进程不应报错
    daemon.stop(timeout=1.0)
    assert daemon.is_running is False


# ---------------------------------------------------------------------------
# 网络检测任务
# ---------------------------------------------------------------------------


def test_trigger_network_check_offline(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.trigger_network_check()
    status = daemon.get_network_status()
    assert status is not None
    assert status.online is False
    assert "server_url" in (status.error or "")


def test_trigger_network_check_task_state_updated(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.trigger_network_check()
    assert daemon._network_task.last_status == "ok"
    assert daemon._network_task.run_count == 1


def test_is_online_offline(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    # 触发检测后 → 离线
    daemon.trigger_network_check()
    assert daemon.is_online() is False


# ---------------------------------------------------------------------------
# 采纳率 flush 任务
# ---------------------------------------------------------------------------


def test_trigger_adoption_flush_offline_skips(daemon_config: ClientConfig):
    """离线时采纳率 flush 应跳过（保留本地缓存）。"""
    reporter = AdoptionReporter(daemon_config)
    reporter.record_recall(asset_id="rule-1")
    daemon = ClientDaemon(daemon_config, adoption_reporter=reporter)
    # 先检测网络（离线）
    daemon.trigger_network_check()
    daemon.trigger_adoption_flush()
    # 离线跳过 → 本地缓存保留
    assert reporter.pending_count() == 1
    assert daemon._adoption_task.last_status == "ok"


def test_trigger_adoption_flush_online(daemon_config: ClientConfig, tmp_path: Path):
    """在线时采纳率 flush 应上报。"""
    # 构造在线配置 + mock recall client
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        agent_id="agent-1",
    )

    # mock recall_client 返回在线
    class MockRecallClient(RecallClient):
        def __init__(self, config):
            super().__init__(config)
            self._cached_network = NetworkStatus(online=True)

        def check_network(self, *, force: bool = False) -> NetworkStatus:
            return NetworkStatus(online=True, latency_ms=10)

    reporter = AdoptionReporter(cfg)
    reporter.record_recall(asset_id="rule-1")

    # mock flush 返回成功
    class MockReporter(AdoptionReporter):
        def flush(self, *, online=None, max_batches=10):
            from server.client.adoption import FlushResult
            return FlushResult(flushed=1, retained=0)

    mock_reporter = MockReporter(cfg)
    mock_reporter.record_recall(asset_id="rule-1")

    recall = MockRecallClient(cfg)
    daemon = ClientDaemon(cfg, recall_client=recall, adoption_reporter=mock_reporter)
    daemon.trigger_adoption_flush()
    assert daemon._adoption_task.last_status == "ok"
    assert daemon._adoption_task.run_count == 1


def test_trigger_adoption_flush_force_when_offline(daemon_config: ClientConfig):
    """force=True 时即使离线也尝试 flush。"""
    reporter = AdoptionReporter(daemon_config)
    reporter.record_recall(asset_id="rule-1")
    daemon = ClientDaemon(daemon_config, adoption_reporter=reporter)
    daemon.trigger_network_check()  # 离线
    daemon.trigger_adoption_flush(force=True)
    # 强制 flush → 走 offline 路径，retained 保留
    assert daemon._adoption_task.last_status in ("ok", "error")


# ---------------------------------------------------------------------------
# 一级提炼任务
# ---------------------------------------------------------------------------


def test_trigger_distill_with_callback(daemon_config: ClientConfig):
    called = []

    def custom_distill(config: ClientConfig) -> dict[str, Any]:
        called.append(config)
        return {"produced": 3, "skipped": 1, "error": None}

    daemon = ClientDaemon(daemon_config, distill_callback=custom_distill)
    daemon.trigger_distill()
    assert len(called) == 1
    assert daemon._distill_task.last_status == "ok"
    assert daemon._distill_task.run_count == 1


def test_trigger_distill_noop_default(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.trigger_distill()
    # noop_distill 返回 error → last_status = error
    assert daemon._distill_task.last_status == "error"
    assert daemon._distill_task.run_count == 1


def test_trigger_distill_callback_exception_isolated(daemon_config: ClientConfig):
    def failing_distill(config: ClientConfig) -> dict[str, Any]:
        raise RuntimeError("distill crashed")

    daemon = ClientDaemon(daemon_config, distill_callback=failing_distill)
    # 异常不应抛出，应被捕获
    daemon.trigger_distill()
    assert daemon._distill_task.last_status == "error"
    assert "distill crashed" in daemon._distill_task.last_error
    assert daemon._distill_task.error_count == 1


# ---------------------------------------------------------------------------
# status 快照
# ---------------------------------------------------------------------------


def test_status_snapshot(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.start()
    try:
        time.sleep(0.2)
        snap = daemon.status()
        assert isinstance(snap, DaemonStatus)
        assert snap.running is True
        assert snap.started_at != ""
        assert snap.uptime_seconds >= 0
        assert "network_check" in snap.tasks
        assert "adoption_flush" in snap.tasks
        assert "distill" in snap.tasks
    finally:
        daemon.stop(timeout=2.0)


def test_status_not_started(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    snap = daemon.status()
    assert snap.running is False
    assert snap.started_at == ""
    assert snap.uptime_seconds == 0.0


def test_status_includes_network_info(daemon_config: ClientConfig):
    daemon = ClientDaemon(daemon_config)
    daemon.trigger_network_check()
    snap = daemon.status()
    assert "online" in snap.network
    assert "latency_ms" in snap.network
    assert snap.network["online"] is False


# ---------------------------------------------------------------------------
# 异常隔离
# ---------------------------------------------------------------------------


def test_network_check_exception_does_not_crash_daemon(daemon_config: ClientConfig):
    """网络检测异常不应导致守护进程崩溃。"""
    class FailingRecallClient(RecallClient):
        def check_network(self, *, force: bool = False) -> NetworkStatus:
            raise RuntimeError("network check crashed")

    recall = FailingRecallClient(daemon_config)
    daemon = ClientDaemon(daemon_config, recall_client=recall)
    daemon.start()
    try:
        time.sleep(0.5)
        # 守护进程仍应运行
        assert daemon.is_running is True
        # 网络任务状态为 error
        assert daemon._network_task.last_status == "error"
        assert daemon._network_task.error_count >= 1
    finally:
        daemon.stop(timeout=2.0)


def test_distill_exception_does_not_affect_other_tasks(daemon_config: ClientConfig):
    """提炼任务异常不影响网络检测与采纳率 flush。"""
    def failing_distill(config: ClientConfig) -> dict[str, Any]:
        raise RuntimeError("distill crashed")

    daemon = ClientDaemon(daemon_config, distill_callback=failing_distill)
    daemon.trigger_network_check()
    daemon.trigger_distill()
    daemon.trigger_adoption_flush()
    # 网络检测正常
    assert daemon._network_task.last_status == "ok"
    # 提炼失败
    assert daemon._distill_task.last_status == "error"
    # 采纳率 flush 不受影响（离线跳过，status=ok）
    assert daemon._adoption_task.last_status == "ok"


# ---------------------------------------------------------------------------
# 集成：后台线程运行
# ---------------------------------------------------------------------------


def test_daemon_background_loop_runs_tasks(daemon_config: ClientConfig):
    """守护进程后台运行时，任务应按周期执行。"""
    daemon = ClientDaemon(daemon_config)
    daemon.start()
    try:
        # 等待至少一次网络检测周期（interval=1s）
        time.sleep(1.5)
        assert daemon._network_task.run_count >= 1
    finally:
        daemon.stop(timeout=2.0)
