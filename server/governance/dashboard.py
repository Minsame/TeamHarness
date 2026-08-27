"""DashboardService — 治理看板服务（SubTask 9.3）。

对应技术方案 3.3.7 治理看板，聚合：
- module_stats：模块资产数（从 asset_index 实时派生，缺陷 8.1 修复）
- split_suggestions：拆分建议（基于实时派生 counts）
- orphan_asset_alerts：未登记告警（module_path 为空或无 module_stats 声明的资产）
- recall_hit_rates：召回命中率（recall_log 实时聚合）
- adoption_rates：采纳率（服务端可采，recall_log 主信号）
- repo_size_alerts：仓库大小告警（500MB 阈值）

设计要点：
- 全部数据实时派生，不依赖缓存（看板请求时聚合）
- module_stats / split_suggestions 委托 ModuleStatsService（缺陷 8.1 修复核心）
- recall_hit_rates / adoption_rates 从 recall_log 实时聚合（服务端可采红线遵守）
- repo_size_alerts 委托 ArchiveLifecycleService 或直接计算
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import case, func, select

from server.governance.adoption import AdoptionMetricsService
from server.governance.archive import SemanticArchiveService
from server.governance.models import (
    DashboardAlert,
    DashboardData,
    ModuleRecallHitRate,
)
from server.governance.module_stats import ModuleStatsService
from server.infra_db.db import Database
from server.infra_db.models import (
    AdoptionEvent,
    AssetIndex as AssetIndexRow,
    RecallLog,
)

logger = logging.getLogger(__name__)


# 召回命中率统计窗口（近 30 天，与采纳率窗口对齐）
RECALL_WINDOW_DAYS = 30


class DashboardService:
    """治理看板聚合服务。

    用法：
        svc = DashboardService(database, archive_root="archive")
        data = svc.get_dashboard()
        overview = svc.get_overview()
    """

    def __init__(
        self,
        database: Database,
        *,
        archive_root: str | Any = "archive",
    ) -> None:
        self._db = database
        self._module_stats = ModuleStatsService(database)
        self._adoption = AdoptionMetricsService(database)
        self._archive = SemanticArchiveService(database, archive_root=archive_root)

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def get_dashboard(self) -> DashboardData:
        """聚合全部治理看板数据。"""
        module_stats = self._module_stats.compute_all_modules()
        split_suggestions = self._module_stats.detect_split_suggestions()
        orphan_alerts = self._detect_orphan_assets()
        recall_hit_rates = self._compute_recall_hit_rates()
        adoption_rates = self._collect_adoption_rates()
        repo_size_alerts = self._check_repo_size()

        # 归档过期告警
        pending_cleanup = self._archive.find_pending_cleanup()
        for record in pending_cleanup:
            orphan_alerts.append(
                DashboardAlert(
                    level="warning",
                    category="archive_overdue",
                    message=(
                        f"归档资产 {record.asset_id} 已过 hard_delete_at，"
                        f"待 cleanup_expired 清理"
                    ),
                    asset_id=record.asset_id,
                    extra={"archive_path": record.archive_path},
                )
            )

        # counts 不一致告警
        for s in module_stats:
            if s.has_mismatch:
                orphan_alerts.append(
                    DashboardAlert(
                        level="warning",
                        category="counts_mismatch",
                        message=(
                            f"模块 {s.module_path} counts 不一致："
                            f"declared(assets={s.declared_asset_count}, "
                            f"submodules={s.declared_submodule_count}) "
                            f"!= actual(assets={s.actual_asset_count}, "
                            f"submodules={s.actual_submodule_count})"
                        ),
                        module_path=s.module_path,
                    )
                )

        return DashboardData(
            module_stats=module_stats,
            split_suggestions=split_suggestions,
            orphan_asset_alerts=orphan_alerts,
            recall_hit_rates=recall_hit_rates,
            adoption_rates=[a.to_dict() for a in adoption_rates],
            repo_size_alerts=repo_size_alerts,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )

    def get_overview(self) -> dict[str, Any]:
        """概览统计（轻量，不含全量模块明细）。"""
        with self._db.session() as sess:
            total = int(
                sess.scalar(
                    select(func.count(AssetIndexRow.id))
                )
                or 0
            )
            active = int(
                sess.scalar(
                    select(func.count(AssetIndexRow.id)).where(
                        AssetIndexRow.status == "active"
                    )
                )
                or 0
            )
            module_count = int(
                sess.scalar(
                    select(func.count())
                    .select_from(
                        select(AssetIndexRow.module_path)
                        .where(AssetIndexRow.module_path != "")
                        .distinct()
                        .subquery()
                    )
                )
                or 0
            )
            orphan_count = int(
                sess.scalar(
                    select(func.count(AssetIndexRow.id)).where(
                        AssetIndexRow.module_path == "",
                        AssetIndexRow.status == "active",
                    )
                )
                or 0
            )
        return {
            "asset_total": total,
            "asset_active": active,
            "module_count": module_count,
            "orphan_asset_count": orphan_count,
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }

    # ------------------------------------------------------------------
    # 内部：未登记告警（orphan assets）
    # ------------------------------------------------------------------

    def _detect_orphan_assets(self) -> list[DashboardAlert]:
        """检测未登记资产：module_path 为空的 active 资产。"""
        alerts: list[DashboardAlert] = []
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.module_path == "")
            )
            for row in sess.scalars(stmt):
                alerts.append(
                    DashboardAlert(
                        level="warning",
                        category="orphan_asset",
                        message=(
                            f"资产 {row.id} 未登记到任何模块"
                            f"（module_path 为空，git_path={row.git_path}）"
                        ),
                        asset_id=row.id,
                        extra={"git_path": row.git_path, "owner": row.owner},
                    )
                )
        return alerts

    # ------------------------------------------------------------------
    # 内部：召回命中率（recall_log 实时聚合）
    # ------------------------------------------------------------------

    def _compute_recall_hit_rates(self) -> list[ModuleRecallHitRate]:
        """按模块聚合召回命中率。

        - recall_count = recall_log 近 30 天该模块总召回次数
        - read_count = recall_log 近 30 天 query='' 的次数（read 事件）
        - hit_rate = read_count / recall_count
        - asset_count = 该模块 active 资产数
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=RECALL_WINDOW_DAYS)
        with self._db.session() as sess:
            # 按模块聚合 recall_log
            stmt = (
                select(
                    RecallLog.module_path,
                    func.count(RecallLog.id).label("recall_count"),
                    func.sum(
                        case((RecallLog.query == "", 1), else_=0)
                    ).label("read_count"),
                )
                .where(RecallLog.recalled_at >= cutoff)
                .group_by(RecallLog.module_path)
            )
            recall_map: dict[str, tuple[int, int]] = {}
            for module_path, recall_count, read_count in sess.execute(stmt):
                recall_map[module_path or ""] = (
                    int(recall_count or 0),
                    int(read_count or 0),
                )

            # 按模块统计 active 资产数
            asset_stmt = (
                select(
                    AssetIndexRow.module_path,
                    func.count(AssetIndexRow.id),
                )
                .where(AssetIndexRow.status == "active")
                .group_by(AssetIndexRow.module_path)
            )
            asset_map: dict[str, int] = {
                str(mp or ""): int(c)
                for mp, c in sess.execute(asset_stmt)
            }

        results: list[ModuleRecallHitRate] = []
        all_modules = set(recall_map.keys()) | set(asset_map.keys())
        for module_path in sorted(all_modules):
            recall_count, read_count = recall_map.get(module_path, (0, 0))
            asset_count = asset_map.get(module_path, 0)
            hit_rate = (read_count / recall_count) if recall_count > 0 else 0.0
            avg_recall = (recall_count / asset_count) if asset_count > 0 else 0.0
            results.append(
                ModuleRecallHitRate(
                    module_path=module_path,
                    recall_count=recall_count,
                    read_count=read_count,
                    asset_count=asset_count,
                    hit_rate=hit_rate,
                    avg_recall_per_asset=avg_recall,
                )
            )
        return results

    # ------------------------------------------------------------------
    # 内部：采纳率（委托 AdoptionMetricsService）
    # ------------------------------------------------------------------

    def _collect_adoption_rates(self) -> list:
        """采集采纳率（服务端可采，recall_log 主信号）。"""
        return self._adoption.collect_metrics()

    # ------------------------------------------------------------------
    # 内部：仓库大小告警
    # ------------------------------------------------------------------

    def _check_repo_size(self) -> list:
        """仓库大小告警（委托 ArchiveLifecycleService 或直接计算）。"""
        # 延迟 import 避免循环
        from server.governance.archive_lifecycle import ArchiveLifecycleService

        svc = ArchiveLifecycleService(self._db, archive_root=self._archive._archive_root)
        return svc.check_repo_size()


__all__ = [
    "RECALL_WINDOW_DAYS",
    "DashboardService",
]
