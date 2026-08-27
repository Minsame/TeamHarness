"""升维循环模块。

对应 resource-harness 规则的「入库升维」环节（经验提炼流程.md 步骤5）。
实现升维检验清单、升维循环、升维抽象（泛化）、熔断检查。

升维流程（经验提炼流程.md）：

    入库升维
      ├─ 判断：是否需升维？
      │    ├─(是)→ 升维抽象 → 回测验证（升维后重新查重）
      │    │              升维计数+1
      │    │                ├─(<6次)→ 回到「入库查重」
      │    │                └─(≥6次)→ 停在当前层级，标记「暂未到顶」
      │    └─(否 / 已达顶层)→ 进入归档溯源

三层结构对应 PromotionLayer：
    第1层 PROJECT       → 项目级规则（.trae/rules 等）
    第2层 RULES_FILE    → 规则文件完整版（~/.xxx/rules/xxx-rules.md）
    第3层 GLOBAL_TOP    → 用户全局顶层（user_profile.md 热点区，不再升维）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.models import (
    DEFAULT_LIMITS,
    PromotionLayer,
    PromotionState,
    PromotionStatus,
)


# ---------------------------------------------------------------------------
# 升维检验清单
# ---------------------------------------------------------------------------

PROMOTION_CHECKLIST: list[str] = [
    "这条规则是绑定特定场景，还是通用操作？",
    "已有 skill 能承载吗？如果能，下沉到 skill",
    "不能下沉的，是工具层通用经验吗？如果是，放到对应规则文件",
    "放到 user_profile.md 顶层？检查'顶层规则防止过分细化'铁律",
    "规则归属的文件类别是否清晰无重叠？",
    "路由表的触发条件描述是否准确覆盖规则适用范围？",
]


# ---------------------------------------------------------------------------
# 项目特定标识识别（用于判断规则是否绑定特定场景）
# ---------------------------------------------------------------------------

# 简化实现：通过正则匹配项目特定的路径、模块名、文件名等标识
_PROJECT_SPECIFIC_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"[A-Za-z]:\\[^\s]+"),  # Windows 绝对路径（如 d:\Code\...）
    re.compile(r"/home/[^\s]+"),  # Linux 绝对路径
    re.compile(r"/Users/[^\s]+"),  # macOS 绝对路径
    re.compile(r"\.trae"),  # Trae 软件目录（.trae / .trae-cn）
    re.compile(r"\.cursor"),  # Cursor 软件目录
    re.compile(r"\.claude"),  # Claude Code 软件目录
    re.compile(r"\.windsurf"),  # Windsurf 软件目录
    re.compile(r"\bserver\.distill_team\.[a-z_.]+"),  # 本项目模块路径
    re.compile(r"\bdistill_team\.[a-z_.]+"),
    re.compile(r"\b\w+\.py\b"),  # 具体 Python 文件名
]


# ---------------------------------------------------------------------------
# 数据类
# ---------------------------------------------------------------------------


@dataclass
class PromotionDecision:
    """升维检验结果（check_promotion_eligibility 返回）。"""

    should_promote: bool
    target_layer: PromotionLayer
    reason: str
    checklist_results: list[tuple[str, bool]] = field(default_factory=list)


@dataclass
class PromotionResult:
    """升维执行结果（PromotionManager.promote 返回）。"""

    promoted: bool
    target_layer: PromotionLayer
    target_path: Path | None  # 写入的文件路径（未写入时为 None）
    rule: RuleEntry  # 升维后的规则（可能被泛化）
    state: PromotionState  # 更新后的状态
    reason: str


# ---------------------------------------------------------------------------
# 升维检验
# ---------------------------------------------------------------------------


def _is_project_specific(content: str) -> bool:
    """判断规则内容是否包含项目特定标识。"""
    for pattern in _PROJECT_SPECIFIC_PATTERNS:
        if pattern.search(content):
            return True
    return False


def _is_top_layer_eligible(rule: RuleEntry) -> bool:
    """判断规则是否适合升维到顶层（足够通用且高频）。

    简化判断：
    - 不包含项目特定标识（通用）
    - 内容简短（核心原则型，避免顶层规则过分细化）
    """
    if _is_project_specific(rule.content):
        return False
    # 顶层规则应简短精炼，避免过分细化（铁律）
    # 简化：内容长度 ≤ 200 字符视为核心原则型
    return len(rule.content) <= 200


def _run_checklist(rule: RuleEntry) -> list[tuple[str, bool]]:
    """执行升维检验清单，返回 (检验项, 是否通过) 列表。"""
    is_specific = _is_project_specific(rule.content)
    top_eligible = _is_top_layer_eligible(rule)

    return [
        # 1. 绑定特定场景 vs 通用操作（通用 → True）
        (PROMOTION_CHECKLIST[0], not is_specific),
        # 2. 已有 skill 能承载吗（简化：未下沉到 skill → False）
        (PROMOTION_CHECKLIST[1], False),
        # 3. 不能下沉的，是工具层通用经验吗（通用 → True）
        (PROMOTION_CHECKLIST[2], not is_specific),
        # 4. 放到顶层？检查铁律（适合顶层 → True）
        (PROMOTION_CHECKLIST[3], top_eligible),
        # 5. 规则归属的文件类别是否清晰无重叠（有 category → True）
        (PROMOTION_CHECKLIST[4], rule.category is not None),
        # 6. 路由表触发条件是否准确覆盖（简化：True）
        (PROMOTION_CHECKLIST[5], True),
    ]


def check_promotion_eligibility(
    rule: RuleEntry,
    state: PromotionState,
) -> PromotionDecision:
    """升维检验：判断规则是否需要升维。

    - 已达顶层（is_top_layer）→ 无需升维
    - 规则内容特定于项目 → 留在项目级
    - 规则内容通用 → 可升维到规则文件级
    - 规则内容足够通用且高频 → 可升维到顶层

    Args:
        rule: 待检验的规则条目
        state: 当前升维状态

    Returns:
        PromotionDecision: 检验结果
    """
    # 已达顶层，无需升维
    if state.is_top_layer:
        return PromotionDecision(
            should_promote=False,
            target_layer=PromotionLayer.GLOBAL_TOP,
            reason="已达顶层（GLOBAL_TOP），无需升维",
            checklist_results=[],
        )

    # 执行检验清单
    checklist_results = _run_checklist(rule)
    is_specific = _is_project_specific(rule.content)

    # 当前在项目级：判断是否可升维到规则文件级
    if state.current_layer == PromotionLayer.PROJECT:
        if is_specific:
            return PromotionDecision(
                should_promote=False,
                target_layer=PromotionLayer.PROJECT,
                reason="规则绑定特定项目场景，留在项目级",
                checklist_results=checklist_results,
            )
        return PromotionDecision(
            should_promote=True,
            target_layer=PromotionLayer.RULES_FILE,
            reason="规则内容通用，升维到规则文件级",
            checklist_results=checklist_results,
        )

    # 当前在规则文件级：判断是否可升维到顶层
    if _is_top_layer_eligible(rule):
        return PromotionDecision(
            should_promote=True,
            target_layer=PromotionLayer.GLOBAL_TOP,
            reason="规则足够通用且高频，升维到顶层",
            checklist_results=checklist_results,
        )
    return PromotionDecision(
        should_promote=False,
        target_layer=PromotionLayer.RULES_FILE,
        reason="规则未达顶层标准，留在规则文件级",
        checklist_results=checklist_results,
    )


# ---------------------------------------------------------------------------
# 升维抽象（泛化）
# ---------------------------------------------------------------------------


def _generalize_content(content: str) -> str:
    """泛化规则内容：去除项目特定标识，保留通用规则。

    将匹配到的项目特定路径/模块名/文件名替换为占位符 `...`。
    """
    generalized = content
    for pattern in _PROJECT_SPECIFIC_PATTERNS:
        generalized = pattern.sub("...", generalized)
    return generalized


def _abstract_to_principle(content: str) -> str:
    """抽象为高层原则：提取核心规则（第一段）。

    顶层规则应简短精炼，取第一段作为核心原则。
    """
    lines = content.strip().splitlines()
    principle_lines: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped:
            # 遇到空行，若已收集到内容则结束
            if principle_lines:
                break
            continue
        principle_lines.append(stripped)
    if principle_lines:
        return "\n".join(principle_lines)
    return content


def abstract_rule(
    rule: RuleEntry,
    target_layer: PromotionLayer,
) -> RuleEntry:
    """升维抽象（泛化）。

    - 项目级 → 规则文件级：去除项目特定路径/模块名，保留通用规则
    - 规则文件级 → 顶层：进一步抽象为高层原则
    - 同层或目标为项目级：原样返回

    Args:
        rule: 原始规则条目
        target_layer: 目标层级

    Returns:
        泛化后的 RuleEntry（新实例，不改原对象）
    """
    content = rule.content

    if target_layer == PromotionLayer.RULES_FILE:
        # 项目级 → 规则文件级：去除项目特定标识
        content = _generalize_content(content)
    elif target_layer == PromotionLayer.GLOBAL_TOP:
        # 规则文件级 → 顶层：抽象为高层原则
        content = _abstract_to_principle(content)

    return RuleEntry(
        rule_id=rule.rule_id,
        title=rule.title,
        content=content,
        file_path=rule.file_path,
        category=rule.category,
        frontmatter=rule.frontmatter,
    )


# ---------------------------------------------------------------------------
# 升维管理器
# ---------------------------------------------------------------------------


class PromotionManager:
    """升维管理器：执行升维循环。

    通过注入的 CodingSoftwareAdapter 写入规则到对应层级目录，
    不硬编码任何 coding 软件的路径。
    """

    def __init__(self, adapter: CodingSoftwareAdapter):
        self._adapter = adapter

    def _write_to_layer(
        self,
        rule: RuleEntry,
        target_layer: PromotionLayer,
        layout: MemoryLayout,
    ) -> Path:
        """写入规则到目标层级，返回写入的文件路径。

        - 第1层 PROJECT：写入 project_rules_dir
        - 第2层 RULES_FILE：写入 global_rules_dir
        - 第3层 GLOBAL_TOP：写入 user_profile_path 热点规则区
        """
        if target_layer == PromotionLayer.GLOBAL_TOP:
            # 顶层写入热点规则区
            self._adapter.write_hotspot_rule(
                user_profile_path=layout.user_profile_path,
                rule_id=rule.rule_id,
                title=rule.title,
                content=rule.content,
            )
            return layout.user_profile_path

        # 第1层 / 第2层：写入对应规则目录
        rules_dir = (
            layout.project_rules_dir
            if target_layer == PromotionLayer.PROJECT
            else layout.global_rules_dir
        )
        return self._adapter.write_rule(
            rules_dir=rules_dir,
            rule_id=rule.rule_id,
            title=rule.title,
            content=rule.content,
            frontmatter=rule.frontmatter or None,
        )

    def promote(
        self,
        rule: RuleEntry,
        state: PromotionState,
        layout: MemoryLayout,
    ) -> PromotionResult:
        """执行升维。

        流程：
        1. 检查是否已达顶层 → 标记 PROMOTED，返回
        2. 执行升维检验清单
        3. 判断是否需升维
        4. 若升维：
           - 升维抽象（泛化规则内容）
           - 写入目标层级
           - increment_promote（升维计数+1）
           - 检查熔断（promote_count >= 6 → NOT_TO_TOP）
        5. 若不升维/已达顶层 → 标记 PROMOTED，进入归档

        Args:
            rule: 待升维的规则条目
            state: 当前升维状态（会被原地更新）
            layout: 记忆路径布局

        Returns:
            PromotionResult: 升维结果
        """
        # 1. 已达顶层，无需升维
        if state.is_top_layer:
            state.status = PromotionStatus.PROMOTED
            return PromotionResult(
                promoted=False,
                target_layer=state.current_layer,
                target_path=None,
                rule=rule,
                state=state,
                reason="已达顶层（GLOBAL_TOP），无需升维，进入归档",
            )

        # 2. 升维检验
        decision = check_promotion_eligibility(rule, state)

        # 3. 不需升维 → 标记 PROMOTED
        if not decision.should_promote:
            state.status = PromotionStatus.PROMOTED
            return PromotionResult(
                promoted=False,
                target_layer=state.current_layer,
                target_path=None,
                rule=rule,
                state=state,
                reason=decision.reason,
            )

        # 4. 执行升维
        # 4a. 升维抽象（泛化）
        abstracted_rule = abstract_rule(rule, decision.target_layer)

        # 4b. 写入目标层级
        target_path = self._write_to_layer(
            abstracted_rule, decision.target_layer, layout
        )

        # 4c. 升维计数+1，更新当前层级
        state.increment_promote()
        state.current_layer = decision.target_layer

        # 4d. 检查熔断（promote_count >= 6 → NOT_TO_TOP）
        if state.promote_count >= DEFAULT_LIMITS.promote_max:
            state.status = PromotionStatus.NOT_TO_TOP
            state.circuit_breaker_reason = (
                f"升维循环熔断：{DEFAULT_LIMITS.promote_max}次，停在当前层级"
            )
            reason = (
                f"升维至 {decision.target_layer}，但触发熔断"
                f"（{state.promote_count}/{DEFAULT_LIMITS.promote_max}），"
                f"停在当前层级"
            )
        elif state.is_top_layer:
            # 升维后已达顶层
            state.status = PromotionStatus.PROMOTED
            reason = f"升维至顶层（{decision.target_layer}），升维完成"
        else:
            # 未达顶层，可继续升维
            state.status = PromotionStatus.PROMOTING
            reason = f"升维至 {decision.target_layer}，可继续升维"

        # 更新泛化后规则的文件路径为实际写入路径
        abstracted_rule.file_path = target_path

        return PromotionResult(
            promoted=True,
            target_layer=decision.target_layer,
            target_path=target_path,
            rule=abstracted_rule,
            state=state,
            reason=reason,
        )


__all__ = [
    "PROMOTION_CHECKLIST",
    "PromotionDecision",
    "PromotionManager",
    "PromotionResult",
    "abstract_rule",
    "check_promotion_eligibility",
]
