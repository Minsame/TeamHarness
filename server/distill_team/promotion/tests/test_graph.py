"""图谱登记模块测试。

覆盖节点管理 / 关系管理 / 环检测 / links 块生成 / 变更日志。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.models import (
    GraphNode,
    GraphRelation,
    GraphRelationType,
)


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


def _make_node(
    node_id: str = "R041",
    name: str = "测试节点",
    node_type: str = "rule",
    location: str = "/path/to/rule.md",
    category: str | None = "backend",
    status: str = "active",
) -> GraphNode:
    return GraphNode(
        node_id=node_id,
        node_type=node_type,
        name=name,
        location=location,
        category=category,
        status=status,
    )


# ---------------------------------------------------------------------------
# 节点管理
# ---------------------------------------------------------------------------


class TestRegisterNode:
    """register_node 注册新节点。"""

    def test_register_new_node(self, tmp_path: Path):
        """注册新节点 → 可查询到。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        node = _make_node(node_id="R041", name="资产归属验证")
        registry.register_node(node)

        found = registry.get_node("R041")
        assert found is not None
        assert found.node_id == "R041"
        assert found.name == "资产归属验证"
        assert found.node_type == "rule"
        assert found.status == "active"

    def test_register_creates_graph_file(self, tmp_path: Path):
        """注册节点 → 创建 graph.md 文件。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        assert not layout.graph_path.exists()
        registry.register_node(_make_node(node_id="R041"))
        assert layout.graph_path.is_file()

    def test_register_updates_existing_node(self, tmp_path: Path):
        """重复注册相同 node_id → 更新（不新增）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(_make_node(node_id="R041", name="原名"))
        registry.register_node(_make_node(node_id="R041", name="新名"))

        nodes = registry.list_nodes()
        assert len(nodes) == 1
        assert nodes[0].name == "新名"

    def test_register_multiple_nodes(self, tmp_path: Path):
        """注册多个不同节点。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        for i in range(3):
            registry.register_node(_make_node(node_id=f"R00{i}", name=f"节点{i}"))
        nodes = registry.list_nodes()
        assert len(nodes) == 3

    def test_get_node_not_found(self, tmp_path: Path):
        """查询不存在的节点 → None。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        assert registry.get_node("NON_EXISTENT") is None


class TestUpdateNodeStatus:
    """update_node_status 更新节点状态。"""

    def test_update_status(self, tmp_path: Path):
        """更新节点状态。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(_make_node(node_id="R041", status="active"))
        registry.update_node_status("R041", "archived")

        node = registry.get_node("R041")
        assert node.status == "archived"

    def test_update_status_to_pending(self, tmp_path: Path):
        """更新为 pending_confirmation。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(_make_node(node_id="R041"))
        registry.update_node_status("R041", "pending_confirmation")
        assert registry.get_node("R041").status == "pending_confirmation"

    def test_update_nonexistent_raises(self, tmp_path: Path):
        """更新不存在的节点 → ValueError。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        with pytest.raises(ValueError, match="不存在"):
            registry.update_node_status("R999", "archived")

    def test_update_preserves_other_fields(self, tmp_path: Path):
        """更新状态时保留其他字段。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(
            _make_node(node_id="R041", name="原名", category="后端")
        )
        registry.update_node_status("R041", "archived")
        node = registry.get_node("R041")
        assert node.name == "原名"
        assert node.category == "后端"
        assert node.node_type == "rule"


# ---------------------------------------------------------------------------
# 关系管理
# ---------------------------------------------------------------------------


