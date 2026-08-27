"""备份与恢复测试（SubTask 3.7）。

覆盖：
- run_backup 完整流程（SQLite + git repo → tar.gz）
- 备份文件结构（含 teamharness.db + repo.tar）
- restore_backup 恢复 SQLite + repo
- 保留策略 _prune_old_backups
- list_backups 倒序
- 边界：源不存在 / 空库 / overwrite=False
- 备份失败时的 BackupResult.error
"""

from __future__ import annotations

import sqlite3
import tarfile
from pathlib import Path

import pytest

from server.deploy.backup import (
    DEFAULT_RETENTION,
    SQLITE_BACKUP_NAME,
    REPO_TAR_NAME,
    BackupResult,
    RestoreResult,
    _prune_old_backups,
    backup_sqlite,
    list_backups,
    restore_backup,
    run_backup,
    tar_git_repo,
)


# ---------------------------------------------------------------------------
# 测试夹具
# ---------------------------------------------------------------------------


@pytest.fixture
def sqlite_db(tmp_path: Path) -> Path:
    """创建带数据的 SQLite 测试库。"""
    db_path = tmp_path / "src.db"
    conn = sqlite3.connect(str(db_path))
    conn.execute("CREATE TABLE asset_index(id TEXT PRIMARY KEY, type TEXT)")
    conn.execute("INSERT INTO asset_index VALUES (?, ?)", ("rule-1", "rule"))
    conn.execute("INSERT INTO asset_index VALUES (?, ?)", ("memory-1", "memory"))
    conn.commit()
    conn.close()
    return db_path


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """创建带文件的伪 git 仓库目录。"""
    repo_path = tmp_path / "teamharness-shared"
    repo_path.mkdir()
    (repo_path / "INDEX.md").write_text("---\nlevel: project\n---\n")
    (repo_path / "rules").mkdir()
    (repo_path / "rules" / "lint.md").write_text("# lint rule\n")
    # 模拟 .git 目录
    git_dir = repo_path / ".git"
    git_dir.mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    return repo_path


# ---------------------------------------------------------------------------
# backup_sqlite
# ---------------------------------------------------------------------------


class TestBackupSqlite:
    def test_正常备份(self, sqlite_db: Path, tmp_path: Path) -> None:
        dest = tmp_path / "backup.db"
        backup_sqlite(sqlite_db, dest)
        assert dest.exists()

        # 验证备份数据一致
        conn = sqlite3.connect(str(dest))
        rows = conn.execute("SELECT id, type FROM asset_index ORDER BY id").fetchall()
        conn.close()
        assert rows == [("memory-1", "memory"), ("rule-1", "rule")]

    def test_源不存在创建空备份(self, tmp_path: Path) -> None:
        src = tmp_path / "nonexistent.db"
        dest = tmp_path / "empty.db"
        backup_sqlite(src, dest)
        assert dest.exists()
        # 空库可正常打开
        conn = sqlite3.connect(str(dest))
        conn.execute("SELECT 1")
        conn.close()

    def test_备份期间源库仍可读写(self, sqlite_db: Path, tmp_path: Path) -> None:
        """SQLite Online Backup API 保证备份期间源库可读写。"""
        dest = tmp_path / "concurrent.db"
        # 在源库打开连接时备份（模拟运行时备份）
        src_conn = sqlite3.connect(str(sqlite_db))
        try:
            backup_sqlite(sqlite_db, dest)
            # 备份过程中源仍可查询
            rows = src_conn.execute("SELECT COUNT(*) FROM asset_index").fetchone()
            assert rows[0] == 2
        finally:
            src_conn.close()
        assert dest.exists()


# ---------------------------------------------------------------------------
# tar_git_repo
# ---------------------------------------------------------------------------


class TestTarGitRepo:
    def test_正常打包(self, git_repo: Path, tmp_path: Path) -> None:
        dest = tmp_path / "repo.tar"
        tar_git_repo(git_repo, dest)
        assert dest.exists()
        assert dest.stat().st_size > 0

        # 解 tar 验证内容
        with tarfile.open(str(dest), "r") as tar:
            names = tar.getnames()
            assert any("INDEX.md" in n for n in names)
            assert any("rules/lint.md" in n for n in names)
            assert any(".git/HEAD" in n for n in names)

    def test_源不存在创建空tar(self, tmp_path: Path) -> None:
        src = tmp_path / "nonexistent-repo"
        dest = tmp_path / "empty.tar"
        tar_git_repo(src, dest)
        assert dest.exists()
        with tarfile.open(str(dest), "r") as tar:
            assert tar.getnames() == []


