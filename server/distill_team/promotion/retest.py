"""回测验证模块（对应经验提炼流程.md 步骤4）。

职责：
- 对提炼后的规则用原始错误案例进行回测验证（只验证当前提炼轮次的原始案例，不做全量历史回测）
- 根据回测结果选择修正策略（策略判断决策树）
- 应用策略修正规则（ADD_CONSTRAINT / SPLIT_RULE / CHANGE_ANGLE / RESTART）
- 回测循环熔断检查（普通层级 4 次 / 顶层 8 次）

对外契约：
- retest_rule(rule, cases) → RetestResult：单次回测
- select_strategy(result, pass_count, total_count, *, was_merged) → RetestStrategy：策略决策树
- apply_strategy(rule, strategy, cases) → RuleEntry：应用策略修正规则
- RetestRunner(adapter).run(rule, state) → RetestOutcome：回测流程编排

注：source_case_ids 在当前实现中直接作为案例文本用于回测。
若上层将案例 ID 存入 source_case_ids，应在调用 run() 前解析为案例文本，
或扩展 CodingSoftwareAdapter 提供案例拉取能力。
"""

from __future__ import annotations

import difflib
import logging
import re
from collections import Counter
from dataclasses import dataclass

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    RuleEntry,
)
from server.distill_team.promotion.models import (
    PromotionState,
    PromotionStatus,
    RetestResult,
    RetestStrategy,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 参数
# ---------------------------------------------------------------------------

# 相似度阈值：规则内容与案例相似度 >= 此值视为该案例通过
SIMILARITY_THRESHOLD = 0.5

# ADD_CONSTRAINT 策略：补充约束时提取的关键词数量上限
ADD_CONSTRAINT_KEYWORD_LIMIT = 5

# 通过率阈值：用于区分 ADD_CONSTRAINT 和 CHANGE_ANGLE
# 通过率 >= 此值 → 规则覆盖了核心场景但遗漏边界条件（ADD_CONSTRAINT）
# 通过率 < 此值 → 规则能覆盖场景但抽象层级不对（CHANGE_ANGLE）
ADD_CONSTRAINT_PASS_RATIO = 0.5

# 停用词表（用于关键词提取，过滤无意义的高频词）
_STOP_WORDS = frozenset({
    # 中文停用词
    "的", "了", "在", "是", "和", "与", "或", "及", "也", "都",
    "不", "要", "会", "能", "可", "以", "对", "为", "由", "从",
    "这", "那", "其", "之", "于", "等", "被", "把", "让", "使",
    # 英文停用词
    "the", "a", "an", "is", "are", "was", "were", "be", "been",
    "and", "or", "not", "no", "for", "with", "in", "on", "at",
    "to", "of", "as", "by", "this", "that", "it", "from", "if",
    "then", "else", "when", "while", "do", "did", "has", "have",
})


# ---------------------------------------------------------------------------
# RetestOutcome：回测结果数据类
# ---------------------------------------------------------------------------


@dataclass
class RetestOutcome:
    """回测结果。

    - result：回测结果（ALL_PASS / PARTIAL_PASS / ALL_FAIL）
    - strategy：不通过时的修正策略；全通过时为 None
    - pass_count：通过的案例数
    - total_count：案例总数
    - should_promote：是否可进入升维（全通过 → True）
    - state：更新后的升维状态
    """

    result: RetestResult
    strategy: RetestStrategy | None
    pass_count: int
    total_count: int
    should_promote: bool
    state: PromotionState


# ---------------------------------------------------------------------------
# 内部辅助函数
# ---------------------------------------------------------------------------


def _tokenize(text: str) -> list[str]:
    """简单分词：提取中英文词组。

    中文按连续字符序列切分，英文按标识符切分。
    简化实现，不依赖外部分词库。
    """
    return re.findall(r"[\u4e00-\u9fff]+|[a-zA-Z_][a-zA-Z0-9_]*", text)


def _case_passes(rule_text: str, case_text: str) -> bool:
    """判断单个案例是否被规则覆盖。

    双重判定（任一通过即视为覆盖）：
    1. 关键词匹配：案例中的关键词在规则中出现的比例 >= 阈值
    2. difflib 相似度：规则内容与案例的序列相似度 >= 阈值
    """
    # 1. 关键词匹配
    case_keywords = [t for t in _tokenize(case_text) if t not in _STOP_WORDS]
    if case_keywords:
        hit = sum(1 for kw in case_keywords if kw in rule_text)
        keyword_ratio = hit / len(case_keywords)
        if keyword_ratio >= SIMILARITY_THRESHOLD:
            return True

    # 2. difflib 相似度
    ratio = difflib.SequenceMatcher(None, rule_text, case_text).ratio()
    return ratio >= SIMILARITY_THRESHOLD


def _count_passes(rule: RuleEntry, cases: list[str]) -> int:
    """统计规则对案例的通过数。"""
    rule_text = (rule.title + "\n" + rule.content).lower()
    pass_count = 0
    for case in cases:
        if _case_passes(rule_text, case.lower()):
            pass_count += 1
    return pass_count


def _find_failed_cases(rule: RuleEntry, cases: list[str]) -> list[str]:
    """找出回测失败的案例。"""
    rule_text = (rule.title + "\n" + rule.content).lower()
    failed: list[str] = []
    for case in cases:
        if not _case_passes(rule_text, case.lower()):
            failed.append(case)
    return failed


def _extract_keywords(cases: list[str], *, top_n: int = ADD_CONSTRAINT_KEYWORD_LIMIT) -> list[str]:
    """从案例中提取关键词（按词频降序）。

    用于 ADD_CONSTRAINT 策略：将失败案例中的关键信息补充到规则。
    """
    if not cases:
        return []
    words: list[str] = []
    for case in cases:
        for token in _tokenize(case):
            # 过滤停用词和长度 <= 1 的词（单字中文词通常无意义）
            if token in _STOP_WORDS or len(token) <= 1:
                continue
            words.append(token)
    if not words:
        return []
    counter = Counter(words)
    return [w for w, _ in counter.most_common(top_n)]


# ---------------------------------------------------------------------------
# 公开函数
# ---------------------------------------------------------------------------


def retest_rule(rule: RuleEntry, cases: list[str]) -> RetestResult:
    """对规则用案例进行回测。

    简化实现：用关键词匹配 + difflib 相似度。
    - 每个案例相似度 >= SIMILARITY_THRESHOLD → 该案例通过
    - 全部通过 → ALL_PASS
    - 部分通过 → PARTIAL_PASS
    - 全不通过 → ALL_FAIL

    案例为空时视为 ALL_PASS（无案例需要回测）。
    """
    if not cases:
        return RetestResult.ALL_PASS

    total = len(cases)
    pass_count = _count_passes(rule, cases)

    if pass_count == total:
        return RetestResult.ALL_PASS
    if pass_count == 0:
        return RetestResult.ALL_FAIL
    return RetestResult.PARTIAL_PASS


def select_strategy(
    result: RetestResult,
    pass_count: int,
    total_count: int,
    *,
    was_merged: bool = False,
) -> RetestStrategy:
    """根据回测结果选择修正策略（策略判断决策树）。

    决策树：
    - ALL_PASS → 不应调用（抛出 ValueError）
    - pass_count == 0 → RESTART（推翻重来）
    - pass_count > 0 但 < total：
        - was_merged=True → SPLIT_RULE（合并导致的问题，拆分）
        - was_merged=False：
            - 通过率 >= ADD_CONSTRAINT_PASS_RATIO → ADD_CONSTRAINT（遗漏边界条件）
            - 通过率 < ADD_CONSTRAINT_PASS_RATIO → CHANGE_ANGLE（抽象层级不对）
    """
    if result == RetestResult.ALL_PASS:
        raise ValueError("ALL_PASS 无需选择修正策略")

    # 通过案例数为 0 → 推翻重来
    if pass_count == 0:
        return RetestStrategy.RESTART

    # 部分通过：合并导致 → 拆分
    if was_merged:
        return RetestStrategy.SPLIT_RULE

    # 部分通过：非合并导致，按通过率区分
    pass_ratio = pass_count / total_count if total_count > 0 else 0.0
    if pass_ratio >= ADD_CONSTRAINT_PASS_RATIO:
        # 通过率较高：覆盖了核心场景但遗漏边界条件
        return RetestStrategy.ADD_CONSTRAINT
    # 通过率较低：能覆盖场景但抽象层级不对
    return RetestStrategy.CHANGE_ANGLE


def apply_strategy(
    rule: RuleEntry,
    strategy: RetestStrategy,
    cases: list[str],
) -> RuleEntry:
    """应用策略修正规则。

    - ADD_CONSTRAINT：从失败案例中提取关键词，补充到规则内容
    - SPLIT_RULE：返回原规则（标记需要拆分，由上层处理拆分逻辑）
    - CHANGE_ANGLE：返回原规则（标记需要换角度，由上层处理）
    - RESTART：返回空规则（content=""，由上层重新提炼）
    """
    if strategy == RetestStrategy.ADD_CONSTRAINT:
        # 从失败案例中提取关键词，补充到规则内容
        failed_cases = _find_failed_cases(rule, cases)
        keywords = _extract_keywords(failed_cases)
        if not keywords:
            return rule
        addition = (
            "\n\n## 补充约束（来自回测失败案例）\n- "
            + "\n- ".join(keywords)
        )
        return RuleEntry(
            rule_id=rule.rule_id,
            title=rule.title,
            content=rule.content + addition,
            file_path=rule.file_path,
            category=rule.category,
            frontmatter=dict(rule.frontmatter),
        )

    if strategy == RetestStrategy.SPLIT_RULE:
        # 标记需要拆分：返回原规则，由上层处理拆分逻辑
        return rule

    if strategy == RetestStrategy.CHANGE_ANGLE:
        # 标记需要换角度：返回原规则，由上层处理
        return rule

    if strategy == RetestStrategy.RESTART:
        # 推翻重来：返回空规则，由上层重新提炼
        return RuleEntry(
            rule_id=rule.rule_id,
            title=rule.title,
            content="",
            file_path=rule.file_path,
            category=rule.category,
            frontmatter=dict(rule.frontmatter),
        )

    # 理论不可达：未知策略返回原规则
    return rule


# ---------------------------------------------------------------------------
# RetestRunner：回测流程编排
# ---------------------------------------------------------------------------


class RetestRunner:
    """回测流程编排器。

    用法：
        runner = RetestRunner(adapter)
        outcome = runner.run(rule, state)
        if outcome.should_promote:
            # 进入升维
            ...
        else:
            # 根据 outcome.strategy 调用 apply_strategy 修正规则
            fixed_rule = apply_strategy(rule, outcome.strategy, cases)
            # 修正后重新回测（进入下一轮循环）
    """

    def __init__(self, adapter: CodingSoftwareAdapter) -> None:
        self._adapter = adapter

    def run(self, rule: RuleEntry, state: PromotionState) -> RetestOutcome:
        """执行回测流程。

        1. 用 state.source_case_ids 拉取原始案例
        2. 回测
        3. 全通过 → 返回 ALL_PASS，should_promote=True，进入升维
        4. 不通过 → 选策略 → increment_retest → 检查熔断
        """
        # 1. 拉取原始案例（当前实现：source_case_ids 直接作为案例文本）
        cases = list(state.source_case_ids)
        total_count = len(cases)

        # 2. 回测（直接用 _count_passes 避免与 retest_rule 重复计算）
        pass_count = _count_passes(rule, cases)
        if pass_count == total_count:
            result = RetestResult.ALL_PASS
        elif pass_count == 0:
            result = RetestResult.ALL_FAIL
        else:
            result = RetestResult.PARTIAL_PASS

        state.last_retest_result = result

        # 3. 全通过 → 进入升维
        if result == RetestResult.ALL_PASS:
            return RetestOutcome(
                result=result,
                strategy=None,
                pass_count=pass_count,
                total_count=total_count,
                should_promote=True,
                state=state,
            )

        # 4. 不通过 → 选策略 → increment_retest → 检查熔断
        # was_merged：当前 PromotionState 无显式合并标志，默认 False
        # 上层若知道规则来自合并，可通过扩展 state 或在调用前标记
        strategy = select_strategy(
            result,
            pass_count,
            total_count,
            was_merged=False,
        )
        state.last_strategy = strategy

        # 累加回测计数
        state.increment_retest()

        # 熔断检查
        if state.retest_count >= state.retest_limit:
            state.status = PromotionStatus.PENDING_CONFIRMATION
            reason = f"回测循环熔断：{state.retest_count}次"
            state.circuit_breaker_reason = reason
            logger.warning(
                "回测熔断：rule_id=%s retest_count=%d limit=%d",
                rule.rule_id,
                state.retest_count,
                state.retest_limit,
            )
            return RetestOutcome(
                result=result,
                strategy=strategy,
                pass_count=pass_count,
                total_count=total_count,
                should_promote=False,
                state=state,
            )

        return RetestOutcome(
            result=result,
            strategy=strategy,
            pass_count=pass_count,
            total_count=total_count,
            should_promote=False,
            state=state,
        )


__all__ = [
    "RetestOutcome",
    "RetestRunner",
    "apply_strategy",
    "retest_rule",
    "select_strategy",
]
