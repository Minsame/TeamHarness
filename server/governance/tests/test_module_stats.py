"""SubTask 9.4+9.11+9.13: module_stats 实时派生 + 拆分判定测试。

覆盖：
- actual_asset_count 从 asset_index 实时聚合（缺陷 8.1 修复核心）
- actual_submodule_count 实时聚合
- by_type / by_status 分布
- declared_* 从 module_stats 表读取（镜像）
- has_mismatch 判定
- 拆分判定：module_count > 5 → 建议拆分
- 拆分判定：single module asset_count > 20 → 建议拆分子模块
- compute_all_modules 取 asset_index + module_stats 并集
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from server.governance.module_stats import (
    ASSET_COUNT_THRESHOLD,
    MODULE_COUNT_THRESHOLD,
    ModuleStatsService,
)


class TestComputeModuleStats:
    """单模块实时派生。"""

    def test_actual_asset_count_from_asset_index(
        self, database, asset_index, upsert_helper
    ):
        """actual_asset_count 从 asset_index 实时聚合。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend", type="rule")
        upsert_helper(asset_index, id="r2", module_path="modules/backend", type="rule")
        upsert_helper(asset_index, id="m1", module_path="modules/backend", type="memory")

        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        assert stats.actual_asset_count == 3
        assert stats.module_path == "modules/backend"

    def test_actual_submodule_count(
        self, database, asset_index, upsert_helper
    ):
        """actual_submodule_count 实时聚合直接子模块。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend/auth")
        upsert_helper(asset_index, id="r2", module_path="modules/backend/db")
        upsert_helper(asset_index, id="r3", module_path="modules/backend/auth/sub")
        upsert_helper(asset_index, id="r4", module_path="modules/backend")

        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        # 直接子模块：auth + db（auth/sub 是孙层，不算）
        assert stats.actual_submodule_count == 2

    def test_by_type_distribution(
        self, database, asset_index, upsert_helper
    ):
        """by_type 按资产类型分布。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend", type="rule")
        upsert_helper(asset_index, id="r2", module_path="modules/backend", type="rule")
        upsert_helper(asset_index, id="m1", module_path="modules/backend", type="memory")

        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        assert stats.by_type == {"rule": 2, "memory": 1}

    def test_by_status_distribution(
        self, database, asset_index, upsert_helper
    ):
        """by_status 按状态分布。"""
        from server.infra_db.models import AssetIndex as AssetIndexRow
        from sqlalchemy import update

        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        upsert_helper(asset_index, id="r2", module_path="modules/backend")
        # 将 r2 标记为 deleted
        with database.session() as sess:
            sess.execute(
                update(AssetIndexRow)
                .where(AssetIndexRow.id == "r2")
                .values(status="deleted")
            )

        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        # actual_asset_count 仅 active
        assert stats.actual_asset_count == 1
        # by_status 含 active + deleted
        assert stats.by_status.get("active") == 1
        assert stats.by_status.get("deleted") == 1

    def test_declared_from_module_stats_table(
        self, database, asset_index, upsert_helper
    ):
        """declared_* 从 module_stats 表读取（镜像）。"""
        from server.infra_db.models import ModuleStats

        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        with database.session() as sess:
            sess.add(
                ModuleStats(
                    module_path="modules/backend",
                    declared_asset_count=5,
                    declared_submodule_count=2,
                    actual_asset_count=1,
                    actual_submodule_count=0,
                    counts_consistent=False,
                    last_synced_at=datetime.now(timezone.utc),
                    last_synced_commit="abc",
                )
            )

        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        assert stats.declared_asset_count == 5
        assert stats.declared_submodule_count == 2
        assert stats.actual_asset_count == 1
        assert stats.has_mismatch is True
        assert stats.counts_consistent is False

    def test_no_declared_consistent(
        self, database, asset_index, upsert_helper
    ):
        """无 module_stats 声明 → counts_consistent=True（不强制要求声明）。"""
        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        svc = ModuleStatsService(database)
        stats = svc.compute_module_stats("modules/backend")
        assert stats.declared_asset_count is None
        assert stats.declared_submodule_count is None
        assert stats.counts_consistent is True
        assert stats.has_mismatch is False