class TestAddRelation:
    """add_relation 添加关系。"""

    def test_add_relation(self, tmp_path: Path):
        """添加关系 → get_relations 返回该关系。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="R041",
                target_id="R043",
                relation_type=GraphRelationType.DEPENDS_ON,
                note="前置依赖",
            )
        )
        rels = registry.get_relations("R041")
        assert len(rels) == 1
        assert rels[0].source_id == "R041"
        assert rels[0].target_id == "R043"
        assert rels[0].relation_type == GraphRelationType.DEPENDS_ON

    def test_add_duplicate_relation_updates_note(self, tmp_path: Path):
        """重复添加相同关系 → 更新 note（不新增）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
                note="原note",
            )
        )
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
                note="新note",
            )
        )
        rels = registry.get_relations("A")
        assert len(rels) == 1
        assert rels[0].note == "新note"

    def test_add_relation_different_types_coexist(self, tmp_path: Path):
        """相同节点间不同关系类型可共存。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.REFERENCES,
            )
        )
        rels = registry.get_relations("A")
        assert len(rels) == 2


class TestGetRelations:
    """get_relations 双向查询关系。"""

    def test_get_relations_as_source(self, tmp_path: Path):
        """作为源节点的查询。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )
        rels = registry.get_relations("A")
        assert len(rels) == 1

    def test_get_relations_as_target(self, tmp_path: Path):
        """作为目标节点的查询（双向）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )
        rels = registry.get_relations("B")
        assert len(rels) == 1
        assert rels[0].source_id == "A"

    def test_get_relations_no_relations(self, tmp_path: Path):
        """无关系的节点 → 空列表。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        assert registry.get_relations("ALONE") == []

    def test_get_relations_bidirectional(self, tmp_path: Path):
        """双向：A→B 和 C→A，查询 A 时都应返回。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )
        registry.add_relation(
            GraphRelation(
                source_id="C",
                target_id="A",
                relation_type=GraphRelationType.COVERS,
            )
        )
        rels = registry.get_relations("A")
        assert len(rels) == 2


# ---------------------------------------------------------------------------
# 环检测
# ---------------------------------------------------------------------------


class TestDetectCycleDependsOn:
    """DEPENDS_ON 环检测（成环拒绝）。"""

    def test_direct_cycle_rejected(self, tmp_path: Path):
        """A→B 后再 B→A → 拒绝（直接环）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A",
                target_id="B",
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )
        with pytest.raises(ValueError, match="环"):
            registry.add_relation(
                GraphRelation(
                    source_id="B",
                    target_id="A",
                    relation_type=GraphRelationType.DEPENDS_ON,
                )
            )

    def test_indirect_cycle_rejected(self, tmp_path: Path):
        """A→B→C 后再 C→A → 拒绝（间接环）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON)
        )
        registry.add_relation(
            GraphRelation(source_id="B", target_id="C", relation_type=GraphRelationType.DEPENDS_ON)
        )
        with pytest.raises(ValueError, match="环"):
            registry.add_relation(
                GraphRelation(source_id="C", target_id="A", relation_type=GraphRelationType.DEPENDS_ON)
            )

    def test_no_cycle_allowed(self, tmp_path: Path):
        """无环的 DEPENDS_ON 链 → 允许添加。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON)
        )
        registry.add_relation(
            GraphRelation(source_id="B", target_id="C", relation_type=GraphRelationType.DEPENDS_ON)
        )
        # A→C 不成环（A→B→C 是单向链）
        registry.add_relation(
            GraphRelation(source_id="A", target_id="C", relation_type=GraphRelationType.DEPENDS_ON)
        )
        # 三条关系都应存在
        rels = registry.get_relations("A")
        assert len(rels) == 2  # A→B 和 A→C

    def test_detect_cycle_method_direct(self, tmp_path: Path):
        """detect_cycle 方法直接调用。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON)
        )
        # 检测 B→A 是否成环 → True
        assert registry.detect_cycle("B", "A", GraphRelationType.DEPENDS_ON) is True

    def test_detect_cycle_method_no_cycle(self, tmp_path: Path):
        """detect_cycle 无环时返回 False。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON)
        )
        # 检测 A→B 是否成环 → False（A→B 已存在但 B→A 不存在）
        # 注意：检测的是添加 source→target 后是否成环
        # 添加 C→A 时，从 A 出发能否回到 C？不能 → False
        assert registry.detect_cycle("C", "A", GraphRelationType.DEPENDS_ON) is False


