"""SubTask 8.1 + 8.2 + 8.12：Light 增量聚类 + 全量聚类 + cluster_fingerprint 去重 + 快照隔离。

覆盖：
- Light 增量聚类只处理新增/修改资产
- cluster_fingerprint 去重（同指纹簇已提炼过则跳过）
- 全量聚类（无去重）
- 快照隔离：启动时快照 commit SHA，完成后增量 delta
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.clustering import (
    ClusteringService,
    ClusterParams,
    compute_cluster_fingerprint,
    is_cluster_already_distilled,
)
from server.distill_team.models import JobStatus, JobTriggerSource
from server.distill_team.snapshot import JobSnapshotIsolation

from .conftest import upsert_asset


# ---------------------------------------------------------------------------
# 8.1 Light 增量聚类
# ---------------------------------------------------------------------------


class TestLightClustering:
    """Light 增量聚类测试。"""

    def test_light_cluster_empty_input_returns_empty(
        self, database, asset_index, embedding_service, vector_store
    ):
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)
        assert cs.light_cluster([]) == []

    def test_light_cluster_single_asset_no_cluster(
        self, database, asset_index, embedding_service, vector_store
    ):
        """单资产无邻居 → 不形成簇（min_cluster_size=2）。"""
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)
        clusters = cs.light_cluster(["a1"])
        # 单资产无法形成簇（min_cluster_size=2）
        assert clusters == []

    def test_light_cluster_dedup_already_distilled(
        self, database, asset_index, embedding_service, vector_store
    ):
        """同 fingerprint 簇已提炼过 → 跳过。"""
        # 写入 2 个同 owner 同 module 资产（聚成一簇）
        upsert_asset(asset_index, id="a1", owner="alice", module_path="m1", content="# rule A")
        upsert_asset(asset_index, id="a2", owner="alice", module_path="m1", content="# rule A")
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)

        # 第一次聚类：产出簇
        clusters1 = cs.light_cluster(["a1", "a2"], skip_already_distilled=True)
        assert len(clusters1) == 1
        fingerprint = clusters1[0].fingerprint

        # 模拟同指纹簇已提炼（写 distillation_job 行）
        from server.infra_db.models import DistillationJob
        import uuid
        from datetime import datetime, timezone

        with database.session() as sess:
            sess.add(DistillationJob(
                id=f"job-{uuid.uuid4().hex[:8]}",
                trigger_source="incremental",
                cluster_fingerprint=fingerprint,
                snapshot_commit="",
                status="completed",
                started_at=datetime.now(timezone.utc),
                finished_at=datetime.now(timezone.utc),
            ))

        # 第二次聚类：同指纹 → 跳过
        clusters2 = cs.light_cluster(["a1", "a2"], skip_already_distilled=True)
        assert clusters2 == []

    def test_light_cluster_no_dedup_when_skip_flag_false(
        self, database, asset_index, embedding_service, vector_store
    ):
        """skip_already_distilled=False → 不去重，重复聚类。"""
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        upsert_asset(asset_index, id="a2", owner="alice", content="# rule")
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)

        clusters1 = cs.light_cluster(["a1", "a2"], skip_already_distilled=True)
        assert len(clusters1) == 1
        fp = clusters1[0].fingerprint

        # 写已提炼记录
        from server.infra_db.models import DistillationJob
        import uuid
        from datetime import datetime, timezone

        with database.session() as sess:
            sess.add(DistillationJob(
                id=f"job-{uuid.uuid4().hex[:8]}",
                trigger_source="incremental",
                cluster_fingerprint=fp,
                snapshot_commit="",
                status="completed",
            ))

        # skip_already_distilled=False → 仍聚出簇
        clusters2 = cs.light_cluster(["a1", "a2"], skip_already_distilled=False)
        assert len(clusters2) == 1


# ---------------------------------------------------------------------------
# cluster_fingerprint 单元测试
# ---------------------------------------------------------------------------


class TestClusterFingerprint:
    """cluster_fingerprint 去重指纹。"""

    def test_fingerprint_deterministic(self):
        fp1 = compute_cluster_fingerprint(["a", "b", "c"])
        fp2 = compute_cluster_fingerprint(["c", "b", "a"])
        assert fp1 == fp2  # 顺序无关

    def test_fingerprint_dedup_distinct(self):
        fp1 = compute_cluster_fingerprint(["a", "b"])
        fp2 = compute_cluster_fingerprint(["a", "c"])
        assert fp1 != fp2

    def test_fingerprint_dedup_dedup_within_list(self):
        """同资产重复传入 → 指纹一致（去重）。"""
        fp1 = compute_cluster_fingerprint(["a", "b", "a"])
        fp2 = compute_cluster_fingerprint(["a", "b"])
        assert fp1 == fp2

    def test_is_cluster_already_distilled_false_on_empty(
        self, database
    ):
        """无任何已提炼 job → False。"""
        assert is_cluster_already_distilled(database, "nonexistent") is False

    def test_is_cluster_already_distilled_true_after_complete(
        self, database
    ):
        """有 completed job → True。"""
        from server.infra_db.models import DistillationJob
        import uuid
        from datetime import datetime, timezone

        with database.session() as sess:
            sess.add(DistillationJob(
                id=f"job-{uuid.uuid4().hex[:8]}",
                trigger_source="incremental",
                cluster_fingerprint="fp-test",
                snapshot_commit="",
                status="completed",
                finished_at=datetime.now(timezone.utc),
            ))
        assert is_cluster_already_distilled(database, "fp-test") is True

    def test_is_cluster_already_distilled_false_on_running(
        self, database
    ):
        """running job 不算已提炼。"""
        from server.infra_db.models import DistillationJob
        import uuid

        with database.session() as sess:
            sess.add(DistillationJob(
                id=f"job-{uuid.uuid4().hex[:8]}",
                trigger_source="incremental",
                cluster_fingerprint="fp-running",
                snapshot_commit="",
                status="running",
            ))
        assert is_cluster_already_distilled(database, "fp-running") is False


# ---------------------------------------------------------------------------
# 8.2 全量聚类
# ---------------------------------------------------------------------------


class TestFullClustering:
    """全量聚类测试。"""

    def test_full_cluster_empty_db_returns_empty(
        self, database, asset_index, embedding_service, vector_store
    ):
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)
        assert cs.full_cluster() == []

    def test_full_cluster_no_dedup(
        self, database, asset_index, embedding_service, vector_store
    ):
        """全量聚类不做 fingerprint 去重（重建发现新跨成员模式）。"""
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        upsert_asset(asset_index, id="a2", owner="alice", content="# rule")
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)

        clusters1 = cs.full_cluster()
        # 写已提炼记录
        if clusters1:
            from server.infra_db.models import DistillationJob
            import uuid

            with database.session() as sess:
                sess.add(DistillationJob(
                    id=f"job-{uuid.uuid4().hex[:8]}",
                    trigger_source="full",
                    cluster_fingerprint=clusters1[0].fingerprint,
                    snapshot_commit="",
                    status="completed",
                ))

        # 全量聚类仍能产出（不去重）
        clusters2 = cs.full_cluster()
        # 簇数应一致（无去重）
        assert len(clusters2) == len(clusters1)


# ---------------------------------------------------------------------------
# 8.12 快照隔离
# ---------------------------------------------------------------------------


class TestJobSnapshotIsolation:
    """快照隔离测试。"""

    def test_start_job_records_snapshot_sha(
        self, database, asset_index, head_resolver_factory
    ):
        head_resolver_factory.set_head("snap-abc")
        iso = JobSnapshotIsolation(database, asset_index, head_resolver_factory)

        upsert_asset(asset_index, id="a1", owner="alice")

        snapshot = iso.start_job(trigger_source=JobTriggerSource.INCREMENTAL)
        assert snapshot.snapshot_commit == "snap-abc"
        assert snapshot.head_commit_at_start == "snap-abc"
        assert "a1" in snapshot.asset_ids
        assert snapshot.asset_count == 1

        # distillation_job 表有对应行
        from server.infra_db.models import DistillationJob
        with database.session() as sess:
            job = sess.get(DistillationJob, snapshot.job_id)
            assert job is not None
            assert job.snapshot_commit == "snap-abc"
            assert job.status == JobStatus.RUNNING
            assert job.trigger_source == JobTriggerSource.INCREMENTAL

    def test_complete_job_no_delta_when_head_unchanged(
        self, database, asset_index, head_resolver_factory
    ):
        head_resolver_factory.set_head("snap-abc")
        iso = JobSnapshotIsolation(database, asset_index, head_resolver_factory)

        snapshot = iso.start_job()
        delta = iso.complete_job(snapshot)

        assert delta.need_delta_job is False
        assert delta.new_commit == ""
        assert delta.changed_asset_ids == []

    def test_complete_job_delta_when_head_advanced(
        self, database, asset_index, head_resolver_factory
    ):
        """HEAD 在 job 期间前进 → need_delta_job=True。"""
        head_resolver_factory.set_head("snap-abc")
        iso = JobSnapshotIsolation(database, asset_index, head_resolver_factory)

        snapshot = iso.start_job()
        # 模拟 job 期间新 commit
        head_resolver_factory.set_head("snap-def")
        # 写入新 commit 的资产
        upsert_asset(asset_index, id="a1", owner="alice", git_commit="snap-def")

        delta = iso.complete_job(snapshot)

        assert delta.need_delta_job is True
        assert delta.new_commit == "snap-def"
        assert "a1" in delta.changed_asset_ids

    def test_complete_job_updates_status(
        self, database, asset_index, head_resolver_factory
    ):
        head_resolver_factory.set_head("snap-abc")
        iso = JobSnapshotIsolation(database, asset_index, head_resolver_factory)

        snapshot = iso.start_job()
        iso.complete_job(snapshot, status=JobStatus.COMPLETED, score=0.75)

        from server.infra_db.models import DistillationJob
        with database.session() as sess:
            job = sess.get(DistillationJob, snapshot.job_id)
            assert job.status == JobStatus.COMPLETED
            assert job.score == 0.75
            assert job.finished_at is not None

    def test_snapshot_isolation_with_empty_head_resolver(
        self, database, asset_index
    ):
        """head_resolver 抛异常 → 快照用空 SHA（不阻塞 job）。"""

        def failing_resolver():
            raise RuntimeError("git 不可达")

        iso = JobSnapshotIsolation(database, asset_index, failing_resolver)
        snapshot = iso.start_job()
        assert snapshot.snapshot_commit == ""  # 容错为空

        delta = iso.complete_job(snapshot)
        # HEAD 仍空 → 无 delta
        assert delta.need_delta_job is False
