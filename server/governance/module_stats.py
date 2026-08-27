"""ModuleStatsService — module_stats 实时派生服务（缺陷 8.1 修复核心）。

对应 SubTask 9.11 + 9.4：
- module_stats 必须从 asset_index 实时派生（SQL 查询时聚合），不得依赖人维护 counts
- 拆分判定基于 asset_index 实时查询，非 module_stats.declared_* 字段

设计原则（红线遵守）：
- `compute_module_stats(module_path)` 全部字段从 asset_index 实时聚合派生
- `compute_all_modules()` 列出全部模块并实时派生
- declared_* 字段从 module_stats 表读取（INDEX.md 镜像），仅用于 counts 一致性校验
- 治理看板与拆分判定只读 actual_* 实时派生字段，不读 declared_*
- ModuleStats ORM 表保留作为 INDEX.md 声明值镜像（兼容历史数据）

拆分判定阈值（基于技术方案 8.13）：
- 项目内模块数 > 5 → 建议按业务模块独立成层
- 单模块资产数 > 20 → 建议该模块拆分子模块
- 模块边界清晰可独立 → 软信号（需判断）
"""

from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import func, select

from server.governance.models import (
    ModuleStatsSnapshot,
    SplitSuggestion,
)
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow, ModuleStats

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 拆分判定阈值（基于技术方案 8.13）
# ---------------------------------------------------------------------------


# 项目内模块数 > 5 → 建议按业务模块独立成层
MODULE_COUNT_THRESHOLD = 5
# 单模块资产数 > 20 → 建议该模块拆分子模块
ASSET_COUNT_THRESHOLD = 20


