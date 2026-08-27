"""SubTask 2.10：INDEX.md counts 服务端校验（单独测试 CountsChecker）。"""

from __future__ import annotations

import pytest

from server.infra_db.counts_check import CountsChecker
from server.infra_db.models import ModuleStats


def test_counts_check_consistent(database, counts_checker):
    """声明与实际一致 → consistent=True。"""
    from server.infra_db.tests.conftest import make_asset

    # 通过 AssetIndex 写入 2 个资产（module_path=""）
    from server.infra_db.asset_index import AssetIndex

    ai = AssetIndex(database, active_embedding_version="v1", shadow_embedding_version="")
    ai.upsert(make_asset(id="c1", module_path=""), git_commit="c1")
    ai.upsert(make_asset(id="c2", module_path=""), git_commit="c1")

    result = counts_checker.check_and_persist(
        {"": {"assets": 2, "submodules": 0}}, commit_sha="c1"
    )
    assert result.ok
    assert len(result.mismatches) == 0

    with database.session() as sess:
        row = sess.get(ModuleStats, "")
        assert row.declared_asset_count == 2
        assert row.actual_asset_count == 2
        assert row.counts_consistent is True


def test_counts_check_mismatch_assets(database, counts_checker):
    """声明 5 实际 2 → 不一致但记录写入 module_stats。"""
    from server.infra_db.asset_index import AssetIndex
    from server.infra_db.tests.conftest import make_asset

    ai = AssetIndex(database, active_embedding_version="v1", shadow_embedding_version="")
    ai.upsert(make_asset(id="c1", module_path=""), git_commit="c1")
    ai.upsert(make_asset(id="c2", module_path=""), git_commit="c1")

    result = counts_checker.check_and_persist(
        {"": {"assets": 5, "submodules": 0}}, commit_sha="c1"
    )
    assert not result.ok
    assert len(result.mismatches) == 1
    mismatch = result.mismatches[0]
    assert mismatch.field_name == "assets"
    assert mismatch.declared == 5
    assert mismatch.actual == 2
    assert mismatch.diff == -3

    with database.session() as sess:
        row = sess.get(ModuleStats, "")
        assert row.counts_consistent is False


def test_counts_check_persists_module_path(database, counts_checker):
    """按 module_path 分别记录 counts。"""
    from server.infra_db.asset_index import AssetIndex
    from server.infra_db.tests.conftest import make_asset

    ai = AssetIndex(database, active_embedding_version="v1", shadow_embedding_version="")
    ai.upsert(make_asset(id="m1", module_path="modules/backend"), git_commit="c1")
    ai.upsert(make_asset(id="m2", module_path="modules/backend"), git_commit="c1")
    ai.upsert(make_asset(id="m3", module_path="modules/frontend"), git_commit="c1")

    counts_checker.check_and_persist({
        "modules/backend": {"assets": 2, "submodules": 0},
        "modules/frontend": {"assets": 1, "submodules": 0},
    }, commit_sha="c1")

    with database.session() as sess:
        be = sess.get(ModuleStats, "modules/backend")
        fe = sess.get(ModuleStats, "modules/frontend")
        assert be.declared_asset_count == 2
        assert be.actual_asset_count == 2
        assert be.counts_consistent is True
        assert fe.declared_asset_count == 1
        assert fe.actual_asset_count == 1
        assert fe.counts_consistent is True


def test_counts_check_upsert_existing_module(database, counts_checker):
    """重复校验：已有 module_stats 行应更新而非报错。"""
    counts_checker.check_and_persist(
        {"modules/x": {"assets": 1, "submodules": 0}}, commit_sha="c1"
    )
    # 第二次：声明变化
    counts_checker.check_and_persist(
        {"modules/x": {"assets": 5, "submodules": 0}}, commit_sha="c2"
    )
    with database.session() as sess:
        row = sess.get(ModuleStats, "modules/x")
        assert row.declared_asset_count == 5
        assert row.last_synced_commit == "c2"


def test_list_mismatches_returns_only_inconsistent(database, counts_checker):
    """list_mismatches 只返回 counts_consistent=False 的模块。"""
    from server.infra_db.asset_index import AssetIndex
    from server.infra_db.tests.conftest import make_asset

    ai = AssetIndex(database, active_embedding_version="v1", shadow_embedding_version="")
    ai.upsert(make_asset(id="m1", module_path="ok"), git_commit="c1")
    ai.upsert(make_asset(id="m2", module_path="bad"), git_commit="c1")
    ai.upsert(make_asset(id="m3", module_path="bad"), git_commit="c1")

    counts_checker.check_and_persist(
        {"ok": {"assets": 1, "submodules": 0}}, commit_sha="c1"
    )
    counts_checker.check_and_persist(
        {"bad": {"assets": 99, "submodules": 0}}, commit_sha="c1"
    )
    mismatches = counts_checker.list_mismatches()
    assert len(mismatches) == 1
    assert mismatches[0].module_path == "bad"
    assert mismatches[0].declared == 99
    assert mismatches[0].actual == 2


def test_counts_check_deleted_assets_excluded(database, counts_checker, asset_index):
    """soft delete 的资产不计入 actual_asset_count。"""
    from server.infra_db.tests.conftest import make_asset

    asset_index.upsert(make_asset(id="d1", module_path=""), git_commit="c1")
    asset_index.upsert(make_asset(id="d2", module_path=""), git_commit="c1")
    asset_index.delete("d2", git_commit="c2")  # soft delete

    result = counts_checker.check_and_persist(
        {"": {"assets": 1, "submodules": 0}}, commit_sha="c2"
    )
    assert result.ok  # 声明 1 实际 1（d2 已 deleted 不计入）
