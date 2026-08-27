"""对账任务：每小时补偿 embedding_id IS NULL 的资产。

对应 SubTask 2.6 + 缺陷 1.1 一致性窗口：
- asset_index 已提交但 embedding 延迟补偿（1 小时内）
- 扫描 embedding_id IS NULL 且 indexed_at 早于 1 小时前的资产
- 重新投递 embedding_task_queue（task_type=reindex）
- 失败重试由 outbox worker 的 max_retries 兜底
- 连续失败 → 记 metric 告警（不静默降级）

调用方式：定时任务每小时触发一次 run()。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from server.infra_db.asset_index import AssetIndex
from server.infra_db.db import Database
from server.infra_db.models import EmbeddingTaskQueue, IndexSyncState

logger = logging.getLogger(__name__)


@dataclass
class ReconcileEmbeddingResult:
    """对账结果。"""

    scanned: int = 0
    requeued: int = 0
    still_null: int = 0
    errors: list[str] = None

    def __post_init__(self):
        if self.errors is None:
            self.errors = []


class ReconcileEmbeddingTask:
    """补偿 embedding_id IS NULL 的资产。

    用法：
        task = ReconcileEmbeddingTask(db, asset_index, active_version="v1", shadow_version="")
        result = task.run(stale_seconds=3600)
    """

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
        *,
        active_version: str = "v1",
        shadow_version: str = "",
        max_requeue_per_run: int = 500,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._active_version = active_version
        self._shadow_version = shadow_version
        self._max_requeue = max_requeue_per_run

    def run(self, *, stale_seconds: int = 3600) -> ReconcileEmbeddingResult:
        """扫描 NULL embedding 的资产并重新投递 outbox 任务。

        stale_seconds：只补偿 indexed_at 早于 N 秒前的资产，避免与刚投递的任务争抢。
        """
        result = ReconcileEmbeddingResult()
        null_assets = self._asset_index.list_null_embedding(
            stale_seconds=stale_seconds, limit=self._max_requeue
        )
        result.scanned = len(null_assets)
        if not null_assets:
            return result

        # 复用 AssetIndex._enqueue_embedding_task：需要新 session + 重新 attach 行
        from datetime import timedelta
        from sqlalchemy import update as sa_update

        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            for row in null_assets:
                # 已存在 pending/in_progress 任务的不重复投递
                existing = sess.execute(
                    select(EmbeddingTaskQueue)
                    .where(EmbeddingTaskQueue.asset_id == row.id)
                    .where(EmbeddingTaskQueue.model_version == self._active_version)
                    .where(EmbeddingTaskQueue.status.in_(["pending", "in_progress"]))
                ).scalars().first()
                if existing is not None:
                    continue
                # 投递 reindex 任务
                task = EmbeddingTaskQueue(
                    asset_id=row.id,
                    task_type="reindex",
                    model_version=self._active_version,
                    status="pending",
                    retry_count=0,
                    max_retries=3,
                )
                sess.add(task)
                if self._shadow_version:
                    shadow_task = EmbeddingTaskQueue(
                        asset_id=row.id,
                        task_type="reindex",
                        model_version=self._shadow_version,
                        status="pending",
                        retry_count=0,
                        max_retries=3,
                    )
                    sess.add(shadow_task)
                result.requeued += 1

        # 统计仍为 NULL 的数量（用于 metric，二次查询，已投递的仍算 NULL 直到 worker 完成）
        result.still_null = len(
            self._asset_index.list_null_embedding(
                stale_seconds=stale_seconds, limit=self._max_requeue
            )
        )
        logger.info(
            "对账完成 scanned=%d requeued=%d still_null=%d",
            result.scanned,
            result.requeued,
            result.still_null,
        )
        return result


__all__ = [
    "ReconcileEmbeddingResult",
    "ReconcileEmbeddingTask",
]
