"""入库查重模块。

对应 resource-harness 规则的「入库查重」环节（经验提炼流程.md 步骤3）。
按操作类型分桶定位已有规则，判定四种重复类型，并管理查重熔断。

查重判定标准（对应经验提炼流程.md）：
- 完全重复（EXACT_DUPLICATE）：规则内容和适用范围一致 → 走「找未触发原因」流程
- 包含重复（SUBSET_DUPLICATE）：新经验是已有规则的子集 → 不入库，检查旧规则为何未触发
- 交叉重复（CROSS_DUPLICATE）：适用范围有交集但不包含 → 拆分新经验
- 不重复（NO_DUPLICATE）：无交集 → 进入回测验证

相似度计算用简单方法：
- 内容相似度：difflib.SequenceMatcher 计算 ratio
- 适用范围相似度：比较 category 字段
- 阈值：ratio >= 0.9 → 完全重复；0.6 <= ratio < 0.9 → 交叉重复；ratio < 0.6 → 不重复
- 包含重复：新经验的所有关键词都在已有规则中
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.models import (
    DEFAULT_LIMITS,
    DedupVerdict,
    PromotionState,
    PromotionStatus,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 相似度阈值
# ---------------------------------------------------------------------------

# 内容相似度 >= 此阈值 → 完全重复（内容一致）
EXACT_DUPLICATE_THRESHOLD = 0.9
# 内容相似度 >= 此阈值且 < 完全阈值 → 交叉重复（有交集）
CROSS_DUPLICATE_THRESHOLD = 0.6

# 关键词提取的最小长度（过滤单字符噪声）
_KEYWORD_MIN_LENGTH = 2

# 查重判定 → 后续动作映射（对应经验提炼流程.md 处理列）
_VERDICT_ACTION_MAP: dict[str, str] = {
    DedupVerdict.EXACT_DUPLICATE: "find_untriggered_reason",
    DedupVerdict.SUBSET_DUPLICATE: "skip",
    DedupVerdict.CROSS_DUPLICATE: "split",
    DedupVerdict.NO_DUPLICATE: "no_duplicate",
}


# ---------------------------------------------------------------------------
# 分桶查重
# ---------------------------------------------------------------------------


def bucket_lookup(category: str | None, layout: MemoryLayout) -> list[Path]:
    """按操作类型分桶定位查重范围。

    对应经验提炼流程.md「分桶查重」：先按操作类型定位到对应类别的规则文件，
    只在该桶内查重。跨类别的经验（升维到顶层后）才做全量扫描。

    - category 为 None（顶层 / 跨类别）→ 全量扫描：项目级 + 全局级
    - 否则 → 只扫描项目级规则目录
    """
    if category is None:
        return [layout.project_rules_dir, layout.global_rules_dir]
    return [layout.project_rules_dir]


# ---------------------------------------------------------------------------
# 相似度计算（内部辅助）
# ---------------------------------------------------------------------------


def _content_similarity(a: str, b: str) -> float:
    """计算两段文本的内容相似度（difflib SequenceMatcher ratio）。"""
    return SequenceMatcher(None, a, b).ratio()


def _same_category(a: str | None, b: str | None) -> bool:
    """判断两个规则的适用范围（category）是否一致。"""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    return a.strip().lower() == b.strip().lower()


def _extract_keywords(text: str) -> set[str]:
    """提取文本关键词（按非字母数字字符分词，过滤短词）。"""
    tokens = re.split(r"\W+", text.lower())
    return {t for t in tokens if len(t) >= _KEYWORD_MIN_LENGTH}


def _rule_text(rule: RuleEntry) -> str:
    """规则的文本（标题 + 内容），用于关键词提取与子集判定。"""
    return f"{rule.title}\n{rule.content}"


def _is_subset(new_rule: RuleEntry, existing: RuleEntry) -> bool:
    """判断新经验是否是已有规则的子集（同类别 + 新经验所有关键词都在已有规则中）。

    子集要求适用范围一致（同 category）且新经验的所有关键词都在已有规则文本中出现。
    用子串匹配而非精确 token 相等，兼容中文（中文无词边界，\\W+ 分词产出的 token 过长）。
    """
    if not _same_category(new_rule.category, existing.category):
        return False
    new_keywords = _extract_keywords(_rule_text(new_rule))
    if not new_keywords:
        return False
    existing_text = _rule_text(existing).lower()
    return all(kw in existing_text for kw in new_keywords)


# ---------------------------------------------------------------------------
# 查重判定
# ---------------------------------------------------------------------------


def _compute_duplicate(
    new_rule: RuleEntry, existing_rules: list[RuleEntry]
) -> tuple[DedupVerdict, RuleEntry | None]:
    """内部：计算查重判定和匹配的已有规则。

    返回 (判定结果, 匹配的已有规则)。无匹配时已有规则为 None。
    """
    if not existing_rules:
        return DedupVerdict.NO_DUPLICATE, None

    # 计算与每条已有规则的内容相似度，找最佳匹配
    best_ratio = 0.0
    best_match: RuleEntry | None = None
    for existing in existing_rules:
        ratio = _content_similarity(new_rule.content, existing.content)
        if ratio > best_ratio:
            best_ratio = ratio
            best_match = existing

    # 判定（按优先级）
    # 1. 完全重复：内容相似度 >= 0.9 且适用范围一致
    if best_ratio >= EXACT_DUPLICATE_THRESHOLD:
        if _same_category(new_rule.category, best_match.category):
            return DedupVerdict.EXACT_DUPLICATE, best_match
        # 内容高度相似但适用范围不同 → 交叉重复
        return DedupVerdict.CROSS_DUPLICATE, best_match

    # 2. 包含重复：新经验所有关键词都在某条已有规则中
    for existing in existing_rules:
        if _is_subset(new_rule, existing):
            return DedupVerdict.SUBSET_DUPLICATE, existing

    # 3. 交叉重复：内容相似度 >= 0.6（有交集但不包含）
    if best_ratio >= CROSS_DUPLICATE_THRESHOLD:
        return DedupVerdict.CROSS_DUPLICATE, best_match

    # 4. 不重复
    return DedupVerdict.NO_DUPLICATE, None


def check_duplicate(
    new_rule: RuleEntry, existing_rules: list[RuleEntry]
) -> DedupVerdict:
    """判定新规则与已有规则的重复类型。

    返回四种判定之一：
    - EXACT_DUPLICATE：规则内容和适用范围一致
    - SUBSET_DUPLICATE：新经验是已有规则的子集
    - CROSS_DUPLICATE：适用范围有交集但不包含
    - NO_DUPLICATE：无交集
    """
    verdict, _ = _compute_duplicate(new_rule, existing_rules)
    return verdict


# ---------------------------------------------------------------------------
# 查重结果
# ---------------------------------------------------------------------------


@dataclass
class DedupResult:
    """查重结果。"""

    verdict: DedupVerdict
    duplicate_of: str | None  # 重复的规则 ID
    existing_rule: RuleEntry | None
    action: str  # "no_duplicate" / "find_untriggered_reason" / "skip" / "split"
    state: PromotionState


# ---------------------------------------------------------------------------
# DedupChecker
# ---------------------------------------------------------------------------


class DedupChecker:
    """查重流程编排器。

    用法：
        checker = DedupChecker(adapter)
        result = checker.check(new_rule, state, layout)
    """

    def __init__(self, adapter: CodingSoftwareAdapter) -> None:
        self._adapter = adapter

    def check(
        self,
        new_rule: RuleEntry,
        state: PromotionState,
        layout: MemoryLayout,
    ) -> DedupResult:
        """执行查重流程。

        1. 分桶定位
        2. 解析已有规则
        3. 判定重复类型
        4. 更新 state（increment_dedup + last_dedup_verdict）
        5. 检查熔断（dedup_count >= 6 → PENDING_CONFIRMATION）
        """
        # 1. 分桶定位：顶层规则跨类别，全量扫描
        lookup_category = new_rule.category
        if state.is_top_layer:
            lookup_category = None
        scan_dirs = bucket_lookup(lookup_category, layout)

        # 2. 解析已有规则
        existing_rules: list[RuleEntry] = []
        for d in scan_dirs:
            existing_rules.extend(self._adapter.parse_existing_rules(d))

        # 3. 判定重复类型
        verdict, matched_rule = _compute_duplicate(new_rule, existing_rules)

        # 4. 更新 state
        state.increment_dedup()
        state.last_dedup_verdict = verdict

        # 5. 检查熔断（查重循环 6 次）
        if state.dedup_count >= DEFAULT_LIMITS.dedup_max:
            state.status = PromotionStatus.PENDING_CONFIRMATION
            state.circuit_breaker_reason = (
                f"查重循环熔断：{DEFAULT_LIMITS.dedup_max}次"
            )

        return DedupResult(
            verdict=verdict,
            duplicate_of=matched_rule.rule_id if matched_rule else None,
            existing_rule=matched_rule,
            action=_VERDICT_ACTION_MAP[verdict],
            state=state,
        )


__all__ = [
    "DedupChecker",
    "DedupResult",
    "bucket_lookup",
    "check_duplicate",
]
