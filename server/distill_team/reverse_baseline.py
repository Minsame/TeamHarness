"""反向验证基线（SubTask 8.11）。

冷启动期（资产 < 50）用公开 Prompt 数据集（种子库）校准模型输出：
- 用种子库的 SeedPrompt 作为已知"高质量"基线
- 跑 LLM 提炼后，比对产出与基线的语义相似度
- 相似度 < 阈值 → 模型输出可能漂移，标记 confidence=low

用途：冷启动期缺乏真实召回数据，用公开 Prompt 数据集作为锚点，
防止 LLM 在低数据量下产出偏差。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from server.distill_team.seed_library import SeedLibrary, SeedPrompt

logger = logging.getLogger(__name__)


@dataclass
class BaselineMatch:
    """反向验证基线匹配结果。"""

    seed: SeedPrompt
    similarity: float  # 0.0-1.0
    passed: bool  # similarity >= threshold


class ReverseBaseline:
    """反向验证基线（冷启动期用公开 Prompt 数据集校准）。

    用法：
        baseline = ReverseBaseline(seed_library=SeedLibrary(repo_root))
        matches = baseline.match(prompt_content="# PR Review\n...", threshold=0.3)
        if not any(m.passed for m in matches):
            # 模型输出与基线偏差大 → 降级 confidence
            prompt.confidence = "low"
    """

    def __init__(
        self,
        seed_library: SeedLibrary,
        *,
        similarity_threshold: float = 0.3,
    ) -> None:
        self._seed_lib = seed_library
        self._threshold = similarity_threshold

    def match(
        self,
        *,
        prompt_content: str,
        prompt_category: str | None = None,
        threshold: float | None = None,
    ) -> list[BaselineMatch]:
        """用种子库匹配 Prompt 内容，返回每种种子的相似度。

        - prompt_category 给定 → 仅匹配同 category 种子（加速）
        - threshold 覆盖默认阈值
        """
        thr = threshold if threshold is not None else self._threshold
        seeds = self._seed_lib.list_seeds()
        if prompt_category:
            seeds = [s for s in seeds if s.category == prompt_category] or seeds

        matches: list[BaselineMatch] = []
        for seed in seeds:
            sim = self._compute_similarity(prompt_content, seed.content)
            matches.append(
                BaselineMatch(
                    seed=seed,
                    similarity=sim,
                    passed=sim >= thr,
                )
            )
        # 按相似度降序
        matches.sort(key=lambda m: m.similarity, reverse=True)
        return matches

    def is_calibrated(
        self,
        *,
        prompt_content: str,
        prompt_category: str | None = None,
        threshold: float | None = None,
    ) -> bool:
        """是否通过基线校准（至少一个种子相似度 ≥ 阈值）。"""
        matches = self.match(
            prompt_content=prompt_content,
            prompt_category=prompt_category,
            threshold=threshold,
        )
        return any(m.passed for m in matches)

    # ------------------------------------------------------------------
    # 内部：相似度计算（基于关键词重叠的简化 Jaccard）
    # ------------------------------------------------------------------

    def _compute_similarity(self, text_a: str, text_b: str) -> float:
        """计算两段文本的相似度（基于关键词 Jaccard）。

        简化实现：去除停用词后取词集合 Jaccard。
        真实场景应用 embedding 余弦相似度。
        """
        if not text_a or not text_b:
            return 0.0
        words_a = self._tokenize(text_a)
        words_b = self._tokenize(text_b)
        if not words_a or not words_b:
            return 0.0
        intersection = words_a & words_b
        union = words_a | words_b
        return len(intersection) / len(union) if union else 0.0

    def _tokenize(self, text: str) -> set[str]:
        """简单分词：按空格/标点切分，转小写，去停用词。"""
        import re

        # 中英文混合分词：英文按空格，中文按字
        words: set[str] = set()
        # 英文词
        for w in re.findall(r"[a-zA-Z_]{2,}", text.lower()):
            if w not in _STOPWORDS_EN:
                words.add(w)
        # 中文字（bigram 提升匹配）
        chinese = re.findall(r"[\u4e00-\u9fff]", text)
        for i in range(len(chinese) - 1):
            words.add(chinese[i] + chinese[i + 1])
        return words


# 英文停用词（简化版）
_STOPWORDS_EN = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "and", "or", "but", "if", "then", "of", "to", "in", "on", "at", "by",
    "for", "with", "without", "from", "as", "this", "that", "these", "those",
    "it", "its", "they", "them", "their", "we", "us", "our", "you", "your",
    "he", "she", "his", "her", "i", "me", "my",
    "should", "must", "shall", "will", "would", "can", "could", "may", "might",
    "do", "does", "did", "done", "have", "has", "had", "having",
    "not", "no", "yes", "so", "than", "too", "very", "just",
}


__all__ = ["BaselineMatch", "ReverseBaseline"]
