"""INDEX.md counts 服务端校验（SubTask 2.10）。

对应技术方案 3.1.4 counts 维护原则 + 缺陷 8.1 counts 校验：
- webhook 同步时从 git INDEX.md 读取 counts 写入 module_stats 镜像表
- 实际资产数从 asset_index 实时派生（治理看板数据源）
- counts 不一致 → 告警，不阻断同步
- 治理看板（Agent 9）从 module_stats 读取 declared 与 actual 差异

校验语义：
- declared_asset_count：INDEX.md 中 counts.assets 的声明值
- actual_asset_count：asset_index 表中该 module_path 下 status=active 的资产数
- 不一致 → counts_consistent=False，记 warning 日志，不阻断
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Iterable

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow, ModuleStats

logger = logging.getLogger(__name__)


@dataclass
class CountsMismatch:
    """counts 不一致记录。"""

    module_path: str
    field_name: str  # assets / submodules
    declared: int
    actual: int

    @property
    def diff(self) -> int:
        return self.actual - self.declared


@dataclass
class CountsCheckResult:
    """counts 校验结果（不阻断同步）。"""

    mismatches: list[CountsMismatch] = field(default_factory=list)
    checked: int = 0

    @property
    def ok(self) -> bool:
        return not self.mismatches


class CountsChecker:
    """INDEX.md counts 服务端校验器。

    用法：
        checker = CountsChecker(db)
        result = checker.check_and_persist(declared_counts_per_module)
        if not result.ok:
            logger.warning("counts 不一致：%s", result.mismatches)
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def check_and_persist(
        self,
        declared: dict[str, dict[str, int]],
        *,
        commit_sha: str = "",
    ) -> CountsCheckResult:
        """校验 declared 与 actual，写入 module_stats 表。

        declared: {module_path: {"assets": int, "submodules": int}}
        返回不一致清单（不阻断同步）。
        """
        result = CountsCheckResult()
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            for module_path, counts in declared.items():
                declared_assets = int(counts.get("assets", 0))
                declared_subs = int(counts.get("submodules", 0))
                actual_assets = self._count_actual_assets(sess, module_path)
                actual_subs = self._count_actual_submodules(sess, module_path)
                consistent = (
                    declared_assets == actual_assets and declared_subs == actual_subs
                )
                # upsert module_stats
                row = sess.get(ModuleStats, module_path)
                if row is None:
                    sess.add(
                        ModuleStats(
                            module_path=module_path,
                            declared_asset_count=declared_assets,
                            declared_submodule_count=declared_subs,
                            actual_asset_count=actual_assets,
                            actual_submodule_count=actual_subs,
                            counts_consistent=consistent,
                            last_synced_at=now,
                            last_synced_commit=commit_sha,
                        )
                    )
                else:
                    row.declared_asset_count = declared_assets
                    row.declared_submodule_count = declared_subs
                    row.actual_asset_count = actual_assets
                    row.actual_submodule_count = actual_subs
                    row.counts_consistent = consistent
                    row.last_synced_at = now
                    row.last_synced_commit = commit_sha

                result.checked += 1
                if declared_assets != actual_assets:
                    result.mismatches.append(
                        CountsMismatch(module_path, "assets", declared_assets, actual_assets)
                    )
                    logger.warning(
                        "counts 不一致 module=%s assets declared=%d actual=%d（不阻断）",
                        module_path,
                        declared_assets,
                        actual_assets,
                    )
                if declared_subs != actual_subs:
                    result.mismatches.append(
                        CountsMismatch(module_path, "submodules", declared_subs, actual_subs)
                    )
                    logger.warning(
                        "counts 不一致 module=%s submodules declared=%d actual=%d（不阻断）",
                        module_path,
                        declared_subs,
                        actual_subs,
                    )
        return result

    def _count_actual_assets(self, sess: Session, module_path: str) -> int:
        """统计 asset_index 表中该 module_path 下 active 资产数。"""
        stmt = (
            select(AssetIndexRow.id)
            .where(AssetIndexRow.module_path == module_path)
            .where(AssetIndexRow.status == "active")
        )
        return len(list(sess.scalars(stmt)))

    def _count_actual_submodules(self, sess: Session, module_path: str) -> int:
        """统计该 module_path 下的子模块数（按 module_path 前缀匹配）。"""
        prefix = module_path + "/" if module_path else ""
        stmt = (
            select(AssetIndexRow.module_path)
            .where(AssetIndexRow.status == "active")
            .distinct()
        )
        all_paths = set(sess.scalars(stmt))
        # 算 module_path 下直接的子层
        children: set[str] = set()
        for p in all_paths:
            if not p or p == module_path:
                continue
            if module_path and not p.startswith(prefix):
                continue
            # 去掉前缀后取第一段
            rest = p[len(prefix):] if module_path else p
            parts = rest.split("/")
            if len(parts) > 1:
                children.add(parts[0])
        return len(children)

    def list_mismatches(self) -> list[CountsMismatch]:
        """列出当前 module_stats 中所有 counts 不一致的模块（治理看板数据源）。"""
        result: list[CountsMismatch] = []
        with self._db.session() as sess:
            stmt = select(ModuleStats).where(ModuleStats.counts_consistent.is_(False))
            for row in sess.scalars(stmt):
                if row.declared_asset_count != row.actual_asset_count:
                    result.append(
                        CountsMismatch(
                            row.module_path,
                            "assets",
                            row.declared_asset_count,
                            row.actual_asset_count,
                        )
                    )
                if row.declared_submodule_count != row.actual_submodule_count:
                    result.append(
                        CountsMismatch(
                            row.module_path,
                            "submodules",
                            row.declared_submodule_count,
                            row.actual_submodule_count,
                        )
                    )
        return result


__all__ = [
    "CountsCheckResult",
    "CountsChecker",
    "CountsMismatch",
]