# ---------------------------------------------------------------------------
# run_backup 完整流程
# ---------------------------------------------------------------------------


class TestRunBackup:
    def test_完整备份成功(self, sqlite_db: Path, git_repo: Path, tmp_path: Path) -> None:
        output_dir = tmp_path / "backups"
        result = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=output_dir,
        )
        assert result.success is True
        assert result.backup_path is not None
        assert result.backup_path.exists()
        assert result.size_bytes > 0
        assert result.sqlite_backed_up is True
        assert result.repo_backed_up is True
        assert result.error is None
        assert result.started_at is not None
        assert result.finished_at is not None

        # tar.gz 内含两个文件
        import gzip

        with gzip.GzipFile(str(result.backup_path), "rb") as gz:
            with tarfile.open(fileobj=gz, mode="r") as tar:
                names = tar.getnames()
                assert SQLITE_BACKUP_NAME in names
                assert REPO_TAR_NAME in names

    def test_备份文件名带时间戳(self, sqlite_db: Path, git_repo: Path, tmp_path: Path) -> None:
        result = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=tmp_path / "out",
            label="nightly",
        )
        assert result.backup_path is not None
        assert "teamharness-backup-" in result.backup_path.name
        assert "nightly" in result.backup_path.name
        assert result.backup_path.suffix == ".gz"

    def test_备份结果size_mb计算(self, sqlite_db: Path, git_repo: Path, tmp_path: Path) -> None:
        result = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=tmp_path / "out",
        )
        # size_bytes 必为正；size_mb 是 round 到 2 位小数，小备份可能为 0.0
        assert result.size_bytes > 0
        assert isinstance(result.size_mb, float)
        assert result.size_mb >= 0

    def test_源均不存在仍生成备份(self, tmp_path: Path) -> None:
        """源 SQLite / repo 均不存在时仍生成空备份（降级而非失败）。"""
        result = run_backup(
            sqlite_path=tmp_path / "no.db",
            repo_path=tmp_path / "no-repo",
            output_dir=tmp_path / "out",
        )
        assert result.success is True
        assert result.backup_path is not None
        assert result.backup_path.exists()


# ---------------------------------------------------------------------------
# 保留策略
# ---------------------------------------------------------------------------


class TestPruneOldBackups:
    def test_超出retention清理最旧(self, tmp_path: Path) -> None:
        # 创建 10 个备份文件
        for i in range(10):
            (tmp_path / f"teamharness-backup-2024010{i}-030000.tar.gz").write_bytes(b"x")
        pruned = _prune_old_backups(tmp_path, retention=7)
        assert len(pruned) == 3
        # 剩 7 个
        remaining = list(tmp_path.glob("teamharness-backup-*.tar.gz"))
        assert len(remaining) == 7
        # 清理的是最旧 3 个
        pruned_names = {p.name for p in pruned}
        assert "teamharness-backup-20240100-030000.tar.gz" in pruned_names
        assert "teamharness-backup-20240101-030000.tar.gz" in pruned_names
        assert "teamharness-backup-20240102-030000.tar.gz" in pruned_names

    def test_未超retention不清理(self, tmp_path: Path) -> None:
        for i in range(3):
            (tmp_path / f"teamharness-backup-2024010{i}-030000.tar.gz").write_bytes(b"x")
        pruned = _prune_old_backups(tmp_path, retention=7)
        assert pruned == []

    def test_retention_0不清理(self, tmp_path: Path) -> None:
        for i in range(3):
            (tmp_path / f"teamharness-backup-2024010{i}-030000.tar.gz").write_bytes(b"x")
        pruned = _prune_old_backups(tmp_path, retention=0)
        assert pruned == []

    def test_只匹配备份文件名格式(self, tmp_path: Path) -> None:
        """非备份文件名的 .tar.gz 不被清理。"""
        (tmp_path / "teamharness-backup-20240101-030000.tar.gz").write_bytes(b"x")
        (tmp_path / "random-file.tar.gz").write_bytes(b"x")
        (tmp_path / "teamharness-backup-20240102-030000.tar.gz").write_bytes(b"x")
        # retention=1 应只清理一个 teamharness-backup-，保留 random-file
        pruned = _prune_old_backups(tmp_path, retention=1)
        assert len(pruned) == 1
        assert (tmp_path / "random-file.tar.gz").exists()

    def test_run_backup触发清理(self, sqlite_db: Path, git_repo: Path, tmp_path: Path) -> None:
        """run_backup 内部调用清理，返回 pruned 列表。"""
        out = tmp_path / "out"
        out.mkdir()
        # 预先放 5 个伪备份
        for i in range(5):
            (out / f"teamharness-backup-2023010{i}-030000.tar.gz").write_bytes(b"old")
        result = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=out,
            retention=3,
        )
        assert result.success is True
        # 5 + 1 = 6 个，保留 3 → 清理 3 个
        assert len(result.pruned) == 3


