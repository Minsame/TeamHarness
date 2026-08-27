"""SubTask 8.7：is_convention=true 单成员旁路测试。"""

from __future__ import annotations

import pytest

from server.distill_team.convention import (
    ConventionBypass,
    is_convention_asset,
    parse_frontmatter,
)
from server.distill_team.models import Cluster

from .conftest import upsert_asset


class TestParseFrontmatter:
    """frontmatter 解析测试。"""

    def test_parse_empty_content(self):
        assert parse_frontmatter("") == {}

    def test_parse_no_frontmatter(self):
        assert parse_frontmatter("# title\nbody") == {}

    def test_parse_valid_frontmatter(self):
        content = "---\nid: a1\nis_convention: true\n---\n# body"
        fm = parse_frontmatter(content)
        assert fm.get("id") == "a1"
        assert fm.get("is_convention") is True

    def test_parse_invalid_yaml_returns_empty(self):
        content = "---\n!!!invalid yaml::\n---\n# body"
        fm = parse_frontmatter(content)
        assert fm == {}


class TestIsConventionAsset:
    """is_convention 资产识别。"""

    def test_is_convention_true(self, database, asset_index):
        upsert_asset(
            asset_index, id="a1", owner="alice",
            content="# 约定\n变量用 snake_case",
            is_convention=True,
        )
        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            row = sess.get(AssetIndexRow, "a1")
            assert is_convention_asset(row) is True

    def test_is_convention_false_when_missing(self, database, asset_index):
        upsert_asset(
            asset_index, id="a2", owner="alice",
            content="# 普通规则",
        )
        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            row = sess.get(AssetIndexRow, "a2")
            assert is_convention_asset(row) is False

    def test_is_convention_false_when_string_false(self, database, asset_index):
        """is_convention: false（字符串）→ False。"""
        # 通过 upsert_asset 写入后再手工修改 content
        upsert_asset(
            asset_index, id="a3", owner="alice",
            content="# 约定",
            is_convention=True,
        )
        # 改写为 false
        from server.common.models import Asset as AssetVO, AssetType, Scope
        asset = AssetVO(
            id="a3", type=AssetType.RULE, owner="alice", scope=Scope.TEAM,
            content_file_ref="rules/a3.md",
            content="---\nid: a3\nis_convention: false\n---\n# 约定",
        )
        asset_index.upsert(
            asset, git_commit="c1",
            content_snapshot="---\nid: a3\nis_convention: false\n---\n# 约定",
        )
        from server.infra_db.models import AssetIndex as AssetIndexRow
        with database.session() as sess:
            row = sess.get(AssetIndexRow, "a3")
            assert is_convention_asset(row) is False


class TestConventionBypass:
    """convention 旁路测试。"""

    def test_collect_convention_assets_empty(
        self, database, asset_index
    ):
        bypass = ConventionBypass(database, asset_index)
        assert bypass.collect_convention_assets() == []

    def test_collect_convention_assets_filters(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice", is_convention=True,
                     content="# 约定 A")
        upsert_asset(asset_index, id="a2", owner="bob",
                     content="# 普通规则")
        upsert_asset(asset_index, id="a3", owner="carol", is_convention=True,
                     content="# 约定 B")

        bypass = ConventionBypass(database, asset_index)
        rows = bypass.collect_convention_assets()
        ids = {r.id for r in rows}
        assert ids == {"a1", "a3"}

    def test_collect_convention_clusters_group_by_category(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice", category="rule-conv",
                     is_convention=True, content="# 约定 A")
        upsert_asset(asset_index, id="a2", owner="bob", category="rule-conv",
                     is_convention=True, content="# 约定 A")
        upsert_asset(asset_index, id="a3", owner="carol", category="rule-other",
                     is_convention=True, content="# 约定 B")

        bypass = ConventionBypass(database, asset_index)
        clusters = bypass.collect_convention_clusters()

        # 按 category 分组：rule-conv 一簇（a1, a2），rule-other 一簇（a3）
        assert len(clusters) == 2
        conv_cluster = next(c for c in clusters if c.category == "rule-conv")
        assert set(conv_cluster.asset_ids) == {"a1", "a2"}
        assert conv_cluster.is_convention is True

    def test_convention_cluster_no_category_individual(
        self, database, asset_index
    ):
        """无 category 的 convention 资产各自成簇（但 min_cluster_size 在 Deep 阶段过滤）。"""
        upsert_asset(asset_index, id="a1", owner="alice",
                     is_convention=True, content="# 约定 A")
        upsert_asset(asset_index, id="a2", owner="bob",
                     is_convention=True, content="# 约定 B")

        bypass = ConventionBypass(database, asset_index)
        clusters = bypass.collect_convention_clusters()
        # 无 category → 各自一组
        assert len(clusters) == 2
        for c in clusters:
            assert c.is_convention is True
            assert len(c.asset_ids) == 1
