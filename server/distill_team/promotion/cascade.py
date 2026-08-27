"""连锁更新模块。

实现 resource-harness 规则的「连锁更新」环节（跨项目升维与连锁更新.md）。
当图谱中某节点发生变更时，沿图谱关系连锁检查所有关联节点是否需要同步更新。

核心流程（含环检测）：
1. 变更发生 → 初始化访问栈 visited_stack = [源节点]
2. 查图谱 → 查找源节点的所有关联节点（沿所有关系类型，双向）
3. 逐个评估 → 对每个关联节点递归评估
   a. 环检测 → 节点已在访问栈中 → 标记 pending_confirmation / 记录环
   b. 否则 → 入栈 → 判断是否需更新 → 递归 / 弹出
4. 记录变更 → 在 graph.md 变更日志中记录

环处理策略：
- 依赖(DEPENDS_ON)成环 → 标记 pending_confirmation，循环依赖需人工拆解
- 同源(SAME_SOURCE)成环 → 不处理（正常双向关系）
- 混合/其他关系成环 → 标记 pending_confirmation，人工审查
"""

from __future__ import annotations

from dataclasses import dataclass

from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.models import GraphRelationType


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class CascadeUpdate:
    """单次连锁更新操作。"""

    node_id: str  # 被检查的节点
    action: str  # 执行的动作（如 "status_updated" / "no_change" / "cycle_detected"）
    relation_type: GraphRelationType | None  # 触发更新的关系类型
    detail: str  # 详细说明


@dataclass
class CascadeResult:
    """连锁更新结果。"""

    source_node_id: str  # 触发源节点
    updates: list[CascadeUpdate]  # 所有执行的更新
    cycles_detected: list[list[str]]  # 检测到的环路径
    pending_confirmation_nodes: list[str]  # 标记为 pending_confirmation 的节点


# ---------------------------------------------------------------------------
# 连锁更新管理器
# ---------------------------------------------------------------------------


