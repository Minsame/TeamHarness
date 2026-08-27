"""冲突解决器（Conflict Resolver）模块。

对应 Task 19：在 peer 上线同步后，对比本地模拟回答与对方真实回答，
基于语义相似度阈值决定自动确认、自动修订还是标记人工介入。

核心设计：
- **三区间决策**：
  - 相似度 >= ``auto_confirm_threshold``（默认 0.8）→ ``confirmed``（自动确认）
  - 相似度 <= ``conflict_threshold``（默认 0.3）→ ``needs_human_review``（人工介入）
  - 中间区间 → ``revised``（自动修订，附上真实回答）
- **相似度计算**：通过注入的 ``similarity_func`` 计算两个文本的语义相似度。
  默认使用字符级 Jaccard 相似度（不依赖外部库）
- **幂等性**：相同输入总是返回相同结果（确定性，不依赖随机性或外部状态）

ConflictResolver 是独立模块，不依赖 sync_protocol.py / peer_comm.py / shadow_comm.py
（避免循环依赖）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from server.async_comm.constants import (
    DEFAULT_AUTO_CONFIRM_THRESHOLD,
    DEFAULT_CONFLICT_THRESHOLD,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REVISED,
)

# 相似度计算函数类型：接收两个字符串，返回 [0.0, 1.0] 的相似度
SimilarityFunc = Callable[[str, str], float]


def default_similarity(text_a: str, text_b: str) -> float:
    """默认相似度计算：字符级 Jaccard 相似度。

    将两个文本按字符分词（适用于中英文混合），计算字符集合的 Jaccard 系数：
    ``|A ∩ B| / |A ∪ B|``。

    - 大小写不敏感（统一转小写）
    - 两个空字符串返回 1.0（完全相同）
    - 一个空一个非空返回 0.0

    返回 [0.0, 1.0]。
    """
    set_a = set(text_a.lower())
    set_b = set(text_b.lower())
    if not set_a and not set_b:
        return 1.0
    if not set_a or not set_b:
        return 0.0
    intersection = set_a & set_b
    union = set_a | set_b
    return len(intersection) / len(union)


@dataclass
class ResolutionResult:
    """冲突解决结果。"""

    decision: str  # "confirmed" / "revised" / "needs_human_review"
    similarity: float  # 计算出的相似度 [0.0, 1.0]
    note: str = ""  # 附加说明


class ConflictResolver:
    """冲突解决器。

    基于语义相似度阈值决定模拟回答与真实回答的对账结果。

    使用：
        resolver = ConflictResolver()
        decision, note = resolver.resolve(
            simulated_answer="模拟回答...",
            real_answer="真实回答...",
        )
    """

    def __init__(
        self,
        *,
        auto_confirm_threshold: float = DEFAULT_AUTO_CONFIRM_THRESHOLD,
        conflict_threshold: float = DEFAULT_CONFLICT_THRESHOLD,
        similarity_func: SimilarityFunc | None = None,
    ) -> None:
        """初始化 ConflictResolver。

        :param auto_confirm_threshold: 自动确认阈值（默认 0.8）
        :param conflict_threshold: 冲突阈值（默认 0.3）
        :param similarity_func: 相似度计算函数（默认用 default_similarity）
        :raises ValueError: 当 ``auto_confirm_threshold`` 不大于 ``conflict_threshold``
        """
        if auto_confirm_threshold <= conflict_threshold:
            raise ValueError(
                "auto_confirm_threshold must be greater than conflict_threshold"
            )
        self.auto_confirm_threshold = auto_confirm_threshold
        self.conflict_threshold = conflict_threshold
        self.similarity_func: SimilarityFunc = (
            similarity_func if similarity_func is not None else default_similarity
        )

    def resolve(
        self,
        *,
        simulated_answer: str,
        real_answer: str,
        event_id: str = "",
    ) -> tuple[str, str]:
        """对比模拟回答与真实回答，返回 ``(decision, note)``。

        流程：
        1. 计算相似度
        2. 相似度 >= ``auto_confirm_threshold`` → ``"confirmed"``
        3. 相似度 <= ``conflict_threshold`` → ``"needs_human_review"``
        4. 中间区间 → ``"revised"``

        :param simulated_answer: 本地模拟回答
        :param real_answer: peer 真实回答
        :param event_id: 关联事件 ID（仅用于 note 说明，可为空）
        :return: ``(decision, note)`` 元组，note 含相似度数值与决策原因
        """
        similarity = self.similarity_func(simulated_answer, real_answer)
        decision = self._decide(similarity)
        note = self._format_note(decision, similarity, event_id)
        return decision, note

    def resolve_with_detail(
        self,
        *,
        simulated_answer: str,
        real_answer: str,
        event_id: str = "",
    ) -> ResolutionResult:
        """带详细信息的冲突解决，返回 :class:`ResolutionResult`。

        :param simulated_answer: 本地模拟回答
        :param real_answer: peer 真实回答
        :param event_id: 关联事件 ID（可为空）
        :return: 含 decision / similarity / note 的 ResolutionResult
        """
        similarity = self.similarity_func(simulated_answer, real_answer)
        decision = self._decide(similarity)
        note = self._format_note(decision, similarity, event_id)
        return ResolutionResult(
            decision=decision,
            similarity=similarity,
            note=note,
        )

    def batch_resolve(
        self,
        items: list[dict[str, str]],
    ) -> list[ResolutionResult]:
        """批量解决冲突。

        :param items: 每个元素为 ``{"simulated_answer": ..., "real_answer": ...,
            "event_id": ...}``（event_id 可选）
        :return: 对应数量的 :class:`ResolutionResult` 列表
        """
        results: list[ResolutionResult] = []
        for item in items:
            results.append(
                self.resolve_with_detail(
                    simulated_answer=item.get("simulated_answer", ""),
                    real_answer=item.get("real_answer", ""),
                    event_id=item.get("event_id", ""),
                )
            )
        return results

    def _decide(self, similarity: float) -> str:
        """根据相似度与阈值决定对账结果。"""
        if similarity >= self.auto_confirm_threshold:
            return EVENT_CONFIRMED
        if similarity <= self.conflict_threshold:
            return EVENT_NEEDS_HUMAN_REVIEW
        return EVENT_REVISED

    def _format_note(self, decision: str, similarity: float, event_id: str) -> str:
        """格式化附加说明。"""
        sim_str = f"{similarity:.4f}"
        if decision == EVENT_CONFIRMED:
            reason = (
                f"auto_confirmed (threshold={self.auto_confirm_threshold})"
            )
        elif decision == EVENT_NEEDS_HUMAN_REVIEW:
            reason = (
                f"needs_human_review (threshold={self.conflict_threshold})"
            )
        else:
            reason = (
                f"revised (auto_confirm={self.auto_confirm_threshold}, "
                f"conflict={self.conflict_threshold})"
            )
        note = f"similarity={sim_str}, {reason}"
        if event_id:
            note = f"event_id={event_id}, {note}"
        return note


__all__ = [
    "ConflictResolver",
    "ResolutionResult",
    "SimilarityFunc",
    "default_similarity",
]
