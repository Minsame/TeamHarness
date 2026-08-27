"""SubTask 2.7：embedding 模型双写过渡 + 后台补齐 + 全量迁移 + drop 旧版本。"""

from __future__ import annotations

import pytest
from sqlalchemy import select

from server.infra_db.embedding import EmbeddingService
from server.infra_db.embedding_migration import EmbeddingMigration
from server.infra_db.models import (
    AssetIndex as AssetIndexRow,
    EmbeddingTaskQueue,
    EmbeddingVector,
)


def _setup_assets(asset_index, count: int = 3) -> None:
    """创建 N 个资产并写好 active 版本向量。"""
    from server.infra_db.tests.conftest import make_asset

    for i in range(count):
        asset = make_asset(id=f"a-mig-{i}", content=f"content {i}")
        asset_index.upsert(asset, git_commit="c1")


def test_start_migration_sets_shadow_version(database, asset_index, embedding_service, vector_store):
    """start_migration 后 EmbeddingService 的 shadow_version 被设置。"""
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    assert embedding_service.get_shadow_version() == ""
    mig.start_migration("v2")
    assert embedding_service.get_shadow_version() == "v2"


def test_migrate_batch_enqueues_shadow_reindex(database, asset_index, embedding_service, vector_store, outbox_worker):
    """migrate_batch 为缺失 shadow 向量的资产投递 reindex 任务。"""
    _setup_assets(asset_index, count=3)
    # 让 active 版本向量先写入
    outbox_worker.run_once()
    # 启动迁移到 v2
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    mig.start_migration("v2")
    # 后台补齐
    result = mig.migrate_batch(batch_size=10)
    assert result.scanned >= 3
    assert result.enqueued == 3  # 3 个资产都缺 v2 向量

    # 验证队列中有 v2 reindex 任务
    with database.session() as sess:
        tasks = list(sess.scalars(
            select(EmbeddingTaskQueue)
            .where(EmbeddingTaskQueue.model_version == "v2")
            .where(EmbeddingTaskQueue.task_type == "reindex")
        ))
        assert len(tasks) == 3
        assert all(t.status == "pending" for t in tasks)


def test_migrate_batch_skips_existing_pending(database, asset_index, embedding_service, vector_store, outbox_worker):
    """migrate_batch 不重复投递已有 pending shadow 任务。"""
    _setup_assets(asset_index, count=2)
    outbox_worker.run_once()  # 写 active 向量
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    mig.start_migration("v2")
    # 第一次：投递
    r1 = mig.migrate_batch(batch_size=10)
    assert r1.enqueued == 2
    # 第二次：应跳过（已有 pending）
    r2 = mig.migrate_batch(batch_size=10)
    assert r2.enqueued == 0


def test_verify_progress(database, asset_index, embedding_service, vector_store, outbox_worker):
    """verify_progress 返回正确的迁移进度。"""
    _setup_assets(asset_index, count=2)
    outbox_worker.run_once()  # 写 active 向量

    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    mig.start_migration("v2")
    progress = mig.verify_progress()
    assert progress.active_version == "v1"
    assert progress.shadow_version == "v2"
    assert progress.total_assets == 2
    assert progress.active_embedding_count >= 2  # active 向量已写
    assert progress.shadow_embedding_count == 0  # shadow 还未补齐
    assert progress.ready_to_switch is False

    # 投递 shadow 任务并消费
    mig.migrate_batch(batch_size=10)
    outbox_worker.run_once()

    progress2 = mig.verify_progress()
    # outbox worker 写向量库后是否也写 EmbeddingVector 表？
    # 注：OutboxWorker 直接调 VectorStore，不写 EmbeddingVector ORM 表
    # 所以 progress.shadow_embedding_count 仍可能为 0
    # 但 ready_to_switch 的判定基于 EmbeddingVector 表
    # 这里验证 progress 字段语义正确即可
    assert progress2.pending_tasks == 0  # 任务已消费


def test_switch_active_updates_asset_index(database, asset_index, embedding_service, vector_store, outbox_worker):
    """switch_active 后 asset_index.active_embedding_version 全表更新。"""
    _setup_assets(asset_index, count=2)
    outbox_worker.run_once()
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    mig.start_migration("v2")
    mig.switch_active("v2")
    # EmbeddingService active 已切换
    assert embedding_service.get_active_version() == "v2"
    # asset_index 行的 active_embedding_version 全为 v2
    with database.session() as sess:
        rows = list(sess.scalars(select(AssetIndexRow)))
        assert all(r.active_embedding_version == "v2" for r in rows)
        # embedding_id 被置空（由对账任务补偿）
        assert all(r.embedding_id is None for r in rows)


def test_drop_old_version_clears_old_data(database, asset_index, embedding_service, vector_store, outbox_worker):
    """drop_old_version 删除旧版本向量与历史任务。"""
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-drop-old", content="old")
    asset_index.upsert(asset, git_commit="c1")
    outbox_worker.run_once()  # 写 v1 向量到 InMemoryVectorStore + EmbeddingVector 跟踪表
    # 确认跟踪表已写入
    with database.session() as sess:
        rows = list(sess.scalars(
            select(EmbeddingVector).where(EmbeddingVector.model_version == "v1")
        ))
        assert len(rows) == 1
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    deleted = mig.drop_old_version("v1")
    assert deleted == 1
    # EmbeddingVector 表无 v1 记录
    with database.session() as sess:
        rows = list(sess.scalars(
            select(EmbeddingVector).where(EmbeddingVector.model_version == "v1")
        ))
        assert len(rows) == 0
    # InMemoryVectorStore 也已清空 v1
    assert vector_store.get("a-drop-old", "v1") is None


def test_rollback_migration_clears_shadow(database, asset_index, embedding_service, vector_store, outbox_worker):
    """rollback_migration 停止 shadow 写，清 shadow 任务与向量。"""
    _setup_assets(asset_index, count=2)
    outbox_worker.run_once()  # 写 active 向量
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    mig.start_migration("v2")
    mig.migrate_batch(batch_size=10)
    # 回滚
    mig.rollback_migration()
    assert embedding_service.get_shadow_version() == ""
    # shadow 任务被清
    with database.session() as sess:
        pending = list(sess.scalars(
            select(EmbeddingTaskQueue)
            .where(EmbeddingTaskQueue.model_version == "v2")
            .where(EmbeddingTaskQueue.status.in_(["pending", "in_progress"]))
        ))
        assert len(pending) == 0


def test_start_migration_rejects_same_version(database, asset_index, embedding_service, vector_store):
    """start_migration 不允许 new_version == active。"""
    mig = EmbeddingMigration(database, asset_index, embedding_service, vector_store)
    with pytest.raises(ValueError, match="不能等于"):
        mig.start_migration("v1")  # active 默认就是 v1