# ---------------------------------------------------------------------------
# restore_backup
# ---------------------------------------------------------------------------


class TestRestoreBackup:
    def test_完整恢复(self, sqlite_db: Path, git_repo: Path, tmp_path: Path) -> None:
        # 先备份
        backup_path = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=tmp_path / "backup",
        ).backup_path
        assert backup_path is not None

        # 恢复到新位置
        restore_sqlite = tmp_path / "restored.db"
        restore_repo = tmp_path / "restored-repo"
        result = restore_backup(
            backup_path=backup_path,
            sqlite_dest=restore_sqlite,
            repo_dest=restore_repo,
        )
        assert result.success is True
        assert result.sqlite_restored is True
        assert result.repo_restored is True

        # 验证 SQLite 数据一致（cursor 返回元组，单列也是元组）
        conn = sqlite3.connect(str(restore_sqlite))
        rows = conn.execute("SELECT id FROM asset_index ORDER BY id").fetchall()
        conn.close()
        assert rows == [("memory-1",), ("rule-1",)]

        # 验证 repo 内容一致
        assert (restore_repo / "INDEX.md").exists()
        assert (restore_repo / "rules" / "lint.md").exists()
        assert (restore_repo / ".git" / "HEAD").exists()

    def test_备份不存在失败(self, tmp_path: Path) -> None:
        result = restore_backup(
            backup_path=tmp_path / "nonexistent.tar.gz",
            sqlite_dest=tmp_path / "out.db",
            repo_dest=tmp_path / "out-repo",
        )
        assert result.success is False
        assert "不存在" in (result.error or "")

    def test_目标已存在overwrite_False失败(
        self, sqlite_db: Path, git_repo: Path, tmp_path: Path
    ) -> None:
        backup_path = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=tmp_path / "backup",
        ).backup_path
        assert backup_path is not None

        # 预先创建目标文件
        existing_sqlite = tmp_path / "existing.db"
        existing_sqlite.write_bytes(b"old")

        result = restore_backup(
            backup_path=backup_path,
            sqlite_dest=existing_sqlite,
            repo_dest=tmp_path / "out-repo",
            overwrite=False,
        )
        assert result.success is False
        assert "已存在" in (result.error or "")

    def test_overwrite_True覆盖目标(
        self, sqlite_db: Path, git_repo: Path, tmp_path: Path
    ) -> None:
        backup_path = run_backup(
            sqlite_path=sqlite_db,
            repo_path=git_repo,
            output_dir=tmp_path / "backup",
        ).backup_path
        assert backup_path is not None

        existing_sqlite = tmp_path / "existing.db"
        existing_sqlite.write_bytes(b"old")

        result = restore_backup(
            backup_path=backup_path,
            sqlite_dest=existing_sqlite,
            repo_dest=tmp_path / "out-repo",
            overwrite=True,
        )
        assert result.success is True
        # 文件已被覆盖
        assert existing_sqlite.stat().st_size > 1


# ---------------------------------------------------------------------------
# list_backups
# ---------------------------------------------------------------------------


class TestListBackups:
    def test_按时间倒序(self, tmp_path: Path) -> None:
        for i in range(5):
            (tmp_path / f"teamharness-backup-2024010{i}-030000.tar.gz").write_bytes(b"x")
        items = list_backups(tmp_path)
        assert len(items) == 5
        # 倒序：最新（05）在前
        assert "20240104" in items[0]["name"]
        assert "20240100" in items[-1]["name"]

    def test_包含size与mtime(self, tmp_path: Path) -> None:
        (tmp_path / "teamharness-backup-20240101-030000.tar.gz").write_bytes(b"hello")
        items = list_backups(tmp_path)
        assert len(items) == 1
        assert items[0]["size_mb"] is not None
        assert "mtime" in items[0]

    def test_空目录返回空列表(self, tmp_path: Path) -> None:
        assert list_backups(tmp_path) == []

    def test_只列备份格式文件(self, tmp_path: Path) -> None:
        (tmp_path / "teamharness-backup-20240101-030000.tar.gz").write_bytes(b"x")
        (tmp_path / "random.tar.gz").write_bytes(b"x")
        (tmp_path / "not-a-tar.txt").write_bytes(b"x")
        items = list_backups(tmp_path)
        assert len(items) == 1
