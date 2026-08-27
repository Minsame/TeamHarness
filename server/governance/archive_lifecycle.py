"""ArchiveLifecycleService — 过期归档 + Owner 接管 + 仓库大小告警（SubTask 9.10）。

对应技术方案 3.3.8 + 8.16：
- 过期归档：长期未引用资产（近 N 天无召回）自动归档
- Owner 接管流程：Owner 离开团队时批量转移资产归属
- 仓库大小告警：500MB 阈值触发 critical 告警

设计要点：
- 过期归档委托 SemanticArchiveService（复用归档逻辑，黄线遵守）
- Owner 接管：批量更新 asset_index.owner + agent_binding 不变（装配按 asset_id 索引）
- 仓库大小：扫描 repo_root 目录树统计字节数（含 .git + archive）
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import func, select, update

from server.governance.archive import SemanticArchiveService
from server.governance.models import OwnerTakeoverResult, RepoSizeAlert
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow, RecallLog

logger = logging.getLogger(__name__)


# 过期归档阈值：近 90 天无召回 → 归档
STALE_RECALL_DAYS = 90
# 仓库大小告警阈值（500MB）
REPO_SIZE_THRESHOLD_MB = 500
# 仓库大小扫描忽略目录
REPO_SCAN_IGNORE_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}


class ArchiveLifecycleService:
    """归档生命周期服务。

    用法：
        svc = ArchiveLifecycleService(database, archive_root="archive", repo_root=".")
        # 过期归档
        archived = svc.archive_stale_assets()
        # Owner 接管
        result = svc.owner_takeover(old_owner="alice", new_owner="bob")
        # 仓库大小告警
        alerts = svc.check_repo_size()
    """

    def __init__(
        self,
        database: Database,
        *,
        archive_root: str | Path = "archive",
        repo_root: str | Path | None = None,
        stale_days: int = STALE_RECALL_DAYS,
        repo_size_threshold_mb: int = REPO_SIZE_THRESHOLD_MB,
    ) -> None:
        self._db = database
        self._archive = SemanticArchiveService(database, archive_root=archive_root)
        self._repo_root = Path(repo_root) if repo_root else None
        self._stale_days = stale_days
        self._repo_size_threshold_mb = repo_size_threshold_mb

    # ------------------------------------------------------------------
    # 过期归档（长期未引用资产）
    # ------------------------------------------------------------------

    def archive_stale_assets(self) -> list[str]:
        """归档近 N 天无召回的资产。

        - 查询近 stale_days 无 recall_log 的 active 资产
        - 调用 SemanticArchiveService 归档（移入 archive/<date>/）
        - 返回归档的 asset_id 列表
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._stale_days)
        with self._db.session() as sess:
            # 近 N 天有召回的 asset_id 集合
            recent_recall_stmt = (
                select(RecallLog.asset_id)
                .where(RecallLog.recalled_at >= cutoff)
                .distinct()
            )
            recent_recalled: set[str] = set(sess.scalars(recent_recall_stmt))

            # 全部 active 资产
            active_stmt = (
                select(AssetIndexRow.id).where(AssetIndexRow.status == "active")
            )
            all_active: list[str] = list(sess.scalars(active_stmt))

        stale_ids = [aid for aid in all_active if aid not in recent_recalled]
        archived: list[str] = []
        for asset_id in stale_ids:
            try:
                self._archive.archive_asset(
                    asset_id=asset_id, reason="stale_no_recall"
                )
                archived.append(asset_id)
                logger.info("过期归档 asset_id=%s（近 %d 天无召回）", asset_id, self._stale_days)
            except Exception as exc:  # noqa: BLE001
                logger.warning("过期归档失败 asset_id=%s err=%s", asset_id, exc)
        return archived

    def find_stale_assets(self) -> list[str]:
        """列出近 N 天无召回的资产（不归档，仅查询）。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=self._stale_days)
        with self._db.session() as sess:
            recent_recall_stmt = (
                select(RecallLog.asset_id)
                .where(RecallLog.recalled_at >= cutoff)
                .distinct()
            )
            recent_recalled: set[str] = set(sess.scalars(recent_recall_stmt))
            active_stmt = (
                select(AssetIndexRow.id).where(AssetIndexRow.status == "active")
            )
            all_active: list[str] = list(sess.scalars(active_stmt))
        return [aid for aid in all_active if aid not in recent_recalled]

    # ------------------------------------------------------------------
    # Owner 接管流程
    # ------------------------------------------------------------------

    def owner_takeover(
        self,
        *,
        old_owner: str,
        new_owner: str,
        asset_ids: list[str] | None = None,
    ) -> OwnerTakeoverResult:
        """Owner 离开团队时批量转移资产归属。

        - asset_ids=None → 转移 old_owner 名下全部资产
        - 更新 asset_index.owner = new_owner
        - agent_binding 不变（装配按 asset_id 索引，与 owner 无关）
        - 返回 OwnerTakeoverResult
        """
        result = OwnerTakeoverResult(old_owner=old_owner, new_owner=new_owner)
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.owner == old_owner)
            )
            if asset_ids:
                stmt = stmt.where(AssetIndexRow.id.in_(asset_ids))
            rows = list(sess.scalars(stmt))
            if not rows:
                result.success = False
                result.error = f"Owner {old_owner} 名下无资产"
                return result
            for row in rows:
                try:
                    sess.execute(
                        update(AssetIndexRow)
                        .where(AssetIndexRow.id == row.id)
                        .values(owner=new_owner)
                    )
                    result.asset_ids.append(row.id)
                except Exception as exc:  # noqa: BLE001
                    result.failed.append(
                        {"asset_id": row.id, "error": str(exc)}
                    )
        result.success = not result.failed
        logger.info(
            "Owner 接管完成 old=%s new=%s transferred=%d failed=%d",
            old_owner, new_owner, len(result.asset_ids), len(result.failed),
        )
        return result

    # ------------------------------------------------------------------
    # 仓库大小告警（500MB 阈值）
    # ------------------------------------------------------------------

    def check_repo_size(self) -> list:
        """扫描仓库大小，超过阈值返回告警。"""
        if self._repo_root is None:
            return []
        repo_path = str(self._repo_root)
        if not self._repo_root.is_dir():
            return []
        size_bytes = self._compute_dir_size(self._repo_root)
        size_mb = size_bytes / (1024 * 1024)
        exceeded = size_mb > self._repo_size_threshold_mb
        alert = RepoSizeAlert(
            repo_path=repo_path,
            size_bytes=size_bytes,
            size_mb=round(size_mb, 2),
            threshold_mb=self._repo_size_threshold_mb,
            exceeded=exceeded,
        )
        if exceeded:
            logger.warning(
                "仓库大小告警 path=%s size=%.2fMB > %dMB",
                repo_path, size_mb, self._repo_size_threshold_mb,
            )
        return [alert]

    def _compute_dir_size(self, path: Path) -> int:
        """递归计算目录大小（忽略 .git / node_modules 等）。"""
        total = 0
        try:
            for entry in path.iterdir():
                if entry.name in REPO_SCAN_IGNORE_DIRS:
                    continue
                if entry.is_dir():
                    total += self._compute_dir_size(entry)
                elif entry.is_file():
                    try:
                        total += entry.stat().st_size
                    except OSError:
                        continue
        except (OSError, PermissionError) as exc:
            logger.warning("扫描目录大小失败 path=%s err=%s", path, exc)
        return total


__all__ = [
    "REPO_SCAN_IGNORE_DIRS",
    "REPO_SIZE_THRESHOLD_MB",
    "STALE_RECALL_DAYS",
    "ArchiveLifecycleService",
]
