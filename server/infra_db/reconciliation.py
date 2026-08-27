"""reconciliation cron：每 5 分钟补偿 webhook 丢失。

对应 SubTask 2.5 + 缺陷 1.3 webhook 补偿：
- 每 5 分钟 git fetch → 读远端 HEAD commit SHA
- 比对 index_sync_state.last_synced_commit 与 HEAD
- 不一致 → 调用 SyncService.trigger_sync(HEAD)
- commit SHA 幂等：trigger_sync 内部已在 WebhookSyncHandler.sync_commit 检查
- 连续 3 周期滞后 → 触发告警（lag_periods 累加）
- webhook 全部丢失时，5 分钟内补同步

注意：HEAD 获取依赖 GitProvider 实现。
- GitLab/Gitea：通过 API 查询 default branch 的 HEAD
- libgit2：通过 pygit2 读 refs/heads/main
为简化，本类提供 head_resolver 注入点；默认实现通过 ls_tree("") 探测，
失败则用 "HEAD" 字符串（依赖 Provider 内部解析）。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select, text, update

from server.infra_db.db import Database
from server.infra_db.models import IndexSyncState

logger = logging.getLogger(__name__)

# HEAD 解析器签名：() → str（返回当前 HEAD commit SHA）
HeadResolver = Callable[[], str]


@dataclass
class ReconcileResult:
    """单次 reconciliation 结果。"""

    head_commit: str = ""
    last_synced_commit: str = ""
    triggered_sync: bool = False
    lag_periods: int = 0
    alert_lagging: bool = False  # 连续 3 周期滞后告警
    error: str | None = None


class ReconciliationCron:
    """reconciliation 定时任务。

    用法：
        cron = ReconciliationCron(db, sync_service, head_resolver)
        result = cron.run_once()  # 单次执行
        cron.start(interval_seconds=300)  # 后台每 5 分钟
        cron.stop()
    """

    LAG_ALERT_THRESHOLD = 3  # 连续 3 周期滞后触发告警
    DEFAULT_INTERVAL_SECONDS = 300  # 5 分钟

    def __init__(
        self,
        database: Database,
        sync_service: Any,  # SyncService，避免循环导入用 Any
        head_resolver: HeadResolver,
        *,
        lag_threshold: int = LAG_ALERT_THRESHOLD,
    ) -> None:
        self._db = database
        self._sync = sync_service
        self._head_resolver = head_resolver
        self._lag_threshold = lag_threshold
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 单次执行
    # ------------------------------------------------------------------

    def run_once(self) -> ReconcileResult:
        """执行一次 reconciliation。

        步骤：
        1. 读 index_sync_state.last_synced_commit
        2. 调 head_resolver 拿远端 HEAD
        3. 不一致 → trigger_sync(HEAD)
        4. 仍滞后 → lag_periods+1；连续 3 周期 → 告警
        """
        result = ReconcileResult()
        try:
            result.head_commit = self._head_resolver() or ""
        except Exception as exc:
            result.error = f"head_resolver 失败: {exc}"
            logger.exception("reconciliation head_resolver 失败")
            return result

        with self._db.session() as sess:
            state = sess.get(IndexSyncState, "singleton")
            if state is None:
                state = IndexSyncState(
                    id="singleton",
                    last_synced_commit="",
                    status="ok",
                    lag_periods=0,
                )
                sess.add(state)
            result.last_synced_commit = state.last_synced_commit

            if not result.head_commit:
                result.error = "head_commit 为空，跳过"
                return result

            if state.last_synced_commit == result.head_commit:
                # 已同步 → 重置 lag_periods
                state.lag_periods = 0
                state.status = "ok"
                result.lag_periods = 0
                return result

            # 滞后 → trigger_sync
            result.triggered_sync = True
            logger.info(
                "reconciliation 检测到滞后 last=%s head=%s，触发同步",
                state.last_synced_commit[:8],
                result.head_commit[:8],
            )

        # 在事务外触发 sync（trigger_sync 内部开启自己的事务）
        try:
            sync_result = self._sync.trigger_sync(result.head_commit)
            if sync_result.errors:
                result.error = ";".join(sync_result.errors)
                # sync 失败 → lag_periods + 1
                self._increment_lag_periods()
            else:
                # sync 成功 → lag_periods 清零
                self._reset_lag_periods()
        except Exception as exc:
            result.error = f"trigger_sync 失败: {exc}"
            logger.exception("reconciliation trigger_sync 失败")
            self._increment_lag_periods()

        # 检查是否触发告警
        result.lag_periods = self._read_lag_periods()
        if result.lag_periods >= self._lag_threshold:
            result.alert_lagging = True
            logger.error(
                "reconciliation 连续 %d 周期滞后，触发告警",
                result.lag_periods,
            )
        return result

    def _increment_lag_periods(self) -> None:
        with self._db.session() as sess:
            sess.execute(
                text(
                    "UPDATE index_sync_state SET lag_periods = lag_periods + 1, "
                    "status = 'lagging' WHERE id = 'singleton';"
                )
            )

    def _reset_lag_periods(self) -> None:
        with self._db.session() as sess:
            sess.execute(
                text(
                    "UPDATE index_sync_state SET lag_periods = 0, status = 'ok' "
                    "WHERE id = 'singleton';"
                )
            )

    def _read_lag_periods(self) -> int:
        with self._db.session() as sess:
            state = sess.get(IndexSyncState, "singleton")
            return state.lag_periods if state else 0

    # ------------------------------------------------------------------
    # 后台定时
    # ------------------------------------------------------------------

    def start(
        self, *, interval_seconds: int = DEFAULT_INTERVAL_SECONDS
    ) -> None:
        """启动后台定时任务（默认每 5 分钟）。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()

        def _loop() -> None:
            while not self._stop_flag.is_set():
                try:
                    self.run_once()
                except Exception:
                    logger.exception("reconciliation cron 异常")
                # 分段 sleep 以便快速响应 stop
                slept = 0
                while slept < interval_seconds and not self._stop_flag.is_set():
                    time.sleep(1)
                    slept += 1

        self._thread = threading.Thread(
            target=_loop, daemon=True, name="reconciliation-cron"
        )
        self._thread.start()

    def stop(self, *, timeout: float = 10.0) -> None:
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


__all__ = [
    "HeadResolver",
    "ReconcileResult",
    "ReconciliationCron",
]