class CascadeUpdater:
    """连锁更新管理器。

    从源节点开始，沿图谱关系递归检查所有关联节点是否需要同步更新。
    使用访问栈(visited_stack)检测环，并根据关系类型采取不同的环处理策略。

    用法：
        updater = CascadeUpdater(graph_registry)
        result = updater.cascade_from("R041", change_type="modified")
        if result.cycles_detected:
            print(f"检测到 {len(result.cycles_detected)} 个环")
    """

    def __init__(self, graph: GraphRegistry) -> None:
        self._graph = graph
        # 当前级联的变更类型，供 _evaluate_node 内部方法使用
        self._change_type: str = "modified"

    def cascade_from(
        self, source_node_id: str, change_type: str = "modified"
    ) -> CascadeResult:
        """从源节点开始连锁更新。

        1. 初始化 visited_stack = [source_node_id]
        2. 查询源节点的所有关联关系（双向）
        3. 对每个关联节点递归评估
        4. 返回 CascadeResult

        Args:
            source_node_id: 触发变更的源节点 ID
            change_type: 变更类型（如 "modified" / "added" / "deleted"）

        Returns:
            CascadeResult: 连锁更新结果
        """
        self._change_type = change_type

        updates: list[CascadeUpdate] = []
        cycles: list[list[str]] = []
        pending_nodes: list[str] = []

        # 初始化访问栈
        visited_stack: list[str] = [source_node_id]

        # 查询源节点的所有关联关系（双向：作为源或目标）
        relations = self._graph.get_relations(source_node_id)

        for relation in relations:
            # 确定关联的另一端节点
            if relation.source_id == source_node_id:
                other_node_id = relation.target_id
            else:
                other_node_id = relation.source_id

            self._evaluate_node(
                node_id=other_node_id,
                source_id=source_node_id,
                relation_type=relation.relation_type,
                visited_stack=visited_stack,
                updates=updates,
                cycles=cycles,
                pending_nodes=pending_nodes,
            )

        # 记录整体连锁更新变更日志
        self._graph.log_change(
            description=(
                f"连锁更新：节点 {source_node_id} 变更({change_type})，"
                f"执行了 {len(updates)} 个关联操作，"
                f"检测到 {len(cycles)} 个环"
            ),
            involved_nodes=[source_node_id],
        )

        return CascadeResult(
            source_node_id=source_node_id,
            updates=updates,
            cycles_detected=cycles,
            pending_confirmation_nodes=pending_nodes,
        )

    def _evaluate_node(
        self,
        node_id: str,
        source_id: str,
        relation_type: GraphRelationType,
        visited_stack: list[str],
        updates: list[CascadeUpdate],
        cycles: list[list[str]],
        pending_nodes: list[str],
    ) -> None:
        """递归评估单个节点是否需要更新。

        1. 环检测：node_id 在 visited_stack 中？
           - 是 → 记录环路径，根据关系类型处理
           - 否 → 继续
        2. 将 node_id 加入 visited_stack
        3. 判断是否需要同步更新（按关系类型）
        4. 若需要 → 执行更新 → 递归检查 node_id 的关联节点
        5. 若不需要 → 从 visited_stack 弹出
        """
        # 1. 环检测
        if node_id in visited_stack:
            # 提取环路径：从 node_id 首次出现位置到末尾，再加 node_id 形成闭环
            cycle_path = visited_stack[visited_stack.index(node_id):] + [node_id]
            cycles.append(cycle_path)
            self._handle_cycle(
                node_id=node_id,
                relation_type=relation_type,
                cycle_path=cycle_path,
                updates=updates,
                pending_nodes=pending_nodes,
            )
            return  # 停止该分支，不继续递归

        # 2. 将 node_id 加入访问栈
        visited_stack.append(node_id)

        # 3. 判断是否需要同步更新
        if self._needs_update(
            node_id, source_id, relation_type, self._change_type
        ):
            # 4. 执行更新
            self._apply_update(
                node_id=node_id,
                source_id=source_id,
                relation_type=relation_type,
                change_type=self._change_type,
                updates=updates,
            )

            # 递归检查 node_id 的关联节点
            relations = self._graph.get_relations(node_id)
            for relation in relations:
                # 确定关联的另一端节点
                if relation.source_id == node_id:
                    other_node_id = relation.target_id
                else:
                    other_node_id = relation.source_id

                self._evaluate_node(
                    node_id=other_node_id,
                    source_id=node_id,
                    relation_type=relation.relation_type,
                    visited_stack=visited_stack,
                    updates=updates,
                    cycles=cycles,
                    pending_nodes=pending_nodes,
                )
        else:
            # 5. 不需要更新，记录 no_change
            updates.append(
                CascadeUpdate(
                    node_id=node_id,
                    action="no_change",
                    relation_type=relation_type,
                    detail=(
                        f"节点 {node_id} 因 {relation_type} 关系被检查，"
                        f"无需更新"
                    ),
                )
            )

        # 回溯：从访问栈弹出
        visited_stack.pop()

    def _handle_cycle(
        self,
        node_id: str,
        relation_type: GraphRelationType,
        cycle_path: list[str],
        updates: list[CascadeUpdate],
        pending_nodes: list[str],
    ) -> None:
        """处理检测到的环。

        根据关系类型采取不同策略：
        - DEPENDS_ON → 标记 pending_confirmation，循环依赖需人工拆解
        - SAME_SOURCE → 不处理（正常双向关系）
        - 其他/混合 → 标记 pending_confirmation，人工审查
        """
        cycle_str = " → ".join(cycle_path)

        # 同源关系成环是正常双向关系，不处理
        if relation_type == GraphRelationType.SAME_SOURCE:
            updates.append(
                CascadeUpdate(
                    node_id=node_id,
                    action="cycle_detected",
                    relation_type=relation_type,
                    detail=(
                        f"同源关系成环 {cycle_str}，"
                        f"正常双向关系，不处理"
                    ),
                )
            )
            return  # 不标记 pending_confirmation

        # DEPENDS_ON 或混合关系成环 → 标记 pending_confirmation
        if relation_type == GraphRelationType.DEPENDS_ON:
            reason = "依赖关系成环，循环依赖需人工拆解"
        else:
            reason = "混合关系成环，需人工审查"

        # 标记节点为 pending_confirmation（仅当节点存在时）
        if self._graph.get_node(node_id) is not None:
            self._graph.update_node_status(node_id, "pending_confirmation")

        pending_nodes.append(node_id)
        updates.append(
            CascadeUpdate(
                node_id=node_id,
                action="cycle_detected",
                relation_type=relation_type,
                detail=f"{reason}：{cycle_str}",
            )
        )

        # 在 graph.md 变更日志中记录环
        self._graph.log_change(
            description=f"检测到环：{cycle_str}（{reason}）",
            involved_nodes=cycle_path,
        )

    def _needs_update(
        self,
        node_id: str,
        source_id: str,
        relation_type: GraphRelationType,
        change_type: str,
    ) -> bool:
        """判断关联节点是否需要同步更新。

        按关系类型的连锁更新行为：
        - PROMOTED_FROM: source 修改 → 检查来源经验是否仍被覆盖
        - COVERS: source 修改 → 检查项目级指针是否需更新
        - REFERENCES: source 修改 → 检查热点版是否需同步
        - DEPENDS_ON: source 修改 → 检查依赖方是否受影响
        - SAME_SOURCE: 新增同源关系 → 检查是否触发再升维
        - REGISTERED_IN: source 修改 → 同步更新索引

        简化实现：所有关系类型都返回 True（需要检查），
        实际是否更新由 _apply_update 决定。
        """
        return True

    def _apply_update(
        self,
        node_id: str,
        source_id: str,
        relation_type: GraphRelationType,
        change_type: str,
        updates: list[CascadeUpdate],
    ) -> None:
        """执行更新：记录更新操作并在 graph.md 中记录变更。

        Args:
            node_id: 被更新的节点
            source_id: 触发更新的源节点
            relation_type: 触发更新的关系类型
            change_type: 变更类型
            updates: 更新记录列表（追加到此处）
        """
        updates.append(
            CascadeUpdate(
                node_id=node_id,
                action="status_updated",
                relation_type=relation_type,
                detail=(
                    f"节点 {node_id} 因 {source_id} 的 "
                    f"{relation_type} 关系变更({change_type})需要检查"
                ),
            )
        )

        # 在 graph.md 变更日志中记录
        self._graph.log_change(
            description=(
                f"连锁更新：{source_id} 变更({change_type})，"
                f"检查关联节点 {node_id}（关系: {relation_type}）"
            ),
            involved_nodes=[source_id, node_id],
        )


__all__ = [
    "CascadeResult",
    "CascadeUpdate",
    "CascadeUpdater",
]
