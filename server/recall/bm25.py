"""BM25 排序算法（模块范围关键词检索）。

对应技术方案 3.2b 召回流程"向量检索 + BM25 + RRF 混合排序"中的 BM25 通道，
以及 7.5 DB 故障降级路径下的"模块范围 BM25 关键词检索"。

实现要点：
- 标准 BM25 公式（k1=1.5, b=0.75）
- 中文友好的分词：英文按非字母数字分割，中文按字 + 双字 bigram 兜底
  （避免依赖 jieba 等外部包；项目 pyproject.toml 未声明中文分词依赖）
- 支持多次查询复用（构建一次 BM25Index，多次 score）
- 仅用于模块范围语料（数量级 < 1k），不做大规模优化
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

# 英文 token：连续字母数字（含下划线 / 短横线）
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]+")
# 中文字符范围（基本 + 扩展 A 兜底）
_CJK_RE = re.compile(r"[\u4e00-\u9fff]+")


def tokenize(text: str) -> list[str]:
    """中文友好分词：英文 token + 中文单字 + 中文双字 bigram。

    设计目标：
    - 不依赖外部中文分词库
    - 单字覆盖度高（保证 recall），双字 bigram 提升精度
    - 同一 token 在两份文档中分词一致（确定性）
    """
    if not text:
        return []
    tokens: list[str] = []
    # 英文 token
    for m in _TOKEN_RE.finditer(text):
        tokens.append(m.group(0).lower())
    # 中文：单字 + 双字 bigram
    for cjk_match in _CJK_RE.finditer(text):
        segment = cjk_match.group(0)
        # 单字
        tokens.extend(list(segment))
        # 双字 bigram（长度 ≥ 2 时）
        for i in range(len(segment) - 1):
            tokens.append(segment[i : i + 2])
    return tokens


@dataclass
class BM25Doc:
    """BM25 文档条目。"""

    doc_id: str  # 资产 id
    tokens: list[str]  # 分词后的 token 序列
    term_freq: Counter = field(default_factory=Counter)
    length: int = 0

    @classmethod
    def build(cls, doc_id: str, content: str) -> "BM25Doc":
        toks = tokenize(content)
        return cls(
            doc_id=doc_id,
            tokens=toks,
            term_freq=Counter(toks),
            length=len(toks),
        )


class BM25Index:
    """BM25 索引，构建一次可多次 score。

    算法参数：
    - k1=1.5：词频饱和控制
    - b=0.75：文档长度归一化强度
    - IDF：log((N - df + 0.5) / (df + 0.5) + 1)，避免负值
    """

    def __init__(self, *, k1: float = 1.5, b: float = 0.75) -> None:
        self.k1 = k1
        self.b = b
        self._docs: dict[str, BM25Doc] = {}
        self._df: Counter = Counter()  # 文档频率
        self._avgdl: float = 0.0
        self._n: int = 0

    def add(self, doc_id: str, content: str) -> None:
        """添加一篇文档（增量构建）。"""
        if doc_id in self._docs:
            # 已存在 → 先移除旧的 df 贡献，再重新加
            self._remove(doc_id)
        doc = BM25Doc.build(doc_id, content)
        self._docs[doc_id] = doc
        for term in doc.term_freq:
            self._df[term] += 1
        self._recompute_stats()

    def _remove(self, doc_id: str) -> None:
        doc = self._docs.pop(doc_id, None)
        if doc is None:
            return
        for term in doc.term_freq:
            self._df[term] -= 1
            if self._df[term] <= 0:
                del self._df[term]
        self._recompute_stats()

    def _recompute_stats(self) -> None:
        self._n = len(self._docs)
        total_len = sum(d.length for d in self._docs.values())
        self._avgdl = total_len / self._n if self._n > 0 else 0.0

    def score(self, query: str) -> list[tuple[str, float]]:
        """对查询打分，返回 [(doc_id, score), ...] 按分数降序。

        空查询或空索引返回空列表。
        """
        if not query or self._n == 0:
            return []
        q_tokens = tokenize(query)
        if not q_tokens:
            return []
        results: list[tuple[str, float]] = []
        for doc_id, doc in self._docs.items():
            s = 0.0
            for qt in q_tokens:
                f = doc.term_freq.get(qt, 0)
                if f == 0:
                    continue
                df = self._df.get(qt, 0)
                # IDF（带 +1 防负）
                idf = math.log((self._n - df + 0.5) / (df + 0.5) + 1.0)
                # 文档长度归一化
                denom = f + self.k1 * (
                    1.0 - self.b + self.b * (doc.length / self._avgdl if self._avgdl > 0 else 0.0)
                )
                s += idf * (f * (self.k1 + 1.0)) / denom
            if s > 0.0:
                results.append((doc_id, s))
        results.sort(key=lambda kv: kv[1], reverse=True)
        return results

    def __len__(self) -> int:
        return self._n


__all__ = [
    "BM25Doc",
    "BM25Index",
    "tokenize",
]
