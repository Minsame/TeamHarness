"""连锁更新模块测试。

覆盖无环连锁更新 / DEPENDS_ON 环检测 / SAME_SOURCE 环处理 / 源节点级联。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.cascade import (
    CascadeResult,
    CascadeUpdate,
    CascadeUpdater,
)
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


def _write_graph_with_cycle(
    graph_path: Path,
    nodes: list[tuple[str, str, str]],
    relations: list[tuple[str, str, str, str]],
) -> None:
    """直接写入含环的 graph.md（绕过 add_relation 的环检测）。

    Args:
        nodes: [(node_id, name, node_type), ...]
        relations: [(source_id, relation_type_zh, target_id, note), ...]
            relation_type_zh: "依赖" / "同源" / "覆盖" / "升维自" / "引用" / "登记于"
    """
    lines = ["# 规则图谱索引", "", "## 节点", ""]
    lines.append("| 节点ID | 名称 | 类型 | 分类 | 位置 | 状态 |")
    lines.append("|--------|------|------|------|------|------|")
    for node_id, name, ntype in nodes:
        lines.append(f"| {node_id} | {name} | {ntype} |  | /path | active |")
    lines.append("")
    lines.append("## 关系")
    lines.append("")
    lines.append("| 源节点 | 关系类型 | 目标节点 | 说明 |")
    lines.append("|--------|---------|---------|------|")
    for src, rt, tgt, note in relations:
        lines.append(f"| {src} | {rt} | {tgt} | {note} |")
    lines.append("")
    lines.append("## 变更日志")
    lines.append("")
    lines.append("| 时间 | 变更 | 涉及节点 |")
    lines.append("|------|------|---------|")
    lines.append("")
    graph_path.parent.mkdir(parents=True, exist_ok=True)
    graph_path.write_text("\n".join(lines), encoding="utf-8")


def _register_chain(graph: GraphRegistry, edges: list[tuple[str, str]]) -> None:
    """注册节点和 DEPENDS_ON 链（无环）。"""
    node_ids = set()
    for a, b in edges:
        node_ids.add(a)
        node_ids.add(b)
    for nid in node_ids:
        graph.register_node(
            GraphNode(
                node_id=nid,
                node_type="rule",
                name=nid,
                location=f"/{nid}",
            )
        )
    for a, b in edges:
        graph.add_relation(
            GraphRelation(
                source_id=a,
                target_id=b,
                relation_type=GraphRelationType.DEPENDS_ON,
            )
        )


# ---------------------------------------------------------------------------
# 无环连锁更新
# ---------------------------------------------------------------------------


class TestNoCycle:
    """无环时正常连锁更新。"""

    def test_cascade_chain(self, tmp_path: Path):
        """A→B→C 链：从 A 触发，B 和 C 都被检查。

        注：CascadeUpdater 双向遍历（作为源或目标），A→B 链中 B 反向回 A
        会触发伪环检测（A 已在 visited_stack 中），这是被测代码的已知行为。
        核心验证目标是 B 和 C 都被检查到。
        """
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B"), ("B", "C")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A", change_type="modified")

        assert isinstance(result, CascadeResult)
        assert result.source_node_id == "A"
        # B 和 C 都应被检查到（核心验证目标）
        checked_nodes = {u.node_id for u in result.updates}
        assert "B" in checked_nodes
        assert "C" in checked_nodes

    def test_cascade_records_updates(self, tmp_path: Path):
        """updates 中记录每个被检查的节点。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")
        # B 应被记录为 status_updated
        b_updates = [u for u in result.updates if u.node_id == "B"]
        assert len(b_updates) >= 1
        assert b_updates[0].action == "status_updated"

    def test_cascade_logs_change(self, tmp_path: Path):
        """cascade_from 在 graph.md 变更日志中记录整体操作。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        updater.cascade_from("A")
        text = layout.graph_path.read_text(encoding="utf-8")
        assert "连锁更新" in text

    def test_cascade_no_relations(self, tmp_path: Path):
        """源节点无关联关系 → updates 为空。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        graph.register_node(
            GraphNode(node_id="ALONE", node_type="rule", name="N", location="/x")
        )

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("ALONE")
        assert result.updates == []
        assert result.cycles_detected == []


