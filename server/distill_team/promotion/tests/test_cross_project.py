"""跨项目再升维模块测试。

覆盖抽象模式识别 / 项目记忆扫描 / 模式升维 / 被动触发流程。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.archive import ArchiveManager
from server.distill_team.promotion.cross_project import (
    CrossProjectPattern,
    CrossProjectPromoter,
    find_abstract_pattern,
    scan_project_memories,
)
from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.models import ArchiveEntry


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_layout(tmp_path: Path) -> MemoryLayout:
    return MemoryLayout(
        project_rules_dir=tmp_path / "project_rules",
        global_rules_dir=tmp_path / "global_rules",
        user_profile_path=tmp_path / "profile.md",
        archive_path=tmp_path / "archive.md",
        graph_path=tmp_path / "graph.md",
        cross_project_root=tmp_path / "cross",
        rules_file_ext=".md",
        supports_frontmatter=True,
        hotspot_section_marker="## 热点规则",
    )


def _make_entry(
    entry_id: str = "E001",
    title: str = "T",
    original_case: str = "case",
    promoted_to: str = "R001",
) -> ArchiveEntry:
    return ArchiveEntry(
        entry_id=entry_id,
        title=title,
        promoted_to=promoted_to,
        promoted_at=datetime(2026, 8, 12),
        original_case=original_case,
        promotion_strategy="strategy",
        source_session="sess",
    )


def _write_project_archive(
    project_dir: Path, entries: list[tuple[str, str, str]]
) -> None:
    """写入项目 archive.md（含多条经验）。

    Args:
        project_dir: 项目目录
        entries: [(entry_id, title, original_case), ...]
    """
    project_dir.mkdir(parents=True, exist_ok=True)
    lines = ["# 归档区", "", "## 经验归档", ""]
    for entry_id, title, case in entries:
        lines.extend([
            f"### {entry_id}：{title}",
            f"- 升维至：R999",
            f"- 升维时间：2026-08-12",
            f"- 原始错误案例：{case}",
            f"- 升维策略：strategy",
            f"- 来源会话：sess",
            "",
        ])
    lines.append("## 触发失败案例")
    lines.append("")
    (project_dir / "archive.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# find_abstract_pattern
# ---------------------------------------------------------------------------


class TestFindAbstractPattern:
    """find_abstract_pattern 抽象模式识别。"""

    def test_find_pattern_with_common_keywords(self):
        """2 个经验含 >= 2 个共有关键词 → 识别为模式。"""
        entries = [
            _make_entry(
                entry_id="E001",
                title="Database session rule",
                original_case="Always close database session after use",
            ),
            _make_entry(
                entry_id="E002",
                title="Database connection rule",
                original_case="Always close database connection after use",
            ),
        ]
        pattern = find_abstract_pattern(entries)
        assert pattern is not None
        assert pattern.pattern_id.startswith("P")
        assert set(pattern.source_entries) == {"E001", "E002"}
        assert pattern.name  # 名称非空
        assert pattern.description  # 描述非空
        assert pattern.abstract_rule_content  # 升维规则内容非空

    def test_no_pattern_single_entry(self):
        """经验不足 2 个 → None。"""
        entries = [_make_entry(entry_id="E001", title="T", original_case="content")]
        pattern = find_abstract_pattern(entries)
        assert pattern is None

    def test_no_pattern_no_common_keywords(self):
        """2 个经验但无共有关键词 → None。"""
        entries = [
            _make_entry(
                entry_id="E001",
                title="Redis cache",
                original_case="Redis cache invalidation strategy",
            ),
            _make_entry(
                entry_id="E002",
                title="GraphQL schema",
                original_case="GraphQL schema design patterns",
            ),
        ]
        pattern = find_abstract_pattern(entries)
        assert pattern is None

    def test_pattern_id_stable(self):
        """相同经验组合生成相同 pattern_id（基于哈希）。"""
        entries = [
            _make_entry(entry_id="E001", title="T", original_case="database session close"),
            _make_entry(entry_id="E002", title="T", original_case="database connection close"),
        ]
        p1 = find_abstract_pattern(entries)
        p2 = find_abstract_pattern(entries)
        assert p1 is not None
        assert p2 is not None
        assert p1.pattern_id == p2.pattern_id

    def test_pattern_content_includes_source_entries(self):
        """升维规则内容包含来源经验 ID。"""
        entries = [
            _make_entry(entry_id="E001", title="DB Rule", original_case="close database session"),
            _make_entry(entry_id="E002", title="DB Rule", original_case="close database connection"),
        ]
        pattern = find_abstract_pattern(entries)
        assert pattern is not None
        assert "E001" in pattern.abstract_rule_content
        assert "E002" in pattern.abstract_rule_content

    def test_pattern_with_three_entries(self):
        """3 个经验含共有关键词 → 生成模式。"""
        entries = [
            _make_entry(entry_id="E001", title="T", original_case="database session close"),
            _make_entry(entry_id="E002", title="T", original_case="database connection close"),
            _make_entry(entry_id="E003", title="T", original_case="database pool close"),
        ]
        pattern = find_abstract_pattern(entries)
        assert pattern is not None
        assert len(pattern.source_entries) == 3


# ---------------------------------------------------------------------------
# scan_project_memories
# ---------------------------------------------------------------------------


class TestScanProjectMemories:
    """scan_project_memories 扫描项目记忆。"""

    def test_scan_empty_root(self, tmp_path: Path):
        """cross_project_root 不存在 → 空字典。"""
        result = scan_project_memories(tmp_path / "no-such-dir")
        assert result == {}

    def test_scan_no_project_dirs(self, tmp_path: Path):
        """cross_project_root 存在但无子目录 → 空字典。"""
        cross_root = tmp_path / "cross"
        cross_root.mkdir()
        result = scan_project_memories(cross_root)
        assert result == {}

    def test_scan_single_project_with_archive(self, tmp_path: Path):
        """扫描含 archive.md 的项目。"""
        cross_root = tmp_path / "cross"
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "T1", "database session close")],
        )
        result = scan_project_memories(cross_root)
        assert len(result) == 1
        # 键是项目路径字符串
        project_key = list(result.keys())[0]
        assert "proj1" in project_key
        # 该项目含 1 条经验
        entries = result[project_key]
        assert len(entries) == 1
        assert entries[0].entry_id == "E001"

    def test_scan_multiple_projects(self, tmp_path: Path):
        """扫描多个项目。"""
        cross_root = tmp_path / "cross"
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "T1", "database session close")],
        )
        _write_project_archive(
            cross_root / "proj2",
            [("E002", "T2", "database connection close")],
        )
        result = scan_project_memories(cross_root)
        assert len(result) == 2

    def test_scan_parses_entry_fields(self, tmp_path: Path):
        """扫描时正确解析经验字段。"""
        cross_root = tmp_path / "cross"
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "DB Rule", "close database session")],
        )
        result = scan_project_memories(cross_root)
        entries = list(result.values())[0]
        e = entries[0]
        assert e.entry_id == "E001"
        assert e.title == "DB Rule"
        assert "close database session" in e.original_case

    def test_scan_skips_non_directory_entries(self, tmp_path: Path):
        """cross_project_root 下的文件被跳过，只处理子目录。"""
        cross_root = tmp_path / "cross"
        cross_root.mkdir()
        (cross_root / "stray.md").write_text("# not a project", encoding="utf-8")
        result = scan_project_memories(cross_root)
        assert result == {}


# ---------------------------------------------------------------------------
# CrossProjectPromoter.promote_pattern
# ---------------------------------------------------------------------------


class TestPromotePattern:
    """promote_pattern 升维模式为全局规则。"""

    def test_promote_pattern_writes_rule(self, tmp_path: Path):
        """升维后全局规则文件被写入。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)

        pattern = CrossProjectPattern(
            pattern_id="P001",
            name="测试模式",
            description="测试描述",
            source_entries=["E001", "E002"],
            source_projects=["/proj1", "/proj2"],
            abstract_rule_content="# 测试规则\n通用规则内容",
        )

        promoter = CrossProjectPromoter(adapter, graph, archive)
        result = promoter.promote_pattern(pattern, layout)

        assert result.triggered is True
        assert len(result.new_global_rules) == 1
        rule_id = result.new_global_rules[0]
        # 全局规则文件应存在
        rule_file = layout.global_rules_dir / f"{rule_id}.md"
        assert rule_file.is_file()
        text = rule_file.read_text(encoding="utf-8")
        assert "通用规则内容" in text

    def test_promote_pattern_registers_graph_node(self, tmp_path: Path):
        """升维后在图谱中注册新规则节点。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)

        pattern = CrossProjectPattern(
            pattern_id="P001",
            name="测试模式",
            description="desc",
            source_entries=["E001", "E002"],
            source_projects=[],
            abstract_rule_content="rule content",
        )
        promoter = CrossProjectPromoter(adapter, graph, archive)
        result = promoter.promote_pattern(pattern, layout)

        rule_id = result.new_global_rules[0]
        node = graph.get_node(rule_id)
        assert node is not None
        assert node.node_type == "rule"
        assert node.name == "测试模式"

    def test_promote_pattern_adds_promoted_from_relations(self, tmp_path: Path):
        """升维后添加 PROMOTED_FROM 关系（新规则 → 来源经验）。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)

        pattern = CrossProjectPattern(
            pattern_id="P001",
            name="T",
            description="d",
            source_entries=["E001", "E002"],
            source_projects=[],
            abstract_rule_content="c",
        )
        promoter = CrossProjectPromoter(adapter, graph, archive)
        result = promoter.promote_pattern(pattern, layout)

        rule_id = result.new_global_rules[0]
        # 查询新规则的关联关系
        rels = graph.get_relations(rule_id)
        # 应有 2 条 PROMOTED_FROM 关系（→ E001, → E002）
        promoted_from_rels = [
            r for r in rels
            if r.relation_type == "promoted_from"
            or r.source_id == rule_id and r.target_id in ("E001", "E002")
        ]
        assert len(promoted_from_rels) >= 2

    def test_promote_pattern_adds_same_source_relations(self, tmp_path: Path):
        """升维后添加 SAME_SOURCE 关系（来源经验之间）。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)

        pattern = CrossProjectPattern(
            pattern_id="P001",
            name="T",
            description="d",
            source_entries=["E001", "E002"],
            source_projects=[],
            abstract_rule_content="c",
        )
        promoter = CrossProjectPromoter(adapter, graph, archive)
        promoter.promote_pattern(pattern, layout)

        # E001 和 E002 之间应有 SAME_SOURCE 关系
        e1_rels = graph.get_relations("E001")
        same_source_rels = [
            r for r in e1_rels
            if r.relation_type == "same_source"
            or (r.source_id == "E001" and r.target_id == "E002")
            or (r.source_id == "E002" and r.target_id == "E001")
        ]
        assert len(same_source_rels) >= 1

    def test_promote_pattern_rule_id_increments(self, tmp_path: Path):
        """多次升维的规则 ID 递增。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)

        promoter = CrossProjectPromoter(adapter, graph, archive)

        # 第一次升维
        pattern1 = CrossProjectPattern(
            pattern_id="P001",
            name="T1",
            description="d",
            source_entries=["E001", "E002"],
            source_projects=[],
            abstract_rule_content="c1",
        )
        result1 = promoter.promote_pattern(pattern1, layout)
        rule_id1 = result1.new_global_rules[0]

        # 第二次升维
        pattern2 = CrossProjectPattern(
            pattern_id="P002",
            name="T2",
            description="d",
            source_entries=["E003", "E004"],
            source_projects=[],
            abstract_rule_content="c2",
        )
        result2 = promoter.promote_pattern(pattern2, layout)
        rule_id2 = result2.new_global_rules[0]

        # 两个规则 ID 应不同
        assert rule_id1 != rule_id2


