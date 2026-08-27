"""SubTask 9.10+9.13: 过期归档 + Owner 接管 + 仓库大小告警测试。

覆盖：
- 过期归档（近 90 天无召回 → 归档）
- find_stale_assets（仅查询不归档）
- Owner 接管流程（批量转移 owner）
- 仓库大小告警（500MB 阈值）
- 仓库大小未超阈值
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.governance.archive_lifecycle import (
    REPO_SIZE_THRESHOLD_MB,
    STALE_RECALL_DAYS,
    ArchiveLifecycleService,
)


class TestArchiveStaleAssets:
    """过期归档（长期未引用资产）。"""

    def test_find_stale_assets(
        self, database, asset_index, archive_root, upsert_helper, recall_log_helper
    ):
        """find_stale_assets 列出近 90 天无召回的资产。"""
        upsert_helper(asset_index, id="rule-active", module_path="modules/backend")
        upsert_helper(asset_index, id="rule-stale", module_path="modules/backend")
        # rule-active 近 5 天有召回
        recall_log_helper(database, asset_id="rule-active", days_ago=5)
        # rule-stale 无召回

        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        stale_ids = svc.find_stale_assets()
        assert "rule-stale" in stale_ids
        assert "rule-active" not in stale_ids

    def test_archive_stale_assets(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """archive_stale_assets 归档无召回资产。"""
        upsert_helper(asset_index, id="rule-stale", content="# test")
        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        archived = svc.archive_stale_assets()
        assert "rule-stale" in archived
        # 归档后 status=deleted
        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            row = sess.get(AssetIndexRow, "rule-stale")
            assert row.status == "deleted"

    def test_stale_threshold_90_days(
        self, database, asset_index, archive_root, upsert_helper, recall_log_helper
    ):
        """近 90 天内有召回 → 不 stale。"""
        upsert_helper(asset_index, id="rule-x")
        recall_log_helper(database, asset_id="rule-x", days_ago=89)

        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        stale_ids = svc.find_stale_assets()
        assert "rule-x" not in stale_ids

    def test_stale_excludes_archived(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """已 deleted 的资产不纳入 stale 检测。"""
        from server.infra_db.models import AssetIndex as AssetIndexRow
        from sqlalchemy import update

        upsert_helper(asset_index, id="rule-archived")
        with database.session() as sess:
            sess.execute(
                update(AssetIndexRow)
                .where(AssetIndexRow.id == "rule-archived")
                .values(status="deleted")
            )

        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        stale_ids = svc.find_stale_assets()
        assert "rule-archived" not in stale_ids


class TestOwnerTakeover:
    """Owner 接管流程。"""

    def test_takeover_all_assets(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """转移 old_owner 名下全部资产。"""
        upsert_helper(asset_index, id="r1", owner="alice")
        upsert_helper(asset_index, id="r2", owner="alice")
        upsert_helper(asset_index, id="r3", owner="bob")

        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        result = svc.owner_takeover(old_owner="alice", new_owner="carol")
        assert result.success is True
        assert len(result.asset_ids) == 2
        assert set(result.asset_ids) == {"r1", "r2"}

        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            r1 = sess.get(AssetIndexRow, "r1")
            r2 = sess.get(AssetIndexRow, "r2")
            r3 = sess.get(AssetIndexRow, "r3")
            assert r1.owner == "carol"
            assert r2.owner == "carol"
            assert r3.owner == "bob"  # 未变

    def test_takeover_specific_assets(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """仅转移指定资产。"""
        upsert_helper(asset_index, id="r1", owner="alice")
        upsert_helper(asset_index, id="r2", owner="alice")

        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        result = svc.owner_takeover(
            old_owner="alice", new_owner="carol", asset_ids=["r1"]
        )
        assert len(result.asset_ids) == 1

        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            r1 = sess.get(AssetIndexRow, "r1")
            r2 = sess.get(AssetIndexRow, "r2")
            assert r1.owner == "carol"
            assert r2.owner == "alice"  # 未变

    def test_takeover_no_assets(
        self, database, archive_root
    ):
        """old_owner 名下无资产 → 失败。"""
        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        result = svc.owner_takeover(old_owner="nobody", new_owner="carol")
        assert result.success is False
        assert "无资产" in result.error


class TestRepoSizeAlert:
    """仓库大小告警（500MB 阈值）。"""

    def test_repo_size_within_threshold(
        self, database, archive_root, tmp_path
    ):
        """仓库大小 < 500MB → exceeded=False。"""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / "rules").mkdir()
        (repo_root / "rules" / "test.md").write_text("# test", encoding="utf-8")

        svc = ArchiveLifecycleService(
            database, archive_root=archive_root, repo_root=repo_root
        )
        alerts = svc.check_repo_size()
        assert len(alerts) == 1
        assert alerts[0].exceeded is False
        assert alerts[0].threshold_mb == REPO_SIZE_THRESHOLD_MB

    def test_repo_size_exceeded(
        self, database, archive_root, tmp_path
    ):
        """仓库大小 > 阈值 → exceeded=True。"""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        # 写入 > 1MB 文件（用低阈值测试）
        big_file = repo_root / "big.bin"
        big_file.write_bytes(b"\x00" * (2 * 1024 * 1024))

        svc = ArchiveLifecycleService(
            database,
            archive_root=archive_root,
            repo_root=repo_root,
            repo_size_threshold_mb=1,  # 1MB 阈值
        )
        alerts = svc.check_repo_size()
        assert len(alerts) == 1
        assert alerts[0].exceeded is True
        assert alerts[0].size_mb > 1.0

    def test_no_repo_root(
        self, database, archive_root
    ):
        """未配置 repo_root → 空告警列表。"""
        svc = ArchiveLifecycleService(database, archive_root=archive_root)
        assert svc.check_repo_size() == []

    def test_scan_ignores_git_dir(
        self, database, archive_root, tmp_path
    ):
        """扫描忽略 .git / node_modules 目录。"""
        repo_root = tmp_path / "repo"
        repo_root.mkdir()
        (repo_root / ".git").mkdir()
        (repo_root / ".git" / "objects").mkdir()
        (repo_root / ".git" / "objects" / "big.bin").write_bytes(
            b"\x00" * (10 * 1024 * 1024)  # 10MB in .git
        )
        (repo_root / "rules").mkdir()
        (repo_root / "rules" / "test.md").write_text("# test", encoding="utf-8")

        svc = ArchiveLifecycleService(
            database,
            archive_root=archive_root,
            repo_root=repo_root,
            repo_size_threshold_mb=1,
        )
        alerts = svc.check_repo_size()
        # .git 被忽略 → 不超阈值
        assert alerts[0].exceeded is False