class TestComputeAllModules:
    """全部模块实时派生。"""

    def test_union_of_index_and_declared(
        self, database, asset_index, upsert_helper
    ):
        """compute_all_modules = asset_index 模块 ∪ module_stats 声明模块。"""
        from server.infra_db.models import ModuleStats

        upsert_helper(asset_index, id="r1", module_path="modules/backend")
        upsert_helper(asset_index, id="r2", module_path="modules/frontend")
        # module_stats 中有声明但 asset_index 无资产（孤儿声明）
        with database.session() as sess:
            sess.add(
                ModuleStats(
                    module_path="modules/orphan",
                    declared_asset_count=3,
                    declared_submodule_count=0,
                    actual_asset_count=0,
                    actual_submodule_count=0,
                )
            )

        svc = ModuleStatsService(database)
        all_stats = svc.compute_all_modules()
        module_paths = {s.module_path for s in all_stats}
        assert "modules/backend" in module_paths
        assert "modules/frontend" in module_paths
        assert "modules/orphan" in module_paths

    def test_empty_db_returns_empty(
        self, database
    ):
        """空库 → 空列表。"""
        svc = ModuleStatsService(database)
        assert svc.compute_all_modules() == []


class TestSplitSuggestions:
    """拆分判定（基于实时派生 counts）。"""

    def test_module_count_too_many(
        self, database, asset_index, upsert_helper
    ):
        """顶层模块数 > 5 → 建议按业务模块独立成层。"""
        # 创建 6 个顶层模块
        for i in range(6):
            upsert_helper(
                asset_index,
                id=f"r{i}",
                module_path=f"modules/mod{i}",
            )

        svc = ModuleStatsService(database)
        suggestions = svc.detect_split_suggestions()
        module_count_signals = [
            s for s in suggestions if s.signal == "module_count_too_many"
        ]
        assert len(module_count_signals) == 1
        assert module_count_signals[0].actual == 6
        assert module_count_signals[0].threshold == MODULE_COUNT_THRESHOLD

    def test_module_count_within_threshold(
        self, database, asset_index, upsert_helper
    ):
        """顶层模块数 ≤ 5 → 无拆分建议。"""
        for i in range(3):
            upsert_helper(
                asset_index, id=f"r{i}", module_path=f"modules/mod{i}"
            )

        svc = ModuleStatsService(database)
        suggestions = svc.detect_split_suggestions()
        module_count_signals = [
            s for s in suggestions if s.signal == "module_count_too_many"
        ]
        assert len(module_count_signals) == 0

    def test_single_module_asset_count_too_many(
        self, database, asset_index, upsert_helper
    ):
        """单模块资产数 > 20 → 建议拆分子模块。"""
        # 创建 21 个资产在同一模块
        for i in range(21):
            upsert_helper(
                asset_index,
                id=f"r{i}",
                module_path="modules/big",
            )

        svc = ModuleStatsService(database)
        suggestions = svc.detect_split_suggestions()
        asset_count_signals = [
            s for s in suggestions if s.signal == "asset_count_too_many"
        ]
        assert len(asset_count_signals) == 1
        assert asset_count_signals[0].module_path == "modules/big"
        assert asset_count_signals[0].actual == 21
        assert asset_count_signals[0].threshold == ASSET_COUNT_THRESHOLD

    def test_no_split_when_within_threshold(
        self, database, asset_index, upsert_helper
    ):
        """资产数 ≤ 20 → 无拆分建议。"""
        for i in range(10):
            upsert_helper(
                asset_index, id=f"r{i}", module_path="modules/normal"
            )

        svc = ModuleStatsService(database)
        suggestions = svc.detect_split_suggestions()
        assert len(suggestions) == 0
