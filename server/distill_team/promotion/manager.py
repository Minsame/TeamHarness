"""升维管理编排器（manager.py）。

编排 promotion 子包的所有子模块，实现完整的升维流程：
查重 → 回测 → 升维 → 归档 → 图谱登记 → 跨项目再升维 → 连锁更新

对应 resource-harness 规则的经验提炼完整流程（经验提炼流程.md + 跨项目升维与连锁更新.md）。

接入点：TeamDistill._distill_clusters 产出 DistilledPrompt 后调用
PromotionOrchestrator.promote(prompt) 执行升维。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.adapters.factory import create_adapter
from server.distill_team.promotion.archive import ArchiveManager
from server.distill_team.promotion.cascade import CascadeResult, CascadeUpdater
from server.distill_team.promotion.cross_project import (
    CrossProjectPromoter,
    CrossProjectResult,
)
from server.distill_team.promotion.dedup import DedupChecker, DedupResult
from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.models import (
    ArchiveEntry,
    DEFAULT_LIMITS,
    DedupVerdict,
    GraphNode,
    GraphRelation,
    GraphRelationType,
    PromotionLayer,
    PromotionState,
    PromotionStatus,
    RetestResult,
)
from server.distill_team.promotion.promote import PromotionManager, PromotionResult
from server.distill_team.promotion.retest import RetestOutcome, RetestRunner

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 编排结果
# ---------------------------------------------------------------------------


@dataclass
class PromotionOutcome:
    """单条规则的升维编排结果。"""

    rule_id: str
    state: PromotionState
    # 各阶段结果
    dedup_result: DedupResult | None = None
    retest_outcome: RetestOutcome | None = None
    promotion_result: PromotionResult | None = None
    archive_entry_id: str | None = None
    graph_node_id: str | None = None
    # 是否触发跨项目再升维
    cross_project_triggered: bool = False
    # 连锁更新结果
    cascade_result: CascadeResult | None = None
    # 最终状态
    final_status: PromotionStatus = PromotionStatus.PROMOTING
    # 错误信息（若有）
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "rule_id": self.rule_id,
            "final_status": self.final_status,
            "current_layer": self.state.current_layer,
            "dedup_count": self.state.dedup_count,
            "retest_count": self.state.retest_count,
            "promote_count": self.state.promote_count,
            "global_iteration": self.state.global_iteration,
            "archive_entry_id": self.archive_entry_id,
            "graph_node_id": self.graph_node_id,
            "cross_project_triggered": self.cross_project_triggered,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# 编排器
# ---------------------------------------------------------------------------


class PromotionOrchestrator:
    """升维管理编排器。

    编排 promotion 子包的所有子模块，实现完整的升维流程。

    用法：
        orchestrator = PromotionOrchestrator(software="trae", project_root=Path("./repo"))
        outcome = orchestrator.promote(rule_entry)
        if outcome.final_status == PromotionStatus.PROMOTED:
            print(f"规则 {outcome.rule_id} 升维完成")
    """

    # 全局迭代上限（对应经验提炼流程.md 熔断参数表）
    GLOBAL_ITERATION_MAX = DEFAULT_LIMITS.global_iteration_max  # 10

    def __init__(
        self,
        *,
        software: str | None = None,
        project_root: Path | None = None,
        adapter: CodingSoftwareAdapter | None = None,
        layout: MemoryLayout | None = None,
    ) -> None:
        """初始化编排器。

        - adapter 已提供 → 直接使用
        - adapter=None → 根据 software / project_root 自动创建
        - layout 已提供 → 直接使用
        - layout=None → 从 adapter 获取
        """
        if adapter is None:
            adapter = create_adapter(software=software, project_root=project_root)
        self._adapter = adapter

        if layout is None:
            if project_root is None:
                project_root = Path.cwd()
            layout = adapter.get_layout(project_root)
        self._layout = layout
        self._layout.ensure_dirs()

        # 初始化子模块
        self._dedup = DedupChecker(adapter)
        self._retest = RetestRunner(adapter)
        self._promote = PromotionManager(adapter)
        self._archive = ArchiveManager(layout)
        self._graph = GraphRegistry(layout)
        self._cross_project = CrossProjectPromoter(adapter, self._graph, self._archive)
        self._cascade = CascadeUpdater(self._graph)

    @property
    def layout(self) -> MemoryLayout:
        return self._layout

    @property
    def graph(self) -> GraphRegistry:
        return self._graph

    @property
    def archive(self) -> ArchiveManager:
        return self._archive

    # ------------------------------------------------------------------
    # 核心编排：完整升维流程
    # ------------------------------------------------------------------

    def promote(
        self,
        rule: RuleEntry,
        *,
        source_cases: list[str] | None = None,
        source_session: str = "",
    ) -> PromotionOutcome:
        """对单条规则执行完整升维流程。

        流程：
        1. 初始化 PromotionState
        2. 查重（dedup）
           - 完全/包含重复 → 不入库，记录触发失败案例
           - 交叉重复 → 拆分（标记需拆分）
           - 不重复 → 进入回测
        3. 回测（retest）
           - 全通过 → 进入升维
           - 不通过 → 选策略 → 修正 → 回到查重（循环）
        4. 升维（promote）
           - 判断是否需升维
           - 升维 → 抽象 → 写入目标层级 → 回到查重（循环）
           - 不升维/已达顶层 → 进入归档
        5. 归档（archive）
        6. 图谱登记（graph）
        7. 跨项目再升维检查（cross_project）
        8. 连锁更新（cascade）

        Args:
            rule: 待升维的规则条目
            source_cases: 原始错误案例列表（用于回测）
            source_session: 来源会话 ID（用于归档溯源）

        Returns:
            PromotionOutcome: 升维结果
        """
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            status=PromotionStatus.PROMOTING,
            source_case_ids=source_cases or [],
            created_at=datetime.now(),
            updated_at=datetime.now(),
        )

        outcome = PromotionOutcome(rule_id=rule.rule_id, state=state)

        try:
            current_rule = rule
            # 主循环：查重 → 回测 → 升维
            while state.status == PromotionStatus.PROMOTING:
                # 全局迭代熔断
                if state.global_iteration >= self.GLOBAL_ITERATION_MAX:
                    state.status = PromotionStatus.PENDING_CONFIRMATION
                    state.circuit_breaker_reason = (
                        f"全局迭代熔断：{state.global_iteration}次"
                    )
                    break

                # 1. 查重
                dedup_result = self._dedup.check(current_rule, state, self._layout)
                outcome.dedup_result = dedup_result

                if state.status == PromotionStatus.PENDING_CONFIRMATION:
                    outcome.error = "查重循环熔断"
                    break

                # 查重判定分支
                if dedup_result.verdict == DedupVerdict.EXACT_DUPLICATE:
                    # 完全重复：找未触发原因，记录触发失败案例
                    self._record_trigger_failure(
                        current_rule, dedup_result.existing_rule
                    )
                    state.status = PromotionStatus.ARCHIVED
                    outcome.final_status = PromotionStatus.ARCHIVED
                    break

                if dedup_result.verdict == DedupVerdict.SUBSET_DUPLICATE:
                    # 包含重复：不入库，记录触发失败案例
                    self._record_trigger_failure(
                        current_rule, dedup_result.existing_rule
                    )
                    state.status = PromotionStatus.ARCHIVED
                    outcome.final_status = PromotionStatus.ARCHIVED
                    break

                if dedup_result.verdict == DedupVerdict.CROSS_DUPLICATE:
                    # 交叉重复：拆分（简化：标记后继续作为新规则回测）
                    logger.info(
                        "规则 %s 与 %s 交叉重复，拆分后继续",
                        current_rule.rule_id,
                        dedup_result.duplicate_of,
                    )

                # 2. 回测（不重复 / 交叉重复拆分后）
                retest_outcome = self._retest.run(current_rule, state)
                outcome.retest_outcome = retest_outcome

                if state.status == PromotionStatus.PENDING_CONFIRMATION:
                    outcome.error = "回测循环熔断"
                    break

                if not retest_outcome.should_promote:
                    # 回测不通过：策略已选，修正规则后回到查重
                    # 简化：不自动修正，标记后退出（由上层处理修正）
                    logger.info(
                        "规则 %s 回测未通过（%s），策略=%s",
                        current_rule.rule_id,
                        retest_outcome.result,
                        retest_outcome.strategy,
                    )
                    # 若还有迭代空间，继续循环（上层应修正规则）
                    # 这里简化为退出，实际应用中应由调用方修正规则后重新调用
                    outcome.final_status = state.status
                    break

                # 3. 升维
                promotion_result = self._promote.promote(
                    current_rule, state, self._layout
                )
                outcome.promotion_result = promotion_result

                if state.status == PromotionStatus.NOT_TO_TOP:
                    # 升维循环熔断：停在当前层级
                    outcome.final_status = PromotionStatus.NOT_TO_TOP
                    break

                if not promotion_result.promoted:
                    # 无需升维或已达顶层 → 进入归档
                    break

                # 升维成功：更新当前规则为升维后的规则，回到查重
                current_rule = promotion_result.rule
                # 升维后需重新查重（升维后的规则可能与更高层已有规则重复）
                continue

            # 4. 归档（若未熔断且已确认无需升维）
            if state.status not in (
                PromotionStatus.PENDING_CONFIRMATION,
                PromotionStatus.NOT_TO_TOP,
            ) and not outcome.archive_entry_id:
                archive_id = self._archive_rule(
                    current_rule, state, source_session
                )
                outcome.archive_entry_id = archive_id

            # 5. 图谱登记
            if outcome.archive_entry_id or state.graph_node_id:
                node_id = self._register_graph_node(
                    current_rule, state, outcome.archive_entry_id
                )
                outcome.graph_node_id = node_id
                state.graph_node_id = node_id

            # 6. 跨项目再升维检查
            cross_result = self._cross_project.check_and_promote(self._layout)
            outcome.cross_project_triggered = cross_result.triggered

            # 7. 连锁更新
            if state.graph_node_id:
                cascade_result = self._cascade.cascade_from(
                    state.graph_node_id, change_type="modified"
                )
                outcome.cascade_result = cascade_result

            # 最终状态
            if state.status == PromotionStatus.PROMOTING:
                state.status = PromotionStatus.PROMOTED
            outcome.final_status = state.status

        except Exception as exc:
            logger.exception("规则 %s 升维失败: %s", rule.rule_id, exc)
            outcome.error = str(exc)
            state.status = PromotionStatus.PENDING_CONFIRMATION
            outcome.final_status = PromotionStatus.PENDING_CONFIRMATION

        return outcome

    # ------------------------------------------------------------------
    # 批量升维
    # ------------------------------------------------------------------

    def promote_batch(
        self,
        rules: list[RuleEntry],
        *,
        source_session: str = "",
    ) -> list[PromotionOutcome]:
        """批量升维多条规则。

        逐条执行升维，不并行（避免图谱写入冲突）。
        """
        outcomes: list[PromotionOutcome] = []
        for rule in rules:
            outcome = self.promote(rule, source_session=source_session)
            outcomes.append(outcome)
        return outcomes

    # ------------------------------------------------------------------
    # 内部：归档
    # ------------------------------------------------------------------

    def _archive_rule(
        self,
        rule: RuleEntry,
        state: PromotionState,
        source_session: str,
    ) -> str:
        """归档规则到 archive.md。"""
        entry_id = self._archive.get_next_entry_id()
        entry = ArchiveEntry(
            entry_id=entry_id,
            title=rule.title,
            promoted_to=rule.rule_id,
            promoted_at=datetime.now(),
            original_case="\n".join(state.source_case_ids) if state.source_case_ids else "",
            promotion_strategy=f"升维至 {state.current_layer}",
            source_session=source_session,
        )
        self._archive.archive_experience(entry)
        state.archive_id = entry_id
        state.archived = True
        state.archived_at = datetime.now()
        return entry_id

    # ------------------------------------------------------------------
    # 内部：图谱登记
    # ------------------------------------------------------------------

    def _register_graph_node(
        self,
        rule: RuleEntry,
        state: PromotionState,
        archive_entry_id: str | None,
    ) -> str:
        """在图谱中注册规则节点。"""
        # 生成节点 ID（如 R041）
        node_id = state.graph_node_id or self._generate_node_id()
        node = GraphNode(
            node_id=node_id,
            node_type="rule",
            name=rule.title,
            location=str(rule.file_path),
            category=rule.category,
            status="active",
        )
        self._graph.register_node(node)

        # 若有归档经验，添加 PROMOTED_FROM 关系
        if archive_entry_id:
            exp_node = GraphNode(
                node_id=archive_entry_id,
                node_type="experience",
                name=rule.title,
                location=str(self._layout.archive_path),
                status="archived",
            )
            self._graph.register_node(exp_node)
            self._graph.add_relation(
                GraphRelation(
                    source_id=node_id,
                    target_id=archive_entry_id,
                    relation_type=GraphRelationType.PROMOTED_FROM,
                    note=f"从 {archive_entry_id} 升维而来",
                )
            )

        return node_id

    def _generate_node_id(self) -> str:
        """生成下一个规则节点 ID（R001, R002, ...）。"""
        existing = self._graph.list_nodes()
        rule_nodes = [n for n in existing if n.node_id.startswith("R")]
        max_num = 0
        for n in rule_nodes:
            try:
                num = int(n.node_id[1:])
                max_num = max(max_num, num)
            except ValueError:
                continue
        return f"R{max_num + 1:03d}"

    # ------------------------------------------------------------------
    # 内部：记录触发失败案例
    # ------------------------------------------------------------------

    def _record_trigger_failure(
        self,
        new_rule: RuleEntry,
        existing_rule: RuleEntry | None,
    ) -> None:
        """记录触发失败案例到 archive.md。"""
        if existing_rule is None:
            return
        from server.distill_team.promotion.models import TriggerFailureCase

        case_id = self._archive.get_next_failure_case_id()
        case = TriggerFailureCase(
            case_id=case_id,
            rule_id=existing_rule.rule_id,
            reason=f"新规则 {new_rule.rule_id} 与已有规则 {existing_rule.rule_id} 重复，已有规则未触发",
            occurred_at=datetime.now(),
            fix_action=f"检查 {existing_rule.rule_id} 为何未触发",
        )
        self._archive.record_trigger_failure(case)


__all__ = ["PromotionOrchestrator", "PromotionOutcome"]
