"""SubTask 9.2 + 9.13: 语义归档测试。

覆盖：
- 归档后资产移入 archive/<date>/
- 归档后 asset_index.status='deleted'
- manifest 持久化
- 清理过期归档（hard_delete_at 到期）
- 幂等归档（已归档资产不重复处理）
- 批量归档
- 禁止直接删除（黄线遵守）
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from server.governance.archive import (
    ARCHIVE_TTL_DAYS,
    SemanticArchiveService,
)


class TestArchiveAsset:
    """单条资产归档。"""

    def test_archive_moves_to_archive_dir(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """归档后内容写入 archive/<date>/<asset_id>.md。"""
        upsert_helper(
            asset_index,
            id="rule-x",
            content="# lint rule\n禁止 print",
        )
        svc = SemanticArchiveService(database, archive_root=archive_root)
        record = svc.archive_asset(asset_id="rule-x", reason="semantic_merge")

        assert record.asset_id == "rule-x"
        assert record.reason == "semantic_merge"
        # 归档文件存在
        archive_file = archive_root / record.archive_path
        assert archive_file.is_file()
        content = archive_file.read_text(encoding="utf-8")
        assert "禁止 print" in content

    def test_archive_sets_status_deleted(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """归档后 asset_index.status='deleted'。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(database, archive_root=archive_root)
        svc.archive_asset(asset_id="rule-x")

        from server.infra_db.models import AssetIndex as AssetIndexRow
        from sqlalchemy import select
        with database.session() as sess:
            row = sess.get(AssetIndexRow, "rule-x")
            assert row.status == "deleted"

    def test_archive_writes_manifest(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """归档后 manifest 含记录。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(database, archive_root=archive_root)
        svc.archive_asset(asset_id="rule-x")

        manifest_path = archive_root / "_manifest.json"
        assert manifest_path.is_file()
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert len(manifest) == 1
        assert manifest[0]["asset_id"] == "rule-x"
        assert "hard_delete_at" in manifest[0]

    def test_archive_idempotent(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """重复归档同一资产 → 幂等返回已有记录。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(database, archive_root=archive_root)
        r1 = svc.archive_asset(asset_id="rule-x")
        r2 = svc.archive_asset(asset_id="rule-x")
        assert r1.asset_id == r2.asset_id
        # manifest 仅一条记录
        manifest = svc._read_manifest()
        assert len(manifest) == 1

    def test_archive_nonexistent_raises(
        self, database, archive_root
    ):
        """归档不存在的资产 → ValueError。"""
        svc = SemanticArchiveService(database, archive_root=archive_root)
        with pytest.raises(ValueError, match="资产不存在"):
            svc.archive_asset(asset_id="nonexistent")


class TestArchiveBatch:
    """批量归档。"""

    def test_batch_archive(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """批量归档多资产。"""
        upsert_helper(asset_index, id="rule-a", content="# a")
        upsert_helper(asset_index, id="rule-b", content="# b")
        upsert_helper(asset_index, id="rule-c", content="# c")

        svc = SemanticArchiveService(database, archive_root=archive_root)
        result = svc.archive_batch(["rule-a", "rule-b", "rule-c"])
        assert result.archived_count == 3
        assert len(result.failed) == 0

    def test_batch_partial_failure(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """批量归档含不存在的资产 → 部分失败。"""
        upsert_helper(asset_index, id="rule-a", content="# a")
        svc = SemanticArchiveService(database, archive_root=archive_root)
        result = svc.archive_batch(["rule-a", "nonexistent"])
        assert result.archived_count == 1
        assert len(result.failed) == 1
        assert result.failed[0]["asset_id"] == "nonexistent"


class TestCleanupExpired:
    """过期归档清理。"""

    def test_cleanup_deletes_expired_files(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """hard_delete_at 到期 → 删除归档文件。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(
            database, archive_root=archive_root, ttl_days=1
        )
        record = svc.archive_asset(asset_id="rule-x")

        # 手动将 hard_delete_at 改为过去时间
        manifest = svc._read_manifest()
        manifest[0]["hard_delete_at"] = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
        svc._write_manifest(manifest)

        archive_file = archive_root / record.archive_path
        assert archive_file.is_file()

        cleaned = svc.cleanup_expired()
        assert cleaned == 1
        assert not archive_file.is_file()

    def test_cleanup_keeps_unexpired(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """未过期的归档文件保留。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(
            database, archive_root=archive_root, ttl_days=180
        )
        record = svc.archive_asset(asset_id="rule-x")
        archive_file = archive_root / record.archive_path

        cleaned = svc.cleanup_expired()
        assert cleaned == 0
        assert archive_file.is_file()

    def test_cleanup_empty_manifest(
        self, database, archive_root
    ):
        """空 manifest → 清理 0 条。"""
        svc = SemanticArchiveService(database, archive_root=archive_root)
        assert svc.cleanup_expired() == 0


class TestListArchived:
    """归档记录查询。"""

    def test_list_archived(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """列出归档记录。"""
        upsert_helper(asset_index, id="rule-a", content="# a")
        upsert_helper(asset_index, id="rule-b", content="# b")
        svc = SemanticArchiveService(database, archive_root=archive_root)
        svc.archive_asset(asset_id="rule-a")
        svc.archive_asset(asset_id="rule-b")

        records = svc.list_archived()
        assert len(records) == 2
        asset_ids = {r.asset_id for r in records}
        assert asset_ids == {"rule-a", "rule-b"}

    def test_find_pending_cleanup(
        self, database, asset_index, archive_root, upsert_helper
    ):
        """列出已过期待清理记录。"""
        upsert_helper(asset_index, id="rule-x", content="# test")
        svc = SemanticArchiveService(
            database, archive_root=archive_root, ttl_days=1
        )
        svc.archive_asset(asset_id="rule-x")

        # 改为已过期
        manifest = svc._read_manifest()
        manifest[0]["hard_delete_at"] = (
            datetime.now(timezone.utc) - timedelta(days=1)
        ).isoformat()
        svc._write_manifest(manifest)

        pending = svc.find_pending_cleanup()
        assert len(pending) == 1
        assert pending[0].asset_id == "rule-x"


class TestNoDirectDeletion:
    """黄线遵守：禁止直接文件删除。"""

    def test_archive_does_not_delete_git_path(
        self, database, asset_index, archive_root, upsert_helper, tmp_path
    ):
        """归档仅移入 archive/，不删除 git 原文件。"""
        # 模拟 git 原文件
        git_file = tmp_path / "rules" / "rule-x.md"
        git_file.parent.mkdir(parents=True, exist_ok=True)
        git_file.write_text("# lint rule\n禁止 print", encoding="utf-8")

        upsert_helper(
            asset_index,
            id="rule-x",
            content="# lint rule\n禁止 print",
            git_path="rules/rule-x.md",
        )
        svc = SemanticArchiveService(database, archive_root=archive_root)
        svc.archive_asset(asset_id="rule-x")

        # git 原文件仍存在（未被删除）
        assert git_file.is_file()