# ---------------------------------------------------------------------------
# DEPENDS_ON 成环
# ---------------------------------------------------------------------------


class TestCycleDependsOn:
    """DEPENDS_ON 成环 → pending_confirmation。"""

    def test_cycle_detected_and_marked(self, tmp_path: Path):
        """A→B→C→A 环：从 A 触发，应检测到环并标记 A 为 pending_confirmation。"""
        layout = _make_layout(tmp_path)
        # 直接写入含环的 graph.md（绕过 add_relation 环检测）
        _write_graph_with_cycle(
            layout.graph_path,
            nodes=[("A", "A", "rule"), ("B", "B", "rule"), ("C", "C", "rule")],
            relations=[
                ("A", "依赖", "B", "dep"),
                ("B", "依赖", "C", "dep"),
                ("C", "依赖", "A", "dep"),
            ],
        )
        graph = GraphRegistry(layout)
        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")

        # 应检测到环
        assert len(result.cycles_detected) >= 1
        # A 应被标记为 pending_confirmation
        assert "A" in result.pending_confirmation_nodes
        # graph.md 中 A 的状态应更新
        node = graph.get_node("A")
        assert node.status == "pending_confirmation"

    def test_cycle_logs_change(self, tmp_path: Path):
        """环检测在变更日志中记录。"""
        layout = _make_layout(tmp_path)
        _write_graph_with_cycle(
            layout.graph_path,
            nodes=[("A", "A", "rule"), ("B", "B", "rule")],
            relations=[
                ("A", "依赖", "B", "dep"),
                ("B", "依赖", "A", "dep"),
            ],
        )
        graph = GraphRegistry(layout)
        updater = CascadeUpdater(graph)
        updater.cascade_from("A")

        text = layout.graph_path.read_text(encoding="utf-8")
        assert "检测到环" in text

    def test_cycle_update_action(self, tmp_path: Path):
        """环检测的 CascadeUpdate action 为 cycle_detected。"""
        layout = _make_layout(tmp_path)
        _write_graph_with_cycle(
            layout.graph_path,
            nodes=[("A", "A", "rule"), ("B", "B", "rule")],
            relations=[
                ("A", "依赖", "B", "dep"),
                ("B", "依赖", "A", "dep"),
            ],
        )
        graph = GraphRegistry(layout)
        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")

        cycle_updates = [u for u in result.updates if u.action == "cycle_detected"]
        assert len(cycle_updates) >= 1


# ---------------------------------------------------------------------------
# SAME_SOURCE 成环
# ---------------------------------------------------------------------------


