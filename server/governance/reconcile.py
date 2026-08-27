"""ReconcileService — teamharness index reconcile 命令（SubTask 9.12）。

对应技术方案 3.3.5 + 8.1 counts 派生修复：
- 比对 module_stats.declared_* 与 asset_index 实时聚合的 actual_*
- 不一致 → 触发 SyncService.reconcile 补同步（从 git 重建 module_stats 镜像）
- 返回 ReconcileResult（含不一致明细 + 是否触发补同步）

设计要点：
- module_stats 实时派生（缺陷 8.1 修复核心）：actual_* 从 asset_index 聚合
- declared_* 从 module_stats 表读取（INDEX.md 镜像）
- 不一致 → 委托 SyncService.reconcile 触发补同步
- 补同步后 module_stats.declared_* 更新（与 actual_* 重新对齐）
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from server.governance.module_stats import ModuleStatsService
from server.infra_db.db import Database

logger = logging.getLogger(__name__)


class ReconcileService:
    """teamharness index reconcile 命令服务。

    用法：
        svc = ReconcileService(database, sync_service=sync_svc)
        result = svc.reconcile()
        if result.modules_with_mismatch > 0:
            print(f"检测到 {result.modules_with_mismatch} 个模块 counts 不一致")
    """

    def __init__(
        self,
        database: Database,
        *,
        sync_service: Any | None = None,
        head_resolver: Any | None = None,
    ) -> None:
        self._db = database
        self._module_stats = ModuleStatsService(database)
        self._sync_service = sync_service
        self._head_resolver = head_resolver

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def reconcile(self) -> Any:
        """执行 reconcile：比对 declared vs actual counts，不一致则触发补同步。

        返回 ReconcileResult。
        """
        from server.governance.models import ReconcileResult

        start = time.monotonic()
        result = ReconcileResult()

        # 1. 解析当前 commit
        result.commit_sha = self._resolve_commit()

        # 2. 全部模块实时派生
        all_stats = self._module_stats.compute_all_modules()
        result.modules_checked = len(all_stats)

        # 3. 比对 declared vs actual
        mismatches: list[dict[str, Any]] = []
        for s in all_stats:
            if s.has_mismatch:
                mismatches.append(
                    {
                        "module_path": s.module_path,
                        "declared_asset_count": s.declared_asset_count,
                        "actual_asset_count": s.actual_asset_count,
                        "declared_submodule_count": s.declared_submodule_count,
                        "actual_submodule_count": s.actual_submodule_count,
                        "last_synced_commit": s.last_synced_commit,
                    }
                )

        result.mismatches = mismatches
        result.modules_with_mismatch = len(mismatches)

        # 4. 不一致 → 触发补同步
        if mismatches and self._sync_service is not None:
            try:
                result.db_resync_triggered = True
                resync_result = self._sync_service.reconcile()
                result.db_resync_result = str(resync_result)
                logger.info(
                    "reconcile 触发补同步 modules=%d result=%s",
                    len(mismatches), resync_result,
                )
            except Exception as exc:  # noqa: BLE001
                result.db_resync_result = f"补同步失败: {exc}"
                logger.warning("reconcile 补同步失败 err=%s", exc)

        result.elapsed_seconds = round(time.monotonic() - start, 3)
        return result

    def reconcile_and_fix(self) -> Any:
        """reconcile + 强制触发补同步（即使无 mismatch 也重建 module_stats）。

        用于手动修复场景。
        """
        result = self.reconcile()
        if not result.mismatches and self._sync_service is not None:
            try:
                result.db_resync_triggered = True
                resync_result = self._sync_service.reconcile()
                result.db_resync_result = str(resync_result)
            except Exception as exc:  # noqa: BLE001
                result.db_resync_result = f"强制补同步失败: {exc}"
        return result

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _resolve_commit(self) -> str:
        """解析当前 HEAD commit。"""
        # 1. head_resolver 优先
        if self._head_resolver is not None:
            try:
                return str(self._head_resolver())
            except Exception:  # noqa: BLE001
                pass
        # 2. 从 index_sync_state 取 last_synced_commit
        from sqlalchemy import select

        from server.infra_db.models import IndexSyncState

        try:
            with self._db.session() as sess:
                row = sess.get(IndexSyncState, "singleton")
                if row is not None:
                    return row.last_synced_commit
        except Exception:  # noqa: BLE001
            pass
        return ""


__all__ = ["ReconcileService"]
