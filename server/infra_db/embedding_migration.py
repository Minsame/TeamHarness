"""embedding 模型双写过渡管理（SubTask 2.7）。

对应缺陷 2.4 embedding 迁移：
- 新旧两表（VectorStore 用 model_version 区分）
- active_embedding_version 控制召回使用哪一套
- 后台补齐：扫描 active_embedding_version != new_version 的资产 → 投递 reindex
- 过渡期召回融合两套向量结果（RRF，由 RecallService 调用 EmbeddingService.fuse_rrf）
- 全量迁移完成后：switch_active → drop_old_version

迁移流程：
1. start_migration(new_version) → EmbeddingService.start_shadow_write(new_version)
   后续 upsert 自动双写（active + shadow）
2. migrate_batch(batch_size) → 后台扫描 active 版本资产，投递 shadow reindex 任务
   worker 异步消费写 shadow 向量库
3. verify_progress() → 检查 shadow 向量数 == active 资产数
4. switch_active(new_version) → EmbeddingService.switch_active_version
   asset_index.active_embedding_version 全表更新
5. drop_old_version(old_version) → VectorStore.delete_collection + 清旧任务

回滚：
- rollback_migration() → 停止 shadow 写，丢弃 shadow 向量
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import func, select, update

from server.infra_db.asset_index import AssetIndex
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import (
    AssetIndex as AssetIndexRow,
    EmbeddingTaskQueue,
    EmbeddingVector,
)
from server.infra_db.vectorstore import VectorStore

logger = logging.getLogger(__name__)


@dataclass
class MigrationProgress:
    """迁移进度快照。"""

    active_version: str
    shadow_version: str
    total_assets: int
    active_embedding_count: int  # active 版本向量数
    shadow_embedding_count: int  # shadow 版本向量数
    pending_tasks: int  # 待处理 shadow reindex 任务数
    ready_to_switch: bool  # shadow 完成度 == 100%

    @property
    def shadow_progress_pct(self) -> float:
        if self.total_assets == 0:
            return 100.0
        return round(self.shadow_embedding_count / self.total_assets * 100, 2)


@dataclass
class MigrateBatchResult:
    """单次迁移批次结果。"""

    scanned: int = 0
    enqueued: int = 0
    already_done: int = 0


class EmbeddingMigration:
    """embedding 模型双写过渡管理器。

    用法：
        mig = EmbeddingMigration(db, asset_index, embedding_service, vector_store)
        mig.start_migration("v2")            # 启动双写
        while True:
            r = mig.migrate_batch(100)        # 后台补齐
            if r.enqueued == 0:
                break
        prog = mig.verify_progress()
        if prog.ready_to_switch:
            mig.switch_active("v2")           # 切换 active
            mig.drop_old_version("v1")        # 清理旧向量
    """

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._embedding_service = embedding_service
        self._vector_store = vector_store

    # ------------------------------------------------------------------
    # 启动 / 回滚
    # ------------------------------------------------------------------

    def start_migration(self, new_version: str) -> None:
        """启动双写过渡：EmbeddingService 配置 shadow_version。

        之后所有 upsert 自动双写 active + new_version。
        """
        active = self._embedding_service.get_active_version()
        if new_version == active:
            raise ValueError(f"new_version({new_version}) 不能等于 active_version({active})")
        self._embedding_service.start_shadow_write(new_version)
        self._asset_index.set_embedding_versions(
            active=active, shadow=new_version
        )
        logger.info("embedding 双写过渡启动 active=%s shadow=%s", active, new_version)

    def rollback_migration(self) -> None:
        """回滚迁移：停止 shadow 写，丢弃 shadow 任务与向量。"""
        shadow = self._embedding_service.get_shadow_version()
        if not shadow:
            return
        self._embedding_service.stop_shadow_write()
        self._asset_index.set_embedding_versions(
            active=self._embedding_service.get_active_version(), shadow=""
        )
        # 删除 shadow 版本的 pending / in_progress 任务
        with self._db.session() as sess:
            rows = sess.execute(
                select(EmbeddingTaskQueue)
                .where(EmbeddingTaskQueue.model_version == shadow)
                .where(EmbeddingTaskQueue.status.in_(["pending", "in_progress"]))
            ).scalars().all()
            for r in rows:
                sess.delete(r)
        # 删除 shadow 向量库 collection（如果支持）
        try:
            self._drop_collection(shadow)
        except Exception as exc:
            logger.warning("回滚删除 shadow 向量库失败 version=%s err=%s", shadow, exc)
        logger.info("embedding 迁移回滚完成 shadow=%s 已清理", shadow)

    # ------------------------------------------------------------------
    # 后台补齐
    # ------------------------------------------------------------------

    def migrate_batch(self, batch_size: int = 100) -> MigrateBatchResult:
        """扫描 active 版本资产，为缺失 shadow 向量的资产投递 reindex 任务。

        策略：找 active_embedding_version == active 且 asset_id 不在
        EmbeddingVector(model_version=shadow) 中的资产 → 投递 reindex 任务。
        """
        result = MigrateBatchResult()
        active = self._embedding_service.get_active_version()
        shadow = self._embedding_service.get_shadow_version()
        if not shadow:
            return result

        with self._db.session() as sess:
            # 找已有 shadow 向量的 asset_id 集合
            have_shadow_ids = set(
                sess.scalars(
                    select(EmbeddingVector.asset_id).where(
                        EmbeddingVector.model_version == shadow
                    )
                )
            )
            # 找 active 资产（status=active）但缺 shadow 向量的
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.active_embedding_version == active)
                .limit(batch_size)
            )
            candidates = list(sess.scalars(stmt))
            result.scanned = len(candidates)
            # 已有 pending shadow 任务的不重复投递
            existing_pending_ids = set(
                sess.scalars(
                    select(EmbeddingTaskQueue.asset_id)
                    .where(EmbeddingTaskQueue.model_version == shadow)
                    .where(EmbeddingTaskQueue.status.in_(["pending", "in_progress"]))
                )
            )
            for row in candidates:
                if row.id in have_shadow_ids:
                    result.already_done += 1
                    continue
                if row.id in existing_pending_ids:
                    continue
                task = EmbeddingTaskQueue(
                    asset_id=row.id,
                    task_type="reindex",
                    model_version=shadow,
                    status="pending",
                    retry_count=0,
                    max_retries=3,
                )
                sess.add(task)
                result.enqueued += 1
        if result.enqueued:
            logger.info(
                "迁移批次：scanned=%d enqueued=%d already_done=%d",
                result.scanned,
                result.enqueued,
                result.already_done,
            )
        return result

    # ------------------------------------------------------------------
    # 进度查询
    # ------------------------------------------------------------------

    def verify_progress(self) -> MigrationProgress:
        """返回当前迁移进度。"""
        active = self._embedding_service.get_active_version()
        shadow = self._embedding_service.get_shadow_version()
        with self._db.session() as sess:
            total = sess.scalar(
                select(func.count(AssetIndexRow.id)).where(
                    AssetIndexRow.status == "active"
                )
            ) or 0
            active_count = sess.scalar(
                select(func.count(EmbeddingVector.id)).where(
                    EmbeddingVector.model_version == active
                )
            ) or 0
            shadow_count = (
                sess.scalar(
                    select(func.count(EmbeddingVector.id)).where(
                        EmbeddingVector.model_version == shadow
                    )
                )
                if shadow
                else 0
            )
            pending = (
                sess.scalar(
                    select(func.count(EmbeddingTaskQueue.id)).where(
                        EmbeddingTaskQueue.model_version == shadow,
                        EmbeddingTaskQueue.status.in_(["pending", "in_progress"]),
                    )
                )
                if shadow
                else 0
            )
        return MigrationProgress(
            active_version=active,
            shadow_version=shadow,
            total_assets=int(total),
            active_embedding_count=int(active_count),
            shadow_embedding_count=int(shadow_count),
            pending_tasks=int(pending),
            ready_to_switch=bool(
                shadow
                and int(shadow_count) >= int(total)
                and int(pending) == 0
            ),
        )

    # ------------------------------------------------------------------
    # 切换 / 清理
    # ------------------------------------------------------------------

    def switch_active(self, new_active: str) -> None:
        """切换 active_embedding_version（全量迁移完成后调用）。

        - EmbeddingService.switch_active_version
        - asset_index.active_embedding_version 全表更新
        - embedding_id 重新指向新版本向量（按需由对账任务补偿）
        """
        old_active = self._embedding_service.get_active_version()
        if new_active == old_active:
            return
        self._embedding_service.switch_active_version(new_active)
        self._asset_index.set_embedding_versions(
            active=new_active, shadow=self._embedding_service.get_shadow_version()
        )
        with self._db.session() as sess:
            # 全表更新 active_embedding_version
            sess.execute(
                update(AssetIndexRow)
                .where(AssetIndexRow.status == "active")
                .values(active_embedding_version=new_active, embedding_id=None)
            )
        logger.info(
            "embedding active 版本切换：%s → %s（embedding_id 已置空，由对账任务补偿）",
            old_active,
            new_active,
        )

    def drop_old_version(self, old_version: str) -> int:
        """全量迁移完成后清理旧版本向量库。

        返回删除的向量记录数。
        """
        # 1. 删除向量库 collection
        try:
            self._drop_collection(old_version)
        except Exception as exc:
            logger.warning("drop_old_version 删除向量库失败 version=%s err=%s", old_version, exc)
        # 2. 删除 EmbeddingVector 表中该版本记录
        with self._db.session() as sess:
            rows = sess.execute(
                select(EmbeddingVector).where(EmbeddingVector.model_version == old_version)
            ).scalars().all()
            count = len(rows)
            for r in rows:
                sess.delete(r)
            # 3. 删除该版本历史任务（保留 done 用于审计）
            sess.execute(
                EmbeddingTaskQueue.__table__.delete().where(
                    EmbeddingTaskQueue.model_version == old_version,
                    EmbeddingTaskQueue.status.in_(["pending", "in_progress", "failed"]),
                )
            )
        logger.info("旧版本向量已清理 version=%s count=%d", old_version, count)
        return count

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _drop_collection(self, model_version: str) -> None:
        """删除向量库 collection（按 backend 类型分派）。"""
        backend = getattr(self._vector_store, "backend_name", "")
        if backend == "qdrant":
            from qdrant_client.http import models as qm

            name = self._vector_store._collection_name(model_version)
            try:
                self._vector_store._client.delete_collection(name)
            except Exception:
                pass
        elif backend == "pgvector":
            from sqlalchemy import text

            tbl = self._vector_store._table_name(model_version)
            with self._vector_store._engine.begin() as conn:
                conn.execute(text(f"DROP TABLE IF EXISTS {tbl};"))
        # InMemoryVectorStore：清空对应 (model_version, *) 项
        elif backend == "memory":
            keys_to_del = [
                k for k in self._vector_store._store if k[0] == model_version
            ]
            for k in keys_to_del:
                self._vector_store._store.pop(k, None)


__all__ = [
    "EmbeddingMigration",
    "MigrateBatchResult",
    "MigrationProgress",
]
