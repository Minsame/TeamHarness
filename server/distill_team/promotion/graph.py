"""图谱登记模块。

实现 resource-harness 规则的"图谱登记"环节：
- 经验提炼流程.md 步骤 7：将升维后的规则登记到 graph.md
- 跨项目升维与连锁更新.md 图谱关系：管理节点间的 6 种关系

GraphRegistry 提供 graph.md 的读写接口：
- 节点管理：注册 / 查询 / 列出 / 更新状态
- 关系管理：添加 / 查询 / 移除
- 环检测：DEPENDS_ON 关系的循环依赖检测
- links 块生成：为规则完整版生成 links 块
- 变更日志：记录所有图谱变更

文件格式：Markdown 表格，含三个表（节点 / 关系 / 变更日志）。
文件操作一律 UTF-8 无 BOM。
"""

from __future__ import annotations

from datetime import datetime

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.models import (
    GraphNode,
    GraphRelation,
    GraphRelationType,
)


# ---------------------------------------------------------------------------
# 关系类型中英文映射
# ---------------------------------------------------------------------------

_RELATION_TYPE_TO_ZH: dict[str, str] = {
    GraphRelationType.PROMOTED_FROM: "升维自",
    GraphRelationType.COVERS: "覆盖",
    GraphRelationType.REFERENCES: "引用",
    GraphRelationType.DEPENDS_ON: "依赖",
    GraphRelationType.SAME_SOURCE: "同源",
    GraphRelationType.REGISTERED_IN: "登记于",
}

_ZH_TO_RELATION_TYPE: dict[str, str] = {v: k for k, v in _RELATION_TYPE_TO_ZH.items()}


# ---------------------------------------------------------------------------
# GraphRegistry
# ---------------------------------------------------------------------------