class ModuleStatsService:
    """module_stats 实时派生服务（缺陷 8.1 修复核心）。

    用法：
        svc = ModuleStatsService(database)
        # 单模块实时派生
        stats = svc.compute_module_stats("modules/backend")
        # 全部模块实时派生
        all_stats = svc.compute_all_modules()
        # 拆分判定（基于实时派生）
        suggestions = svc.detect_split_suggestions()
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # 实时派生（核心 API）
    # ------------------------------------------------------------------

    def compute_module_stats(self, module_path: str) -> ModuleStatsSnapshot:
        """实时派生单模块的统计（缺陷 8.1 修复核心）。

        全部 actual_* 字段从 asset_index 实时聚合，不依赖 module_stats 表的 declared_*。
        declared_* 字段从 module_stats 表读取（INDEX.md 镜像），用于一致性校验。
        """
        with self._db.session() as sess:
            actual_assets = self._count_actual_assets(sess, module_path)
            actual_submodules = self._count_actual_submodules(sess, module_path)
            by_type = self._count_by_type(sess, module_path)
            by_status = self._count_by_status(sess, module_path)
            # 读取 INDEX.md 声明值镜像（可能为 None 表示无声明）
            declared_row = sess.get(ModuleStats, module_path)

        if declared_row is not None:
            declared_assets: int | None = declared_row.declared_asset_count
            declared_subs: int | None = declared_row.declared_submodule_count
            last_synced_at = declared_row.last_synced_at
            last_synced_commit = declared_row.last_synced_commit
            counts_consistent = (
                declared_assets == actual_assets and declared_subs == actual_submodules
            )
        else:
            declared_assets = None
            declared_subs = None
            last_synced_at = None
            last_synced_commit = ""
            # 无声明值 → 视为一致（不强制要求 INDEX.md 维护 counts）
            counts_consistent = True

        return ModuleStatsSnapshot(
            module_path=module_path,
            actual_asset_count=actual_assets,
            actual_submodule_count=actual_submodules,
            by_type=by_type,
            by_status=by_status,
            declared_asset_count=declared_assets,
            declared_submodule_count=declared_subs,
            counts_consistent=counts_consistent,
            last_synced_at=last_synced_at,
            last_synced_commit=last_synced_commit,
        )

    def compute_all_modules(self) -> list[ModuleStatsSnapshot]:
        """列出全部模块并实时派生统计。

        - 不传 module_path 时返回所有有资产的模块（从 asset_index.distinct module_path）
        - 同时纳入 module_stats 表中声明但 asset_index 中无资产的模块（孤儿声明）
        """
        with self._db.session() as sess:
            # 1. asset_index 中所有非空 module_path（去重）
            stmt_active = (
                select(AssetIndexRow.module_path)
                .where(AssetIndexRow.module_path != "")
                .distinct()
            )
            modules_in_index: set[str] = set(sess.scalars(stmt_active))
            # 2. module_stats 表中已声明的模块（可能 asset_index 已无资产）
            stmt_declared = select(ModuleStats.module_path)
            modules_declared: set[str] = set(sess.scalars(stmt_declared))

        all_modules = modules_in_index | modules_declared
        return [self.compute_module_stats(m) for m in sorted(all_modules)]

    # ------------------------------------------------------------------
    # 拆分判定（基于实时派生 counts，非人维护 counts）
    # ------------------------------------------------------------------

    def detect_split_suggestions(self) -> list[SplitSuggestion]:
        """基于 asset_index 实时查询的拆分判定。

        对应技术方案 8.13 拆分信号：
        - 项目内模块数 > 5 → 建议按业务模块独立成层
        - 单模块资产数 > 20 → 建议该模块拆分子模块
        - 软信号（模块边界清晰）暂不自动判定
        """
        suggestions: list[SplitSuggestion] = []

        # 1. 项目内模块数 > 5
        all_stats = self.compute_all_modules()
        # 顶层模块数（取 module_path 第一段，如 modules/backend → backend）
        top_level_modules: set[str] = set()
        for s in all_stats:
            if not s.module_path:
                continue
            parts = s.module_path.split("/")
            # 取第二段（modules/backend → backend；顶层 backend → backend）
            if len(parts) >= 2 and parts[0] == "modules":
                top_level_modules.add(parts[1])
            elif len(parts) >= 1 and parts[0]:
                top_level_modules.add(parts[0])

        if len(top_level_modules) > MODULE_COUNT_THRESHOLD:
            suggestions.append(
                SplitSuggestion(
                    module_path="(project)",
                    signal="module_count_too_many",
                    threshold=MODULE_COUNT_THRESHOLD,
                    actual=len(top_level_modules),
                    suggestion=(
                        f"项目内模块数 {len(top_level_modules)} > {MODULE_COUNT_THRESHOLD}，"
                        "建议按业务模块独立成层（modules/ 下建子目录 + INDEX.md）"
                    ),
                    severity="warning",
                )
            )

        # 2. 单模块资产数 > 20
        for s in all_stats:
            if s.actual_asset_count > ASSET_COUNT_THRESHOLD:
                suggestions.append(
                    SplitSuggestion(
                        module_path=s.module_path,
                        signal="asset_count_too_many",
                        threshold=ASSET_COUNT_THRESHOLD,
                        actual=s.actual_asset_count,
                        suggestion=(
                            f"模块 {s.module_path} 资产数 {s.actual_asset_count} "
                            f"> {ASSET_COUNT_THRESHOLD}，建议拆分子模块"
                        ),
                        severity="warning",
                    )
                )

        return suggestions

    # ------------------------------------------------------------------
    # 内部：实时聚合查询（不读 module_stats.declared_*）
    # ------------------------------------------------------------------

    def _count_actual_assets(self, sess, module_path: str) -> int:
        """统计 asset_index 表中该 module_path 下 active 资产数。

        精确匹配（不递归子模块，避免与 submodule_count 双重计数）。
        """
        stmt = (
            select(func.count(AssetIndexRow.id))
            .where(AssetIndexRow.module_path == module_path)
            .where(AssetIndexRow.status == "active")
        )
        return int(sess.scalar(stmt) or 0)

    def _count_actual_submodules(self, sess, module_path: str) -> int:
        """统计该 module_path 下的直接子模块数。

        按 module_path 前缀匹配：直接子层算一个。
        例如 module_path="modules/backend"，子模块为：
        - modules/backend/auth
        - modules/backend/db
        但 modules/backend/auth/sub 不算（孙层）
        """
        prefix = module_path + "/" if module_path else ""
        stmt = (
            select(AssetIndexRow.module_path)
            .where(AssetIndexRow.status == "active")
            .where(AssetIndexRow.module_path != module_path)
            .distinct()
        )
        all_paths: set[str] = set(sess.scalars(stmt))
        children: set[str] = set()
        for p in all_paths:
            if not p:
                continue
            if module_path:
                if not p.startswith(prefix):
                    continue
                rest = p[len(prefix):]
                # 只取第一段为子模块名（直接子层算一个，孙层不算）
                # rest.split("/", 1) 必返回至少 1 元素，直接取 parts[0]
                parts = rest.split("/", 1)
                if parts[0]:
                    # 直接子层（如 "auth"）或孙层第一段（如 "auth/sub" → "auth"）均算直接子模块
                    children.add(parts[0])
            else:
                # 顶层：取 module_path 第一段
                parts = p.split("/", 1)
                if parts[0]:
                    children.add(parts[0])
        return len(children)

    def _count_by_type(self, sess, module_path: str) -> dict[str, int]:
        """按 type 分布（active 资产）。"""
        stmt = (
            select(AssetIndexRow.type, func.count(AssetIndexRow.id))
            .where(AssetIndexRow.module_path == module_path)
            .where(AssetIndexRow.status == "active")
            .group_by(AssetIndexRow.type)
        )
        return {str(t): int(c) for t, c in sess.execute(stmt)}

    def _count_by_status(self, sess, module_path: str) -> dict[str, int]:
        """按 status 分布（active/deleted/superseded）。"""
        stmt = (
            select(AssetIndexRow.status, func.count(AssetIndexRow.id))
            .where(AssetIndexRow.module_path == module_path)
            .group_by(AssetIndexRow.status)
        )
        return {str(s): int(c) for s, c in sess.execute(stmt)}


__all__ = [
    "ASSET_COUNT_THRESHOLD",
    "MODULE_COUNT_THRESHOLD",
    "ModuleStatsService",
]
