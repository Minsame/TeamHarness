"""升维管理模块数据模型。

扩展 distill_team.models 中的 DistilledPrompt，增加升维状态追踪字段。
对应 resource-harness 规则的三层结构 + 熔断参数。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


# ---------------------------------------------------------------------------
# 枚举
# ---------------------------------------------------------------------------


class PromotionLayer(str):
    """规则层级（对应 resource-harness 三层结构）。"""

    PROJECT = "project"  # 第1层：项目级规则（.trae/rules / .cursor/rules 等）
    RULES_FILE = "rules_file"  # 第2层：规则文件完整版（~/.xxx/rules/xxx-rules.md）
    GLOBAL_TOP = "global_top"  # 第3层：用户全局顶层（user_profile.md 热点区，不再升维）


class PromotionStatus(str):
    """升维状态。"""

    PROMOTING = "promoting"  # 升维进行中
    PROMOTED = "promoted"  # 升维完成（到达顶层或确认无需升维）
    PENDING_CONFIRMATION = "pending_confirmation"  # 熔断，待人工确认
    NOT_TO_TOP = "not_to_top"  # 升维循环6次后停在当前层级
    ARCHIVED = "archived"  # 已归档


class DedupVerdict(str):
    """查重判定结果（四种标准）。"""

    EXACT_DUPLICATE = "exact_duplicate"  # 完全重复
    SUBSET_DUPLICATE = "subset_duplicate"  # 包含重复
    CROSS_DUPLICATE = "cross_duplicate"  # 交叉重复
    NO_DUPLICATE = "no_duplicate"  # 不重复


class RetestResult(str):
    """回测结果。"""

    ALL_PASS = "all_pass"  # 全部通过
    PARTIAL_PASS = "partial_pass"  # 部分通过
    ALL_FAIL = "all_fail"  # 全不通过


class RetestStrategy(str):
    """回测策略（策略判断决策树）。"""

    ADD_CONSTRAINT = "add_constraint"  # 补回关键约束
    SPLIT_RULE = "split_rule"  # 拆分独立规则
    CHANGE_ANGLE = "change_angle"  # 换抽象角度
    RESTART = "restart"  # 推翻重来


class GraphRelationType(str):
    """图谱关系类型（6 种）。"""

    PROMOTED_FROM = "promoted_from"  # 升维自：全局规则 → 归档区经验
    COVERS = "covers"  # 覆盖：全局规则 → 项目级规则
    REFERENCES = "references"  # 引用：热点规则 → 完整版规则
    DEPENDS_ON = "depends_on"  # 依赖：规则A → 规则B（可能成环）
    SAME_SOURCE = "same_source"  # 同源：经验A ↔ 经验B（可能成环）
    REGISTERED_IN = "registered_in"  # 登记于：资源 → 索引


# ---------------------------------------------------------------------------
# 熔断参数（对应 resource-harness 规则硬编码）
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CircuitBreakerLimits:
    """熔断参数表（对应经验提炼流程.md 熔断参数表）。"""

    dedup_max: int = 6  # 查重循环熔断
    retest_normal_max: int = 4  # 回测循环（普通层级）熔断
    retest_top_max: int = 8  # 回测循环（顶层）熔断
    promote_max: int = 6  # 升维循环熔断（停在当前层级）
    global_iteration_max: int = 10  # 全局迭代上限
    meta_rule_violation_max: int = 3  # 元规则未遵守熔断


DEFAULT_LIMITS = CircuitBreakerLimits()


# ---------------------------------------------------------------------------
# 升维状态
# ---------------------------------------------------------------------------


@dataclass
class PromotionState:
    """追踪某条 DistilledPrompt 的升维全过程。

    对应经验提炼流程.md 的完整流程：
    查重 → 回测 → 升维 → 归档 → 图谱登记
    """

    # 当前层级
    current_layer: PromotionLayer = PromotionLayer.PROJECT
    # 当前状态
    status: PromotionStatus = PromotionStatus.PROMOTING

    # 计数器（统一计数器，策略间切换不重置）
    dedup_count: int = 0  # 查重循环计数
    retest_count: int = 0  # 回测循环计数
    promote_count: int = 0  # 升维循环计数
    global_iteration: int = 0  # 全局迭代计数

    # 查重结果
    last_dedup_verdict: DedupVerdict | None = None
    # 回测结果
    last_retest_result: RetestResult | None = None
    # 最近使用的回测策略
    last_strategy: RetestStrategy | None = None

    # 原始错误案例（用于回测，只验证当前提炼轮次的原始案例）
    source_case_ids: list[str] = field(default_factory=list)

    # 归档信息
    archived: bool = False
    archive_id: str | None = None  # archive.md 中的经验 ID（如 E001）
    archived_at: datetime | None = None

    # 图谱节点 ID（如 R041）
    graph_node_id: str | None = None
    # 图谱关系
    links: dict[str, list[str]] = field(default_factory=dict)

    # 熔断原因（若触发熔断）
    circuit_breaker_reason: str | None = None

    # 升维时间线
    created_at: datetime | None = None
    updated_at: datetime | None = None

    def increment_dedup(self) -> None:
        self.dedup_count += 1
        self.global_iteration += 1
        self.updated_at = datetime.now()

    def increment_retest(self) -> None:
        self.retest_count += 1
        self.global_iteration += 1
        self.updated_at = datetime.now()

    def increment_promote(self) -> None:
        self.promote_count += 1
        self.global_iteration += 1
        self.updated_at = datetime.now()

    @property
    def is_top_layer(self) -> bool:
        """是否已达顶层（第3层）。"""
        return self.current_layer == PromotionLayer.GLOBAL_TOP

    @property
    def retest_limit(self) -> int:
        """当前层级的回测熔断上限。"""
        if self.is_top_layer:
            return DEFAULT_LIMITS.retest_top_max
        return DEFAULT_LIMITS.retest_normal_max

    def to_dict(self) -> dict[str, Any]:
        return {
            "current_layer": self.current_layer,
            "status": self.status,
            "dedup_count": self.dedup_count,
            "retest_count": self.retest_count,
            "promote_count": self.promote_count,
            "global_iteration": self.global_iteration,
            "last_dedup_verdict": self.last_dedup_verdict,
            "last_retest_result": self.last_retest_result,
            "last_strategy": self.last_strategy,
            "source_case_ids": self.source_case_ids,
            "archived": self.archived,
            "archive_id": self.archive_id,
            "archived_at": self.archived_at.isoformat() if self.archived_at else None,
            "graph_node_id": self.graph_node_id,
            "links": self.links,
            "circuit_breaker_reason": self.circuit_breaker_reason,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }


# ---------------------------------------------------------------------------
# 图谱节点与关系
# ---------------------------------------------------------------------------


@dataclass
class GraphNode:
    """图谱节点。"""

    node_id: str  # 如 R041 / E001 / ark-image-generation
    node_type: str  # rule / experience / resource
    name: str
    location: str  # 文件路径
    category: str | None = None
    status: str = "active"  # active / archived / pending_confirmation


@dataclass
class GraphRelation:
    """图谱关系。"""

    source_id: str
    target_id: str
    relation_type: GraphRelationType
    note: str = ""


# ---------------------------------------------------------------------------
# 归档区经验条目
# ---------------------------------------------------------------------------


@dataclass
class ArchiveEntry:
    """归档区经验条目（archive.md）。"""

    entry_id: str  # 如 E001
    title: str
    promoted_to: str  # 升维至的规则 ID（如 R005）
    promoted_at: datetime
    original_case: str  # 原始错误案例完整记录
    promotion_strategy: str  # 升维策略
    source_session: str  # 来源会话 ID


@dataclass
class TriggerFailureCase:
    """触发失败案例（archive.md 中单独段落）。"""

    case_id: str  # 如 TF-001
    rule_id: str  # 对应规则
    reason: str  # 未触发原因
    occurred_at: datetime
    fix_action: str  # 修补动作


__all__ = [
    "ArchiveEntry",
    "CircuitBreakerLimits",
    "DEFAULT_LIMITS",
    "DedupVerdict",
    "GraphNode",
    "GraphRelation",
    "GraphRelationType",
    "PromotionLayer",
    "PromotionState",
    "PromotionStatus",
    "RetestResult",
    "RetestStrategy",
    "TriggerFailureCase",
]