class GraphRegistry:
    """图谱登记器。

    管理 graph.md 文件，提供节点和关系的增删查改接口。

    用法：
        registry = GraphRegistry(layout)
        registry.register_node(GraphNode(
            node_id="R041",
            node_type="rule",
            name="资产归属验证",
            location="~/.trae-cn/rules/xxx-rules.md",
            category="后端资产访问控制",
            status="active",
        ))
        registry.add_relation(GraphRelation(
            source_id="R041",
            target_id="R043",
            relation_type=GraphRelationType.DEPENDS_ON,
            note="owner 校验前置",
        ))
        block = registry.generate_links_block("R041")
    """

    def __init__(self, layout: MemoryLayout) -> None:
        self._layout = layout

    # ------------------------------------------------------------------
    # 文件读写
    # ------------------------------------------------------------------

    def _read_file(self) -> str:
        """读取 graph.md 内容，文件不存在返回空字符串。"""
        path = self._layout.graph_path
        if not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _write_file(self, content: str) -> None:
        """写入 graph.md，确保目录存在。UTF-8 无 BOM。"""
        path = self._layout.graph_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    # ------------------------------------------------------------------
    # 解析与序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _is_separator_row(line: str) -> bool:
        """判断是否为 Markdown 表格分隔行（如 |---|---|）。"""
        stripped = line.strip()
        if not stripped.startswith("|"):
            return False
        inner = stripped[1:-1] if stripped.endswith("|") else stripped[1:]
        # 去掉列分隔符 | 后，剩余字符只能是 -、: 或空格，且至少含一个 -
        inner_no_pipe = inner.replace("|", "")
        return all(c in "-: " for c in inner_no_pipe) and "-" in inner_no_pipe

    @staticmethod
    def _parse_table_row(line: str) -> list[str]:
        """解析 Markdown 表格行，返回单元格列表。"""
        stripped = line.strip()
        if stripped.startswith("|"):
            stripped = stripped[1:]
        if stripped.endswith("|"):
            stripped = stripped[:-1]
        return [cell.strip() for cell in stripped.split("|")]

    def _parse(
        self, content: str
    ) -> tuple[list[GraphNode], list[GraphRelation], list[tuple[str, str, str]]]:
        """解析 graph.md 内容。

        返回 (节点列表, 关系列表, 日志列表)，日志每项为 (时间, 描述, 涉及节点)。
        """
        nodes: list[GraphNode] = []
        relations: list[GraphRelation] = []
        logs: list[tuple[str, str, str]] = []

        section: str | None = None
        past_separator = False

        for line in content.splitlines():
            stripped = line.strip()

            # 检测 section 切换
            if stripped.startswith("## "):
                if stripped == "## 节点":
                    section = "nodes"
                elif stripped == "## 关系":
                    section = "relations"
                elif stripped == "## 变更日志":
                    section = "logs"
                else:
                    section = None
                past_separator = False
                continue

            if section is None:
                continue

            if not stripped:
                continue

            if not stripped.startswith("|"):
                continue

            # 跳过表头行，直到遇到分隔行
            if not past_separator:
                if self._is_separator_row(stripped):
                    past_separator = True
                continue

            # 解析数据行
            cells = self._parse_table_row(stripped)

            if section == "nodes" and len(cells) >= 6:
                node_id, name, node_type, category, location, status = cells[:6]
                nodes.append(
                    GraphNode(
                        node_id=node_id,
                        name=name,
                        node_type=node_type,
                        category=category or None,
                        location=location,
                        status=status or "active",
                    )
                )
            elif section == "relations" and len(cells) >= 4:
                source_id, rt_zh, target_id, note = cells[:4]
                relation_type = _ZH_TO_RELATION_TYPE.get(rt_zh)
                if relation_type is not None:
                    relations.append(
                        GraphRelation(
                            source_id=source_id,
                            target_id=target_id,
                            relation_type=relation_type,  # type: ignore[arg-type]
                            note=note,
                        )
                    )
            elif section == "logs" and len(cells) >= 3:
                time_str, desc, involved = cells[:3]
                logs.append((time_str, desc, involved))

        return nodes, relations, logs

    def _serialize(
        self,
        nodes: list[GraphNode],
        relations: list[GraphRelation],
        logs: list[tuple[str, str, str]],
    ) -> str:
        """序列化为 graph.md 格式的字符串。"""
        lines: list[str] = []
        lines.append("# 规则图谱索引")
        lines.append("")
        lines.append("## 节点")
        lines.append("")
        lines.append("| 节点ID | 名称 | 类型 | 分类 | 位置 | 状态 |")
        lines.append("|--------|------|------|------|------|------|")
        for n in nodes:
            category = n.category or ""
            lines.append(
                f"| {n.node_id} | {n.name} | {n.node_type} | {category} | {n.location} | {n.status} |"
            )
        lines.append("")
        lines.append("## 关系")
        lines.append("")
        lines.append("| 源节点 | 关系类型 | 目标节点 | 说明 |")
        lines.append("|--------|---------|---------|------|")
        for r in relations:
            rt_zh = _RELATION_TYPE_TO_ZH.get(r.relation_type, r.relation_type)
            lines.append(f"| {r.source_id} | {rt_zh} | {r.target_id} | {r.note} |")
        lines.append("")
        lines.append("## 变更日志")
        lines.append("")
        lines.append("| 时间 | 变更 | 涉及节点 |")
        lines.append("|------|------|---------|")
        for time_str, desc, involved in logs:
            lines.append(f"| {time_str} | {desc} | {involved} |")
        lines.append("")  # 末尾空行
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 节点管理
    # ------------------------------------------------------------------

    def register_node(self, node: GraphNode) -> None:
        """注册新节点到 graph.md。

        1. 检查节点是否已存在（按 node_id）
        2. 若存在 → 更新节点信息（全字段替换）
        3. 若不存在 → 追加到节点表
        4. 记录变更日志
        """
        content = self._read_file()
        nodes, relations, logs = self._parse(content)

        today = datetime.now().strftime("%Y-%m-%d")

        # 检查是否已存在
        found = False
        for i, n in enumerate(nodes):
            if n.node_id == node.node_id:
                nodes[i] = node
                logs.append((today, f"更新节点 {node.node_id}", node.node_id))
                found = True
                break

        if not found:
            nodes.append(node)
            logs.append(
                (
                    today,
                    f"新增节点 {node.node_id} ({node.node_type})",
                    node.node_id,
                )
            )

        self._write_file(self._serialize(nodes, relations, logs))

    def get_node(self, node_id: str) -> GraphNode | None:
        """查询节点。"""
        nodes = self._load_nodes()
        for n in nodes:
            if n.node_id == node_id:
                return n
        return None

    def list_nodes(self) -> list[GraphNode]:
        """列出所有节点。"""
        return self._load_nodes()

    def update_node_status(self, node_id: str, status: str) -> None:
        """更新节点状态。若节点不存在抛出 ValueError。"""
        content = self._read_file()
        nodes, relations, logs = self._parse(content)

        found = False
        for i, n in enumerate(nodes):
            if n.node_id == node_id:
                nodes[i] = GraphNode(
                    node_id=n.node_id,
                    node_type=n.node_type,
                    name=n.name,
                    location=n.location,
                    category=n.category,
                    status=status,
                )
                found = True
                break

        if not found:
            raise ValueError(f"节点 {node_id} 不存在")

        today = datetime.now().strftime("%Y-%m-%d")
        logs.append(
            (today, f"更新节点 {node_id} 状态为 {status}", node_id)
        )

        self._write_file(self._serialize(nodes, relations, logs))

    # ------------------------------------------------------------------
    # 关系管理
    # ------------------------------------------------------------------

    def add_relation(self, relation: GraphRelation) -> None:
        """添加关系。

        1. 检查是否会形成环（仅 DEPENDS_ON 关系需要检查）
        2. 若成环 → 拒绝添加，抛出 ValueError
        3. 若已存在相同关系 → 更新 note（如有变化）
        4. 若不存在 → 追加到关系表
        5. 记录变更日志
        """
        # 环检测（DEPENDS_ON 专属）
        if self.detect_cycle(
            relation.source_id, relation.target_id, relation.relation_type
        ):
            raise ValueError(
                f"添加关系 {relation.source_id} → {relation.target_id} "
                f"({relation.relation_type}) 会形成环"
            )

        content = self._read_file()
        nodes, relations, logs = self._parse(content)

        today = datetime.now().strftime("%Y-%m-%d")
        rt_zh = _RELATION_TYPE_TO_ZH.get(relation.relation_type, relation.relation_type)

        # 检查是否已存在相同关系（去重 + 更新 note）
        for i, r in enumerate(relations):
            if (
                r.source_id == relation.source_id
                and r.target_id == relation.target_id
                and r.relation_type == relation.relation_type
            ):
                if relation.note and r.note != relation.note:
                    relations[i] = GraphRelation(
                        source_id=r.source_id,
                        target_id=r.target_id,
                        relation_type=r.relation_type,
                        note=relation.note,
                    )
                    logs.append(
                        (
                            today,
                            f"更新关系 {relation.source_id} → {relation.target_id} ({rt_zh})",
                            ", ".join([relation.source_id, relation.target_id]),
                        )
                    )
                    self._write_file(self._serialize(nodes, relations, logs))
                return

        # 追加新关系
        relations.append(relation)
        logs.append(
            (
                today,
                f"新增关系 {relation.source_id} → {relation.target_id} ({rt_zh})",
                ", ".join([relation.source_id, relation.target_id]),
            )
        )
        self._write_file(self._serialize(nodes, relations, logs))

    def get_relations(self, node_id: str) -> list[GraphRelation]:
        """查询某节点的所有关联关系（双向：作为源或目标）。"""
        relations = self._load_relations()
        return [
            r
            for r in relations
            if r.source_id == node_id or r.target_id == node_id
        ]

    def remove_relation(
        self,
        source_id: str,
        target_id: str,
        relation_type: GraphRelationType,
    ) -> None:
        """移除关系。若关系不存在则静默返回（幂等）。"""
        content = self._read_file()
        nodes, relations, logs = self._parse(content)

        new_relations: list[GraphRelation] = []
        removed = False
        for r in relations:
            if (
                r.source_id == source_id
                and r.target_id == target_id
                and r.relation_type == relation_type
            ):
                removed = True
                continue
            new_relations.append(r)

        if removed:
            today = datetime.now().strftime("%Y-%m-%d")
            rt_zh = _RELATION_TYPE_TO_ZH.get(relation_type, relation_type)
            logs.append(
                (
                    today,
                    f"移除关系 {source_id} → {target_id} ({rt_zh})",
                    ", ".join([source_id, target_id]),
                )
            )
            self._write_file(self._serialize(nodes, new_relations, logs))

    # ------------------------------------------------------------------
    # 环检测
    # ------------------------------------------------------------------

    def detect_cycle(
        self,
        source_id: str,
        target_id: str,
        relation_type: GraphRelationType,
    ) -> bool:
        """检测添加 source_id → target_id 关系是否会形成环。

        - PROMOTED_FROM / COVERS / REFERENCES / REGISTERED_IN：天然有向无环，返回 False
        - SAME_SOURCE：双向关系，不构成环问题，返回 False
        - DEPENDS_ON：检查 target_id 是否已（直接或间接）依赖 source_id
          （即从 target_id 出发沿 DEPENDS_ON 边能否回到 source_id）
        """
        # 仅 DEPENDS_ON 需要环检测
        if relation_type != GraphRelationType.DEPENDS_ON:
            return False

        relations = self._load_relations()

        # 构建 DEPENDS_ON 邻接表：source → [target, ...]
        adj: dict[str, list[str]] = {}
        for r in relations:
            if r.relation_type == GraphRelationType.DEPENDS_ON:
                adj.setdefault(r.source_id, []).append(r.target_id)

        # DFS 从 target_id 出发，检查能否到达 source_id
        visited: set[str] = set()
        stack: list[str] = [target_id]
        while stack:
            node = stack.pop()
            if node == source_id:
                return True
            if node in visited:
                continue
            visited.add(node)
            stack.extend(adj.get(node, []))

        return False

    # ------------------------------------------------------------------
    # links 块生成
    # ------------------------------------------------------------------

    def generate_links_block(self, node_id: str) -> str:
        """生成节点的 links 块。

        按六种关系类型组织：
        - 升维自 / 覆盖 / 引用 / 依赖 / 登记于：列出该节点作为源节点的 target 列表
        - 同源：双向关系，列出该节点参与的所有 SAME_SOURCE 关系的对端节点

        返回格式：
            links:
              升维自: [E001]
              覆盖: []
              引用: []
              依赖: []
              同源: [R002]
              登记于: []
        """
        relations = self._load_relations()

        promoted_from: list[str] = []
        covers: list[str] = []
        references: list[str] = []
        depends_on: list[str] = []
        same_source: list[str] = []
        registered_in: list[str] = []

        for r in relations:
            if r.relation_type == GraphRelationType.PROMOTED_FROM:
                if r.source_id == node_id:
                    promoted_from.append(r.target_id)
            elif r.relation_type == GraphRelationType.COVERS:
                if r.source_id == node_id:
                    covers.append(r.target_id)
            elif r.relation_type == GraphRelationType.REFERENCES:
                if r.source_id == node_id:
                    references.append(r.target_id)
            elif r.relation_type == GraphRelationType.DEPENDS_ON:
                if r.source_id == node_id:
                    depends_on.append(r.target_id)
            elif r.relation_type == GraphRelationType.SAME_SOURCE:
                # 同源是双向关系，列出对端节点
                if r.source_id == node_id:
                    same_source.append(r.target_id)
                elif r.target_id == node_id:
                    same_source.append(r.source_id)
            elif r.relation_type == GraphRelationType.REGISTERED_IN:
                if r.source_id == node_id:
                    registered_in.append(r.target_id)

        def fmt(ids: list[str]) -> str:
            return "[" + ", ".join(ids) + "]"

        lines = [
            "links:",
            f"  升维自: {fmt(promoted_from)}",
            f"  覆盖: {fmt(covers)}",
            f"  引用: {fmt(references)}",
            f"  依赖: {fmt(depends_on)}",
            f"  同源: {fmt(same_source)}",
            f"  登记于: {fmt(registered_in)}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 变更日志
    # ------------------------------------------------------------------

    def log_change(self, description: str, involved_nodes: list[str]) -> None:
        """在变更日志表中追加一行。

        格式：| {当前日期} | {description} | {节点列表} |
        节点列表用逗号 + 空格分隔。
        """
        content = self._read_file()
        nodes, relations, logs = self._parse(content)

        today = datetime.now().strftime("%Y-%m-%d")
        involved = ", ".join(involved_nodes)
        logs.append((today, description, involved))

        self._write_file(self._serialize(nodes, relations, logs))

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _load_nodes(self) -> list[GraphNode]:
        """从文件加载所有节点。"""
        content = self._read_file()
        nodes, _, _ = self._parse(content)
        return nodes

    def _load_relations(self) -> list[GraphRelation]:
        """从文件加载所有关系。"""
        content = self._read_file()
        _, relations, _ = self._parse(content)
        return relations


__all__ = ["GraphRegistry"]