# ---------------------------------------------------------------------------
# check_and_promote 完整被动触发流程
# ---------------------------------------------------------------------------


class TestCheckAndPromote:
    """check_and_promote 完整被动触发流程。"""

    def test_check_and_promote_triggers(self, tmp_path: Path):
        """2 个项目经验含共有关键词 → 触发再升维。"""
        layout = _make_layout(tmp_path)
        cross_root = layout.cross_project_root
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "Database session rule", "Always close database session after use")],
        )
        _write_project_archive(
            cross_root / "proj2",
            [("E002", "Database connection rule", "Always close database connection after use")],
        )

        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        assert result.triggered is True
        assert len(result.new_global_rules) >= 1
        assert len(result.patterns) >= 1

    def test_check_and_promote_no_pattern(self, tmp_path: Path):
        """2 个项目经验无共有关键词 → 不触发。"""
        layout = _make_layout(tmp_path)
        cross_root = layout.cross_project_root
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "Redis cache", "Redis cache invalidation strategy")],
        )
        _write_project_archive(
            cross_root / "proj2",
            [("E002", "GraphQL schema", "GraphQL schema design patterns")],
        )

        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        assert result.triggered is False
        assert result.new_global_rules == []
        assert result.patterns == []

    def test_check_and_promote_empty_cross_root(self, tmp_path: Path):
        """cross_project_root 为空 → 不触发。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        assert result.triggered is False

    def test_check_and_promote_single_project(self, tmp_path: Path):
        """只有 1 个项目 → 经验不足 2 → 不触发。"""
        layout = _make_layout(tmp_path)
        cross_root = layout.cross_project_root
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "T", "database session close")],
        )

        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        # 只有 1 条经验 → 无法识别模式（需 >= 2）
        assert result.triggered is False

    def test_check_and_promote_writes_global_rule(self, tmp_path: Path):
        """触发后全局规则文件实际被写入。"""
        layout = _make_layout(tmp_path)
        cross_root = layout.cross_project_root
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "Database session", "Always close database session after use")],
        )
        _write_project_archive(
            cross_root / "proj2",
            [("E002", "Database connection", "Always close database connection after use")],
        )

        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        assert result.triggered is True
        rule_id = result.new_global_rules[0]
        rule_file = layout.global_rules_dir / f"{rule_id}.md"
        assert rule_file.is_file()

    def test_check_and_promote_registers_graph(self, tmp_path: Path):
        """触发后在图谱中登记新规则节点。"""
        layout = _make_layout(tmp_path)
        cross_root = layout.cross_project_root
        _write_project_archive(
            cross_root / "proj1",
            [("E001", "Database session", "Always close database session after use")],
        )
        _write_project_archive(
            cross_root / "proj2",
            [("E002", "Database connection", "Always close database connection after use")],
        )

        adapter = TraeAdapter()
        graph = GraphRegistry(layout)
        archive = ArchiveManager(layout)
        promoter = CrossProjectPromoter(adapter, graph, archive)

        result = promoter.check_and_promote(layout)
        rule_id = result.new_global_rules[0]
        node = graph.get_node(rule_id)
        assert node is not None
        assert node.category == "跨项目通用规则"
