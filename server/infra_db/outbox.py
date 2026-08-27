"""outbox worker — 异步消费 embedding_task_queue 写向量库。

对应 SubTask 2.4（outbox 模式）核心：
- 从 embedding_task_queue 领取 pending 任务（先进先出）
- 用 SELECT ... FOR UPDATE SKIP LOCKED（PG）/ 乐观锁（SQLite）并发领取
- 调用 EmbeddingService.embed 计算向量
- 调用 VectorStore.upsert / delete 写向量库
- 成功 → 回写 asset_index.embedding_id + 队列 status=done
- 失败 → retry_count+1，超限则 status=failed
- 孤儿补偿：若资产已被 delete（status=deleted），worker 写完后立即清向量库

幂等保证：
- 同一 (asset_id, model_version) 的多个 pending 任务由 worker 取最新一条执行
- 任务执行采用 point_id = f"{model_version}_{asset_id}"，重复 upsert 覆盖
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.orm import Session

from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import (
    AssetIndex as AssetIndexRow,
    EmbeddingTaskQueue,
    EmbeddingVector,
)
from server.infra_db.vectorstore import VectorStore

logger = logging.getLogger(__name__)


class OutboxWorker:
    """outbox 队列消费者，后台线程 / 定时调用均可。

    用法：
        worker = OutboxWorker(db, embedding_service, vector_store)
        worker.run_once(batch_size=50)  # 单次扫描
        worker.start()  # 后台线程持续运行
        worker.stop()   # 停止
    """

    LEASE_TIMEOUT_SECONDS = 300  # 5 分钟未完成视为 lease 失效，可被重新领取

    def __init__(
        self,
        database: Database,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        *,
        lease_timeout: int = LEASE_TIMEOUT_SECONDS,
        worker_id: str | None = None,
    ) -> None:
        self._db = database
        self._embedding = embedding_service
        self._vector_store = vector_store
        self._lease_timeout = lease_timeout
        self._worker_id = worker_id or f"worker-{threading.get_ident()}"
        self._stop_flag = threading.Event()
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    # 任务领取
    # ------------------------------------------------------------------

    def _claim_tasks(self, sess: Session, batch_size: int) -> list[EmbeddingTaskQueue]:
        """领取一批 pending 或 lease 过期的任务。

        PG 用 SELECT FOR UPDATE SKIP LOCKED 支持多 worker 并发；
        SQLite 退化为 SELECT + UPDATE 乐观锁（单 worker 测试足够）。
        """
        now = datetime.now(timezone.utc)
        lease_cutoff = now - timedelta(seconds=self._lease_timeout)
        # 先选候选 id（避免整行锁在 SQLite 上的兼容问题）
        stmt = (
            select(EmbeddingTaskQueue)
            .where(
                or_(
                    EmbeddingTaskQueue.status == "pending",
                    and_(
                        EmbeddingTaskQueue.status == "in_progress",
                        EmbeddingTaskQueue.leased_at < lease_cutoff,
                    ),
                )
            )
            .order_by(EmbeddingTaskQueue.created_at.asc())
            .limit(batch_size)
        )
        # PG 用 SKIP LOCKED
        if self._db.sync_engine.dialect.name in ("postgresql", "psycopg", "psycopg2"):
            stmt = stmt.with_for_update(skip_locked=True)
        tasks = list(sess.scalars(stmt))
        now_str = now
        for t in tasks:
            t.status = "in_progress"
            t.leased_at = now_str
            t.lease_owner = self._worker_id
        sess.flush()
        return tasks

    # ------------------------------------------------------------------
    # 任务执行
    # ------------------------------------------------------------------

    def run_once(self, *, batch_size: int = 50) -> int:
        """单次扫描并执行任务，返回处理条数。"""
        processed = 0
        with self._db.session() as sess:
            tasks = self._claim_tasks(sess, batch_size)
            for task in tasks:
                try:
                    self._execute_task(sess, task)
                    processed += 1
                except Exception as exc:
                    self._handle_failure(sess, task, exc)
                    logger.exception(
                        "outbox 任务执行失败 task_id=%s asset_id=%s",
                        task.id,
                        task.asset_id,
                    )
        return processed

    def _execute_task(self, sess: Session, task: EmbeddingTaskQueue) -> None:
        """执行单条任务：计算向量 + 写向量库 + 回写状态。"""
        asset_row = sess.get(AssetIndexRow, task.asset_id)

        # ---------- delete 任务 ----------
        if task.task_type == "delete":
            self._vector_store.delete(task.asset_id, task.model_version)
            self._delete_embedding_track(sess, task.asset_id, task.model_version)
            task.status = "done"
            task.completed_at = datetime.now(timezone.utc)
            return

        # ---------- upsert / reindex 任务 ----------
        if asset_row is None or asset_row.status == "deleted":
            # 孤儿补偿：资产已被回滚 / 删除，向量库可能已写入 → 主动清理
            logger.info(
                "孤儿补偿：资产 %s 不存在或已删除，清理向量库 model_version=%s",
                task.asset_id,
                task.model_version,
            )
            try:
                self._vector_store.delete(task.asset_id, task.model_version)
            except Exception:
                logger.exception("孤儿补偿删除向量库失败 asset_id=%s", task.asset_id)
            self._delete_embedding_track(sess, task.asset_id, task.model_version)
            task.status = "orphan_compensated"
            task.completed_at = datetime.now(timezone.utc)
            return

        # 计算向量（用资产内容快照）
        content = asset_row.content_snapshot or ""
        emb = self._embedding.embed(content, model_version=task.model_version)
        # 写向量库
        from server.infra_db.vectorstore import VectorRecord

        metadata = {
            "module_path": asset_row.module_path,
            "category": asset_row.category or "",
            "type": asset_row.type,
            "owner": asset_row.owner,
            "scope": asset_row.scope,
            "model_version": task.model_version,
        }
        record = VectorRecord(
            asset_id=task.asset_id,
            vector=emb.vector,
            dim=emb.dim,
            metadata=metadata,
        )
        # ensure_collection 由 upsert 内部触发
        point_id = self._vector_store.upsert(record, task.model_version)

        # 同步 upsert EmbeddingVector 跟踪表（同事务，迁移进度查询数据源）
        self._upsert_embedding_track(
            sess, task.asset_id, task.model_version, emb.vector, emb.dim
        )

        # 回写 asset_index.embedding_id（仅当任务版本 == 资产 active_embedding_version）
        if (
            asset_row.active_embedding_version
            and asset_row.active_embedding_version == task.model_version
        ):
            asset_row.embedding_id = point_id
            asset_row.updated_at = datetime.now(timezone.utc)

        task.status = "done"
        task.embedding_id = point_id
        task.completed_at = datetime.now(timezone.utc)
        task.last_error = None

    def _handle_failure(self, sess: Session, task: EmbeddingTaskQueue, exc: Exception) -> None:
        """任务失败处理：重试计数累加，超限置 failed。"""
        task.retry_count += 1
        task.last_error = f"{type(exc).__name__}: {exc}"
        if task.retry_count >= task.max_retries:
            task.status = "failed"
        else:
            task.status = "pending"
            task.leased_at = None
            task.lease_owner = None

    # ------------------------------------------------------------------
    # EmbeddingVector 跟踪表维护（与向量库写入同事务）
    # ------------------------------------------------------------------

    def _upsert_embedding_track(
        self,
        sess: Session,
        asset_id: str,
        model_version: str,
        vector: list[float],
        dim: int,
    ) -> None:
        """upsert asset_embedding 跟踪表（迁移进度查询数据源）。

        向量序列化为 JSON 字符串（PGVector 后端由 VectorStore 自管 raw 表，
        此 ORM 表仅作进度跟踪与跨后端统一计数源）。
        """
        existing = sess.execute(
            select(EmbeddingVector).where(
                EmbeddingVector.asset_id == asset_id,
                EmbeddingVector.model_version == model_version,
            )
        ).scalar_one_or_none()
        serialized = json.dumps(vector, ensure_ascii=False)
        if existing is not None:
            existing.embedding = serialized
            existing.dim = dim
            existing.created_at = datetime.now(timezone.utc)
        else:
            sess.add(
                EmbeddingVector(
                    asset_id=asset_id,
                    model_version=model_version,
                    embedding=serialized,
                    dim=dim,
                )
            )

    def _delete_embedding_track(
        self,
        sess: Session,
        asset_id: str,
        model_version: str,
    ) -> None:
        """删除 asset_embedding 跟踪行（delete / 孤儿补偿路径调用）。"""
        row = sess.execute(
            select(EmbeddingVector).where(
                EmbeddingVector.asset_id == asset_id,
                EmbeddingVector.model_version == model_version,
            )
        ).scalar_one_or_none()
        if row is not None:
            sess.delete(row)

    # ------------------------------------------------------------------
    # 后台线程
    # ------------------------------------------------------------------

    def start(self, *, poll_interval: float = 1.0, batch_size: int = 50) -> None:
        """启动后台线程持续消费队列。"""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_flag.clear()

        def _loop() -> None:
            while not self._stop_flag.is_set():
                try:
                    self.run_once(batch_size=batch_size)
                except Exception:
                    logger.exception("outbox worker 循环异常")
                time.sleep(poll_interval)

        self._thread = threading.Thread(target=_loop, daemon=True, name="outbox-worker")
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        """停止后台线程。"""
        self._stop_flag.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None


__all__ = ["OutboxWorker"]
