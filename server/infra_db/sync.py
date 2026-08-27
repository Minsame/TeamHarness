"""SyncService — DB 派生索引层同步入口（对外契约 API）。

整合 webhook 同步处理 + reconciliation + 对账任务，提供三契约方法：
- trigger_sync(commit_sha) → 触发同步（增量 / 全量）
- get_sync_status() → 返回 {last_synced_commit, last_synced_at, status, lag_periods}
- reconcile() → reconciliation 入口（git fetch + 比对 + trigger_sync）

依赖：
- WebhookSyncHandler：增量扫描 + embedding 计算
- ReconciliationCron：5 分钟补偿
- ReconcileEmbeddingTask：1 小时对账
- OutboxWorker：异步消费队列写向量库
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select

from server.infra_db.asset_index import AssetIndex
from server.infra_db.counts_check import CountsChecker
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import IndexSyncState
from server.infra_db.reconcile_embedding import ReconcileEmbeddingTask
from server.infra_db.reconciliation import (
    HeadResolver,
    ReconcileResult,
    ReconciliationCron,
)
from server.infra_db.webhook_sync import SyncResult, WebhookSyncHandler
from server.infra_git.git_provider import GitProvider

logger = logging.getLogger(__name__)


@dataclass
class SyncStatus:
    """同步状态快照（对外契约）。"""

    last_synced_commit: str
    last_synced_at: datetime | None
    status: str  # ok / syncing / error / lagging
    lag_periods: int
    last_error: str | None = None


class SyncService:
    """DB 派生索引层同步服务（对外契约 API）。

    用法：
        sync = SyncService(
            database=db,
            git_provider=git,
            asset_index=asset_index,
            embedding_service=emb,
            counts_checker=checker,
            repo_root=".",
        )
        result = sync.trigger_sync(commit_sha)
        status = sync.get_sync_status()
        reconcile_result = sync.reconcile(head_resolver=...)

    """

    def __init__(
        self,
        *,
        database: Database,
        git_provider: GitProvider,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        counts_checker: CountsChecker,
        repo_root: str = "",
        head_resolver: HeadResolver | None = None,
    ) -> None:
        self._db = database
        self._git = git_provider
        self._asset_index = asset_index
        self._embedding_service = embedding_service
        self._counts_checker = counts_checker
        self._repo_root = repo_root
        self._head_resolver = head_resolver

        self._sync_handler = WebhookSyncHandler(
            database=database,
            git_provider=git_provider,
            asset_index=asset_index,
            counts_checker=counts_checker,
            repo_root=repo_root,
        )

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def trigger_sync(self, commit_sha: str) -> SyncResult:
        """触发同步：增量扫描 + embedding 投递。

        幂等：commit_sha == last_synced_commit 时跳过。
        """
        # 设置 embedding 版本到 asset_index（保证 outbox 投递到正确版本）
        self._asset_index.set_embedding_versions(
            active=self._embedding_service.get_active_version(),
            shadow=self._embedding_service.get_shadow_version(),
        )
        return self._sync_handler.sync_commit(commit_sha)

    def get_sync_status(self) -> SyncStatus:
        """返回当前同步状态。"""
        with self._db.session() as sess:
            state = sess.get(IndexSyncState, "singleton")
            if state is None:
                return SyncStatus(
                    last_synced_commit="",
                    last_synced_at=None,
                    status="ok",
                    lag_periods=0,
                )
            return SyncStatus(
                last_synced_commit=state.last_synced_commit,
                last_synced_at=state.last_synced_at,
                status=state.status,
                lag_periods=state.lag_periods,
                last_error=state.last_error,
            )

    def reconcile(self, *, head_resolver: HeadResolver | None = None) -> ReconcileResult:
        """reconciliation 入口：比对 HEAD 与 last_synced_commit，不一致则补同步。

        head_resolver 不传时用构造时注入的 resolver。
        """
        resolver = head_resolver or self._head_resolver
        if resolver is None:
            raise ValueError(
                "reconciliation 需要 head_resolver（构造时传入或调用时显式提供）"
            )
        cron = ReconciliationCron(
            database=self._db,
            sync_service=self,
            head_resolver=resolver,
        )
        return cron.run_once()

    # ------------------------------------------------------------------
    # 便捷：执行对账任务（每小时补偿 NULL embedding）
    # ------------------------------------------------------------------

    def run_reconcile_embedding(
        self, *, stale_seconds: int = 3600
    ) -> "ReconcileEmbeddingResult":
        """执行对账任务（每小时补偿 embedding_id IS NULL）。"""
        task = ReconcileEmbeddingTask(
            database=self._db,
            asset_index=self._asset_index,
            active_version=self._embedding_service.get_active_version(),
            shadow_version=self._embedding_service.get_shadow_version(),
        )
        return task.run(stale_seconds=stale_seconds)

    # ------------------------------------------------------------------
    # 便捷：从 WebhookEvent 触发同步
    # ------------------------------------------------------------------

    def handle_webhook_event(self, event) -> SyncResult:
        """从 WebhookEvent 触发同步。

        event.after 为目标 commit SHA。
        event.before 为空（删除分支等）时跳过。
        """
        from server.common.models import WebhookEvent  # 局部导入避免循环

        if not isinstance(event, WebhookEvent):
            raise TypeError(f"期望 WebhookEvent，得到 {type(event)}")
        if not event.after or set(event.after) == {"0"}:
            return SyncResult(commit_sha="", skipped=True, skip_reason="after 为空，跳过")
        return self.trigger_sync(event.after)


__all__ = [
    "SyncService",
    "SyncStatus",
]