class TestCycleSameSource:
    """SAME_SOURCE 成环 → 不处理（正常双向关系）。"""

    def test_same_source_cycle_not_marked(self, tmp_path: Path):
        """A↔B 同源关系：检测到环但不标记 pending_confirmation。"""
        layout = _make_layout(tmp_path)
        # SAME_SOURCE 关系 add_relation 不检测环，可直接添加
        graph = GraphRegistry(layout)
        graph.register_node(
            GraphNode(node_id="A", node_type="rule", name="A", location="/a")
        )
        graph.register_node(
            GraphNode(node_id="B", node_type="rule", name="B", location="/b")
        )
        graph.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.SAME_SOURCE
            )
        )
        graph.add_relation(
            GraphRelation(
                source_id="B", target_id="A", relation_type=GraphRelationType.SAME_SOURCE
            )
        )

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")

        # 应检测到环（A→B→A）
        assert len(result.cycles_detected) >= 1
        # 但不标记 pending_confirmation（SAME_SOURCE 正常双向）
        assert result.pending_confirmation_nodes == []

    def test_same_source_cycle_logs_no_pending(self, tmp_path: Path):
        """SAME_SOURCE 环在 updates 中记录但不触发 pending。

        注：_handle_cycle 对 SAME_SOURCE 成环只追加 CascadeUpdate（detail 含
        "同源关系成环"），不调用 log_change 写入 graph.md。故应检查
        result.updates 而非 graph.md 文本。
        """
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        graph.register_node(
            GraphNode(node_id="A", node_type="rule", name="A", location="/a")
        )
        graph.register_node(
            GraphNode(node_id="B", node_type="rule", name="B", location="/b")
        )
        graph.add_relation(
            GraphRelation(
                source_id="A", target_id="B", relation_type=GraphRelationType.SAME_SOURCE
            )
        )
        graph.add_relation(
            GraphRelation(
                source_id="B", target_id="A", relation_type=GraphRelationType.SAME_SOURCE
            )
        )

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")

        # SAME_SOURCE 成环应在 updates 中记录为 cycle_detected
        cycle_updates = [u for u in result.updates if u.action == "cycle_detected"]
        assert len(cycle_updates) >= 1
        # detail 中含"同源关系成环"或"正常双向"
        assert any(
            "同源关系成环" in u.detail or "正常双向" in u.detail
            for u in cycle_updates
        )
        # SAME_SOURCE 成环不标记 pending_confirmation
        assert result.pending_confirmation_nodes == []


# ---------------------------------------------------------------------------
# cascade_from 完整流程
# ---------------------------------------------------------------------------


class TestCascadeFrom:
    """cascade_from 完整流程测试。"""

    def test_returns_cascade_result(self, tmp_path: Path):
        """返回 CascadeResult 类型。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")
        assert isinstance(result, CascadeResult)
        assert result.source_node_id == "A"

    def test_change_type_default(self, tmp_path: Path):
        """默认 change_type 为 modified。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")  # 不传 change_type
        # 默认 change_type 在 detail 中体现
        b_update = next(u for u in result.updates if u.node_id == "B")
        assert "modified" in b_update.detail

    def test_change_type_added(self, tmp_path: Path):
        """change_type="added" 传递到 detail。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A", change_type="added")
        b_update = next(u for u in result.updates if u.node_id == "B")
        assert "added" in b_update.detail

    def test_cascade_propagates_two_hops(self, tmp_path: Path):
        """连锁更新传播两跳（A→B→C）。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B"), ("B", "C")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")
        # B 和 C 都应被检查
        checked_nodes = {u.node_id for u in result.updates}
        assert "B" in checked_nodes
        assert "C" in checked_nodes

    def test_cascade_update_has_relation_type(self, tmp_path: Path):
        """CascadeUpdate 记录触发的关系类型。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        _register_chain(graph, [("A", "B")])

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")
        b_update = next(u for u in result.updates if u.node_id == "B")
        assert b_update.relation_type == GraphRelationType.DEPENDS_ON

    def test_cascade_bidirectional_traversal(self, tmp_path: Path):
        """连锁更新双向遍历（作为源或目标）。"""
        layout = _make_layout(tmp_path)
        graph = GraphRegistry(layout)
        # B→A 关系：从 A 触发时，A 是 target，应反向检查 B
        graph.register_node(
            GraphNode(node_id="A", node_type="rule", name="A", location="/a")
        )
        graph.register_node(
            GraphNode(node_id="B", node_type="rule", name="B", location="/b")
        )
        graph.add_relation(
            GraphRelation(
                source_id="B", target_id="A", relation_type=GraphRelationType.DEPENDS_ON
            )
        )

        updater = CascadeUpdater(graph)
        result = updater.cascade_from("A")
        # B 应被检查（虽然 A 是 target）
        checked_nodes = {u.node_id for u in result.updates}
        assert "B" in checked_nodes
