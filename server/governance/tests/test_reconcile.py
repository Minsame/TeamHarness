"""SubTask 9.12+9.13: teamharness index reconcile 命令测试。

覆盖：
- reconcile 检测 declared vs actual 不一致
- reconcile 无不一致 → 不触发补同步
- reconcile 触发补同步（sync_service.reconcile 调用）
- reconcile_and_fix 强制补同步
- commit_sha 解析
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

import pytest

from server.governance.reconcile import ReconcileService
from server.infra_db.models import IndexSyncState, ModuleStats


class MockSyncService:
    """mock SyncService，记录 reconcile 调用。"""

    def __init__(self, *, reconcile_result: str = "ok") -> None:
        self.reconcile_called = False
        self.reconcile_result = reconcile_result

    def reconcile(self) -> str:
        self.reconcile_called = True
        return self.reconcile_result


class TestReconcile:
    """teamharness index reconcile 命令。"""

    def test_reconcile_detects_mismatch(
        self, database, asset_index, upsert_helper
    ):
        """declared vs actual 不一致 → mismatch 检测到。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        with database.session() as sess:
            sess.add(
                ModuleStats(
                    module_path="modules/backend",
                    declared_asset_count=5,  # actual 是 1
                    declared_submodule_count=0,
                    actual_asset_count=1,
                    actual_submodule_count=0,
                    counts_consistent=False,
                    last_synced_at=datetime.now(timezone.utc),
                    last_synced_commit="abc",
                )
            )

        svc = ReconcileService(database, sync_service=None)
        result = svc.reconcile()
        assert result.modules_checked >= 1
        assert result.modules_with_mismatch == 1
        assert len(result.mismatches) == 1
        assert result.mismatches[0]["module_path"] == "modules/backend"
        assert result.mismatches[0]["declared_asset_count"] == 5
        assert result.mismatches[0]["actual_asset_count"] == 1
        # 无 sync_service → 不触发补同步
        assert result.db_resync_triggered is False

    def test_reconcile_no_mismatch(
        self, database, asset_index, upsert_helper
    ):
        """无不一致 → modules_with_mismatch=0。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        svc = ReconcileService(database, sync_service=None)
        result = svc.reconcile()
        assert result.modules_with_mismatch == 0
        assert result.mismatches == []

    def test_reconcile_triggers_resync(
        self, database, asset_index, upsert_helper
    ):
        """不一致 + sync_service → 触发补同步。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        with database.session() as sess:
            sess.add(
                ModuleStats(
                    module_path="modules/backend",
                    declared_asset_count=5,
                    declared_submodule_count=0,
                    actual_asset_count=1,
                    actual_submodule_count=0,
                    counts_consistent=False,
                    last_synced_at=datetime.now(timezone.utc),
                    last_synced_commit="abc",
                )
            )

        mock_sync = MockSyncService(reconcile_result="resynced 1 module")
        svc = ReconcileService(database, sync_service=mock_sync)
        result = svc.reconcile()
        assert result.db_resync_triggered is True
        assert "resynced" in result.db_resync_result
        assert mock_sync.reconcile_called is True

    def test_reconcile_and_fix_forces_resync(
        self, database, asset_index, upsert_helper
    ):
        """reconcile_and_fix 即使无不一致也强制补同步。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        mock_sync = MockSyncService()
        svc = ReconcileService(database, sync_service=mock_sync)
        result = svc.reconcile_and_fix()
        assert result.db_resync_triggered is True
        assert mock_sync.reconcile_called is True

    def test_commit_sha_from_sync_state(
        self, database
    ):
        """commit_sha 从 index_sync_state 解析。"""
        with database.session() as sess:
            row = sess.get(IndexSyncState, "singleton")
            if row:
                row.last_synced_commit = "abc123"
            else:
                sess.add(
                    IndexSyncState(
                        id="singleton",
                        last_synced_commit="abc123",
                        status="ok",
                        lag_periods=0,
                    )
                )

        svc = ReconcileService(database)
        result = svc.reconcile()
        assert result.commit_sha == "abc123"

    def test_commit_sha_from_head_resolver(
        self, database
    ):
        """commit_sha 优先从 head_resolver 解析。"""
        svc = ReconcileService(
            database, head_resolver=lambda: "head-abc"
        )
        result = svc.reconcile()
        assert result.commit_sha == "head-abc"

    def test_reconcile_empty_db(
        self, database
    ):
        """空库 → 0 modules checked。"""
        svc = ReconcileService(database)
        result = svc.reconcile()
        assert result.modules_checked == 0
        assert result.modules_with_mismatch == 0

    def test_reconcile_result_to_dict(
        self, database, asset_index, upsert_helper
    ):
        """reconcile 结果可序列化为 dict。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        svc = ReconcileService(database)
        result = svc.reconcile()
        d = result.to_dict()
        assert "commit_sha" in d
        assert "modules_checked" in d
        assert "modules_with_mismatch" in d
        assert "mismatches" in d
        assert "db_resync_triggered" in d
        assert "elapsed_seconds" in d
