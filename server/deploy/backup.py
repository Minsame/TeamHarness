"""单机模式每日 cron 备份（SubTask 3.3）。

对应技术方案：单机模式每日 cron 备份 SQLite + git repo → tar.gz。

备份策略：
1. SQLite：使用 SQLite Online Backup API（sqlite3.Connection.backup），
   保证备份时数据库可读写，避免文件级复制带来的锁/不一致问题。
2. Git repo：tar 打包整个仓库目录（含 .git），保留完整版本历史。
3. 合并：SQLite 备份文件 + repo tar 包写入同一 tar.gz（带时间戳命名）。
4. 保留策略：默认保留最近 7 个备份，超出按时间倒序删除。
5. 恢复：从 tar.gz 解出 SQLite 与 repo tar，还原到指定目录。

非目标：
- 不做 PG/Qdrant 备份（docker-compose 模式走卷快照，由部署方负责）
- 不做加密（如有需求由调用方在文件系统层加密）
- 不做远程上传（保留本地，由 cron 脚本可选 rsync 到异地）
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import tarfile
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

# 默认保留备份数（每日 1 个 → 保留 1 周）
DEFAULT_RETENTION = 7

# 备份文件内部路径约定（恢复时按此路径解出）
SQLITE_BACKUP_NAME = "teamharness.db"
REPO_TAR_NAME = "repo.tar"


# ---------------------------------------------------------------------------
# 备份结果
# ---------------------------------------------------------------------------


@dataclass
class BackupResult:
    """单次备份结果。"""

    success: bool
    backup_path: Path | None = None
    size_bytes: int = 0
    sqlite_backed_up: bool = False
    repo_backed_up: bool = False
    started_at: datetime | None = None
    finished_at: datetime | None = None
    error: str | None = None
    pruned: list[Path] = field(default_factory=list)

    @property
    def duration_seconds(self) -> float:
        if self.started_at is None or self.finished_at is None:
            return 0.0
        return (self.finished_at - self.started_at).timestamp()  # type: ignore[union-attr]

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2) if self.size_bytes else 0.0


# ---------------------------------------------------------------------------
# SQLite 在线备份
# ---------------------------------------------------------------------------


def backup_sqlite(src_db: Path, dest_db: Path) -> None:
    """使用 SQLite Online Backup API 备份 SQLite 数据库。

    相比直接文件复制，Online Backup 在数据库运行时（有写入）也能拿到一致快照。
    src_db 不存在时创建空文件 dest_db（视为空库备份），便于测试。
    """
    dest_db.parent.mkdir(parents=True, exist_ok=True)
    if not src_db.exists():
        # 源库不存在：写一个空 SQLite 文件占位
        sqlite3.connect(str(dest_db)).close()
        logger.warning("源 SQLite 不存在，已创建空备份：%s", dest_db)
        return

    src = sqlite3.connect(str(src_db))
    dst = sqlite3.connect(str(dest_db))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()


# ---------------------------------------------------------------------------
# Git repo 打包
# ---------------------------------------------------------------------------


def tar_git_repo(repo_path: Path, dest_tar: Path) -> None:
    """将 git 仓库目录打包为 tar（不压缩，压缩由外层 tar.gz 统一处理）。

    保留 .git 目录以保留完整版本历史与分支信息。
    """
    dest_tar.parent.mkdir(parents=True, exist_ok=True)
    if not repo_path.exists():
        # 仓库不存在：写一个空 tar 占位
        with tarfile.open(dest_tar, "w"):
            pass
        logger.warning("源 git repo 不存在，已创建空 tar：%s", dest_tar)
        return

    with tarfile.open(dest_tar, "w") as tar:
        # arcname 设为相对 repo_path 父目录的路径，便于恢复时保留目录名
        arcname = repo_path.name
        tar.add(str(repo_path), arcname=arcname)


# ---------------------------------------------------------------------------
# 主备份流程
# ---------------------------------------------------------------------------


def run_backup(
    *,
    sqlite_path: Path | str,
    repo_path: Path | str,
    output_dir: Path | str,
    retention: int = DEFAULT_RETENTION,
    label: str | None = None,
) -> BackupResult:
    """执行单机模式备份。

    参数：
    - sqlite_path：SQLite 数据库文件路径（All-in-One / 单机模式元数据库）
    - repo_path：git 仓库根目录（含 .git）
    - output_dir：备份输出目录，tar.gz 文件写入此目录
    - retention：保留备份数，超出按时间倒序删除（默认 7）
    - label：备份文件名标签（默认取时间戳）

    返回 BackupResult，包含备份路径、大小、清理的旧备份等。
    """
    started = datetime.now()
    sqlite_path = Path(sqlite_path)
    repo_path = Path(repo_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    timestamp = started.strftime("%Y%m%d-%H%M%S")
    suffix = f"-{label}" if label else ""
    backup_name = f"teamharness-backup-{timestamp}{suffix}.tar.gz"
    backup_path = output_dir / backup_name

    result = BackupResult(
        success=False,
        backup_path=backup_path,
        started_at=started,
    )

    try:
        with tempfile.TemporaryDirectory(prefix="teamharness-backup-") as tmpdir:
            tmp = Path(tmpdir)
            sqlite_dest = tmp / SQLITE_BACKUP_NAME
            repo_tar = tmp / REPO_TAR_NAME

            # 1. SQLite 在线备份
            backup_sqlite(sqlite_path, sqlite_dest)
            result.sqlite_backed_up = True

            # 2. git repo tar
            tar_git_repo(repo_path, repo_tar)
            result.repo_backed_up = True

            # 3. 合并为 tar.gz（gzip 压缩）
            with gzip.GzipFile(filename=str(backup_path), mode="wb") as gz:
                with tarfile.open(fileobj=gz, mode="w") as tar:
                    tar.add(str(sqlite_dest), arcname=SQLITE_BACKUP_NAME)
                    tar.add(str(repo_tar), arcname=REPO_TAR_NAME)

        result.size_bytes = backup_path.stat().st_size

        # 4. 保留策略：清理超出 retention 的旧备份
        result.pruned = _prune_old_backups(output_dir, retention)

        result.success = True
        result.finished_at = datetime.now()
        logger.info(
            "备份完成：%s (%.2f MB, sqlite=%s, repo=%s, pruned=%d)",
            backup_path,
            result.size_mb,
            result.sqlite_backed_up,
            result.repo_backed_up,
            len(result.pruned),
        )
    except Exception as exc:
        result.success = False
        result.finished_at = datetime.now()
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("备份失败：%s", exc)
    return result


# ---------------------------------------------------------------------------
# 恢复流程
# ---------------------------------------------------------------------------


@dataclass
class RestoreResult:
    """单次恢复结果。"""

    success: bool
    sqlite_restored: bool = False
    repo_restored: bool = False
    error: str | None = None


def restore_backup(
    *,
    backup_path: Path | str,
    sqlite_dest: Path | str,
    repo_dest: Path | str,
    overwrite: bool = False,
) -> RestoreResult:
    """从 tar.gz 备份恢复 SQLite 与 git repo。

    参数：
    - backup_path：备份文件路径
    - sqlite_dest：SQLite 恢复目标路径（恢复后的 db 文件位置）
    - repo_dest：git repo 恢复目标目录（恢复后的 repo 根目录，文件直接落在其下）
    - overwrite：是否覆盖已存在的目标文件（默认 False，存在则报错）

    返回 RestoreResult。

    说明：
    - tar_git_repo 打包时以 repo_path.name 作为 tar 顶层目录名（保留原仓库名），
      恢复时将 tar 内容解压到 repo_dest，并把顶层目录内的文件上移到 repo_dest
      根，使 repo_dest 直接成为可用的 repo 根目录（与备份时 repo_path 同构）。
    """
    backup_path = Path(backup_path)
    sqlite_dest = Path(sqlite_dest)
    repo_dest = Path(repo_dest)
    result = RestoreResult(success=False)

    if not backup_path.exists():
        result.error = f"备份文件不存在：{backup_path}"
        return result

    if not overwrite:
        if sqlite_dest.exists():
            result.error = f"目标 SQLite 已存在（overwrite=False）：{sqlite_dest}"
            return result
        if repo_dest.exists() and any(repo_dest.iterdir()):
            result.error = f"目标 repo 目录非空（overwrite=False）：{repo_dest}"
            return result

    try:
        with tempfile.TemporaryDirectory(prefix="teamharness-restore-") as tmpdir:
            tmp = Path(tmpdir)
            with gzip.GzipFile(filename=str(backup_path), mode="rb") as gz:
                with tarfile.open(fileobj=gz, mode="r") as tar:
                    tar.extractall(str(tmp))  # noqa: S202 - 受信备份文件

            # SQLite 恢复
            src_sqlite = tmp / SQLITE_BACKUP_NAME
            if src_sqlite.exists():
                sqlite_dest.parent.mkdir(parents=True, exist_ok=True)
                if overwrite and sqlite_dest.exists():
                    sqlite_dest.unlink()
                shutil.copy2(str(src_sqlite), str(sqlite_dest))
                result.sqlite_restored = True

            # git repo 恢复
            src_repo_tar = tmp / REPO_TAR_NAME
            if src_repo_tar.exists() and src_repo_tar.stat().st_size > 0:
                repo_dest.mkdir(parents=True, exist_ok=True)
                if overwrite and repo_dest.exists():
                    shutil.rmtree(str(repo_dest))
                    repo_dest.mkdir(parents=True, exist_ok=True)
                with tarfile.open(str(src_repo_tar), "r") as tar:
                    # 解压到 repo_dest；tar 内顶层为备份时的 repo_path.name
                    tar.extractall(str(repo_dest))  # noqa: S202 - 受信备份文件
                # 若解压后 repo_dest 下只有一个子目录（tar 顶层 arcname），
                # 将该子目录内容上移到 repo_dest 根，使 repo_dest 成为 repo 根目录
                children = (
                    [p for p in repo_dest.iterdir()] if repo_dest.exists() else []
                )
                if len(children) == 1 and children[0].is_dir():
                    extracted_root = children[0]
                    for item in list(extracted_root.iterdir()):
                        target = repo_dest / item.name
                        shutil.move(str(item), str(target))
                    extracted_root.rmdir()
                result.repo_restored = True

        result.success = True
        logger.info(
            "恢复完成：sqlite=%s, repo=%s (from %s)",
            result.sqlite_restored,
            result.repo_restored,
            backup_path,
        )
    except Exception as exc:
        result.error = f"{type(exc).__name__}: {exc}"
        logger.exception("恢复失败：%s", exc)
    return result


# ---------------------------------------------------------------------------
# 保留策略：清理旧备份
# ---------------------------------------------------------------------------


def _prune_old_backups(output_dir: Path, retention: int) -> list[Path]:
    """按保留数量清理旧备份，返回被删除的备份路径列表。

    按文件名时间戳排序（命名约定保证字典序 == 时间序）。
    """
    if retention <= 0:
        return []
    backups = sorted(
        (p for p in output_dir.glob("teamharness-backup-*.tar.gz") if p.is_file()),
        key=lambda p: p.name,
    )
    if len(backups) <= retention:
        return []
    to_delete = backups[: len(backups) - retention]
    pruned: list[Path] = []
    for p in to_delete:
        try:
            p.unlink()
            pruned.append(p)
            logger.info("已清理旧备份：%s", p)
        except OSError as exc:
            logger.warning("清理旧备份失败：%s (%s)", p, exc)
    return pruned


def list_backups(output_dir: Path | str) -> list[dict[str, object]]:
    """列出备份目录中所有备份文件，按时间倒序。

    返回每条备份的 {name, size_mb, mtime}。
    """
    output_dir = Path(output_dir)
    items: list[dict[str, object]] = []
    for p in sorted(
        output_dir.glob("teamharness-backup-*.tar.gz"),
        key=lambda p: p.name,
        reverse=True,
    ):
        if not p.is_file():
            continue
        st = p.stat()
        items.append(
            {
                "name": p.name,
                "path": str(p),
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "mtime": datetime.fromtimestamp(st.st_mtime).isoformat(timespec="seconds"),
            }
        )
    return items