class TestDetectCycleNoCycle:
    """非 DEPENDS_ON 关系不触发环检测。"""

    def test_same_source_no_cycle_check(self, tmp_path: Path):
        """SAME_SOURCE 双向关系不构成环问题。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.SAME_SOURCE
            )
        )
        # SAME_SOURCE 反向添加不抛异常
        registry.add_relation(
            GraphRelation(
                source_id="B", target_id="A", relation_type=GraphRelationType.SAME_SOURCE
            )
        )
        # 两条关系都应存在（SAME_SOURCE 不去重，但 add_relation 检查相同关系去重）
        # 注意：(B→A) 与 (A→B) 是不同关系（source/target 互换）
        rels_a = registry.get_relations("A")
        assert len(rels_a) == 2

    def test_covers_no_cycle_check(self, tmp_path: Path):
        """COVERS 关系不触发环检测。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(source_id="A", target_id="B", relation_type=GraphRelationType.COVERS)
        )
        # 反向 COVERS 也不应抛异常
        registry.add_relation(
            GraphRelation(source_id="B", target_id="A", relation_type=GraphRelationType.COVERS)
        )

    def test_detect_cycle_non_depends_returns_false(self, tmp_path: Path):
        """detect_cycle 对非 DEPENDS_ON 关系一律返回 False。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        # 即使会形成"环"，非 DEPENDS_ON 也返回 False
        assert registry.detect_cycle("A", "A", GraphRelationType.SAME_SOURCE) is False
        assert registry.detect_cycle("A", "A", GraphRelationType.COVERS) is False
        assert registry.detect_cycle("A", "A", GraphRelationType.REFERENCES) is False


# ---------------------------------------------------------------------------
# links 块生成
# ---------------------------------------------------------------------------


class TestGenerateLinksBlock:
    """generate_links_block 生成 links 块。"""

    def test_empty_links_block(self, tmp_path: Path):
        """无关系的节点 → 空链接列表。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(_make_node(node_id="ALONE"))
        block = registry.generate_links_block("ALONE")
        assert "links:" in block
        assert "升维自: []" in block
        assert "覆盖: []" in block
        assert "依赖: []" in block

    def test_links_block_with_depends_on(self, tmp_path: Path):
        """含 DEPENDS_ON 关系的 links 块。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON
            )
        )
        block = registry.generate_links_block("A")
        assert "依赖: [B]" in block

    def test_links_block_with_promoted_from(self, tmp_path: Path):
        """含 PROMOTED_FROM 关系的 links 块。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="R005", target_id="E001", relation_type=GraphRelationType.PROMOTED_FROM
            )
        )
        block = registry.generate_links_block("R005")
        assert "升维自: [E001]" in block

    def test_links_block_same_source_bidirectional(self, tmp_path: Path):
        """SAME_SOURCE 关系在 links 块中双向显示。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.SAME_SOURCE
            )
        )
        # A 的同源列表应含 B
        block_a = registry.generate_links_block("A")
        assert "同源: [B]" in block_a
        # B 的同源列表也应含 A
        block_b = registry.generate_links_block("B")
        assert "同源: [A]" in block_b

    def test_links_block_format(self, tmp_path: Path):
        """links 块格式正确（6 类关系）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        block = registry.generate_links_block("X")
        # 应包含所有 6 类
        assert "升维自:" in block
        assert "覆盖:" in block
        assert "引用:" in block
        assert "依赖:" in block
        assert "同源:" in block
        assert "登记于:" in block


# ---------------------------------------------------------------------------
# 变更日志
# ---------------------------------------------------------------------------


class TestLogChange:
    """log_change 记录变更日志。"""

    def test_log_change_creates_file(self, tmp_path: Path):
        """log_change 创建 graph.md 文件。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        assert not layout.graph_path.exists()
        registry.log_change("测试变更", ["R001"])
        assert layout.graph_path.is_file()

    def test_log_change_appended(self, tmp_path: Path):
        """多次 log_change 应追加（不覆盖）。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.log_change("第一次变更", ["A"])
        registry.log_change("第二次变更", ["B"])
        text = layout.graph_path.read_text(encoding="utf-8")
        assert "第一次变更" in text
        assert "第二次变更" in text

    def test_log_change_format(self, tmp_path: Path):
        """变更日志格式正确。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.log_change("描述文本", ["R001", "R002"])
        text = layout.graph_path.read_text(encoding="utf-8")
        assert "## 变更日志" in text
        assert "描述文本" in text
        assert "R001" in text
        assert "R002" in text

    def test_register_node_logs_change(self, tmp_path: Path):
        """register_node 自动记录变更日志。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.register_node(_make_node(node_id="R041"))
        text = layout.graph_path.read_text(encoding="utf-8")
        assert "新增节点 R041" in text

    def test_add_relation_logs_change(self, tmp_path: Path):
        """add_relation 自动记录变更日志。"""
        layout = _make_layout(tmp_path)
        registry = GraphRegistry(layout)
        registry.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.DEPENDS_ON
            )
        )
        text = layout.graph_path.read_text(encoding="utf-8")
        assert "新增关系" in text
        assert "A" in text
        assert "B" in text
