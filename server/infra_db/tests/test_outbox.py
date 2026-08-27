"""SubTask 2.4：outbox 模式 — asset_index + embedding_task_queue 同事务 + worker 一致性。

核心验证点（域内验证点）：
- asset_index + embedding_task_queue 同事务写入
- 异步 worker 消费队列写向量库，成功回写 embedding_id
- 事务回滚后两表一起回滚（无孤儿）
- 孤儿补偿：worker 写完向量库但资产已 delete → 主动清理向量库
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from server.infra_db.models import AssetIndex as AssetIndexRow, EmbeddingTaskQueue


def test_upsert_writes_asset_index_and_queue_atomically(database, asset_index):
    """upsert 应在同事务写 asset_index + embedding_task_queue。

    验证：upsert 后两表都有对应行；事务结束后队列任务为 pending。
    """
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-outbox-1", content="hello world")
    asset_index.upsert(asset, git_commit="commit-1")

    # 两表都有行
    status = asset_index.get_status("a-outbox-1")
    assert status is not None
    assert status.status == "active"
    assert status.embedding_id is None  # worker 未消费，embedding_id 仍为 None

    with database.session() as sess:
        tasks = list(sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-outbox-1")
        ))
        assert len(tasks) == 1
        assert tasks[0].status == "pending"
        assert tasks[0].task_type == "upsert"
        assert tasks[0].model_version == "v1"


def test_upsert_atomic_rollback_on_error(database, asset_index):
    """事务回滚时 asset_index + embedding_task_queue 一起回滚（无孤儿）。"""
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-rollback-1", content="rollback test")
    # 模拟事务内异常：故意触发数据库约束错误（重复主键 + 异常）
    try:
        with database.session() as sess:
            # 这里直接操作 session 模拟 outbox 写入
            from server.infra_db.models import AssetIndex as Row

            row = Row(
                id="a-rollback-1",
                type="rule",
                owner="tester",
                scope="team",
                git_path="rules/x.md",
                git_commit="commit-x",
                module_path="",
                status="active",
                tags="[]",
                related_to="[]",
            )
            sess.add(row)
            # 同事务投递 outbox
            from server.infra_db.models import EmbeddingTaskQueue

            task = EmbeddingTaskQueue(
                asset_id="a-rollback-1",
                task_type="upsert",
                model_version="v1",
                status="pending",
            )
            sess.add(task)
            # 在 commit 前抛异常
            raise RuntimeError("模拟业务异常")
    except RuntimeError:
        pass

    # 两表都不应有行（事务已回滚）
    assert asset_index.get_status("a-rollback-1") is None
    with database.session() as sess:
        tasks = list(sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-rollback-1")
        ))
        assert len(tasks) == 0


def test_worker_consumes_task_and_writes_vector(database, asset_index, outbox_worker, vector_store):
    """worker 消费 pending 任务 → 写向量库 → 回写 embedding_id。"""
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-worker-1", content="worker test content")
    asset_index.upsert(asset, git_commit="commit-1")

    # 队列有 1 条 pending
    with database.session() as sess:
        tasks = list(sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-worker-1")
        ))
        assert len(tasks) == 1
        assert tasks[0].status == "pending"

    # worker 单次消费
    processed = outbox_worker.run_once(batch_size=10)
    assert processed == 1

    # 队列任务变 done，asset_index.embedding_id 已回写
    status = asset_index.get_status("a-worker-1")
    assert status is not None
    assert status.embedding_id is not None

    with database.session() as sess:
        task = sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-worker-1")
        ).first()
        assert task.status == "done"
        assert task.embedding_id is not None
        assert task.completed_at is not None

    # 向量库有对应向量
    rec = vector_store.get("a-worker-1", "v1")
    assert rec is not None
    assert rec.dim > 0


def test_worker_skips_already_done_task(database, asset_index, outbox_worker):
    """worker 不会重复处理已 done 的任务。"""
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-idem-1", content="idempotent test")
    asset_index.upsert(asset, git_commit="commit-1")
    # 第一次消费
    assert outbox_worker.run_once(batch_size=10) == 1
    # 第二次消费应无任务可处理
    assert outbox_worker.run_once(batch_size=10) == 0


def test_worker_orphan_compensation_when_asset_deleted(database, asset_index, outbox_worker, vector_store):
    """孤儿补偿：worker 写完向量库前资产已 delete → worker 应清理向量库。

    场景：
    1. upsert 资产 → 投递 pending 任务
    2. delete 资产（soft delete + 投递 delete 任务）
    3. worker 消费 upsert 任务时检测 status=deleted → 标记 orphan_compensated 并删向量
    """
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-orphan-1", content="orphan test")
    asset_index.upsert(asset, git_commit="commit-1")
    # 在 worker 消费前 soft delete
    asset_index.delete("a-orphan-1", git_commit="commit-2", soft_delete=True)

    # worker 消费 upsert 任务：应进入孤儿补偿分支
    processed = outbox_worker.run_once(batch_size=10)
    assert processed >= 1

    with database.session() as sess:
        tasks = list(sess.scalars(
            select(EmbeddingTaskQueue)
            .where(EmbeddingTaskQueue.asset_id == "a-orphan-1")
            .order_by(EmbeddingTaskQueue.id)
        ))
        # 第一条是 upsert 任务（孤儿补偿），第二条是 delete 任务
        upsert_task = tasks[0]
        assert upsert_task.task_type == "upsert"
        assert upsert_task.status == "orphan_compensated"

    # delete 任务由 worker 正常处理（删向量库）
    if any(t.task_type == "delete" and t.status == "pending" for t in tasks):
        outbox_worker.run_once(batch_size=10)

    # 向量库应无该资产向量
    assert vector_store.get("a-orphan-1", "v1") is None


def test_worker_delete_task_removes_vector(database, asset_index, outbox_worker, vector_store):
    """delete 任务：worker 调用 vector_store.delete。"""
    from server.infra_db.tests.conftest import make_asset

    asset = make_asset(id="a-del-1", content="delete test")
    asset_index.upsert(asset, git_commit="commit-1")
    outbox_worker.run_once(batch_size=10)  # 写入向量

    # 现在 delete 资产
    asset_index.delete("a-del-1", git_commit="commit-2")
    # worker 处理 delete 任务
    outbox_worker.run_once(batch_size=10)

    # 向量库已删
    assert vector_store.get("a-del-1", "v1") is None

    with database.session() as sess:
        delete_tasks = list(sess.scalars(
            select(EmbeddingTaskQueue)
            .where(EmbeddingTaskQueue.asset_id == "a-del-1")
            .where(EmbeddingTaskQueue.task_type == "delete")
        ))
        assert all(t.status == "done" for t in delete_tasks)


def test_worker_retry_on_failure_then_failed(database, asset_index, embedding_service, vector_store):
    """worker 失败重试 max_retries 次后置 failed。"""
    from server.infra_db.tests.conftest import make_asset
    from server.infra_db.outbox import OutboxWorker

    # 构造一个失败的 embedding service（embed 抛异常）
    class _FailingEmbedding:
        def embed(self, text, *, model_version=None):
            raise RuntimeError("embedding 失败")

        def get_active_version(self):
            return "v1"

        def get_shadow_version(self):
            return ""

    worker = OutboxWorker(database, _FailingEmbedding(), vector_store, worker_id="fail-worker")
    worker._lease_timeout = 600  # 长一些避免 lease 失效干扰

    asset = make_asset(id="a-fail-1", content="fail test")
    asset_index.upsert(asset, git_commit="commit-1")

    # max_retries=3，应重试 3 次
    for i in range(3):
        worker.run_once(batch_size=10)
    # 第 4 次应无任务可处理（已 failed）
    assert worker.run_once(batch_size=10) == 0

    with database.session() as sess:
        task = sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-fail-1")
        ).first()
        assert task.status == "failed"
        assert task.retry_count == 3
        assert "embedding 失败" in (task.last_error or "")


def test_lease_timeout_reclaims_in_progress_task(database, asset_index, embedding_service, vector_store):
    """lease 超时的 in_progress 任务可被重新领取。"""
    from server.infra_db.tests.conftest import make_asset
    from server.infra_db.outbox import OutboxWorker

    asset = make_asset(id="a-lease-1", content="lease test")
    asset_index.upsert(asset, git_commit="commit-1")

    # 手动把任务置为 in_progress 且 lease_at 早于 cutoff
    cutoff_seconds_ago = 1000
    with database.session() as sess:
        task = sess.scalars(
            select(EmbeddingTaskQueue).where(EmbeddingTaskQueue.asset_id == "a-lease-1")
        ).first()
        task.status = "in_progress"
        task.leased_at = datetime.now(timezone.utc) - timedelta(seconds=cutoff_seconds_ago)
        task.lease_owner = "dead-worker"

    # 新 worker 短 lease_timeout，应能领取该任务
    worker = OutboxWorker(
        database, embedding_service, vector_store,
        lease_timeout=1, worker_id="new-worker",
    )
    processed = worker.run_once(batch_size=10)
    assert processed == 1

    # 验证任务已 done
    status = asset_index.get_status("a-lease-1")
    assert status.embedding_id is not None
