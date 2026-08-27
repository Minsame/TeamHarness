"""EmbeddingService — 资产文本向量化服务。

对应 SubTask 2.7（embedding 模型双写过渡） + 对外契约 API：
- embed(text) → (vector, dim, model_version)
- embed_batch(texts) → [(vector, dim, model_version), ...]
- get_active_version() → 当前激活版本字符串

双写过渡设计：
- active_embedding_version 控制主版本（召回用）
- migration_shadow_version 不为空时，同时计算阴影版本（双写）
- 全量迁移完成后，切换 active_embedding_version，drop 旧向量
- 召回侧由 RecallService（Agent 4）调用 fuse_rrf 融合两套结果

底层 embedding 计算依赖 LLMProvider（Agent 7 提供），本服务通过
`embedding_function` 注入接受 LLM 调用。Agent 7 完成后切换真实调用。
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class EmbeddingResult:
    """单条 embedding 结果。"""

    vector: list[float]
    dim: int
    model_version: str


# 类型别名：embedding 函数签名 (text, model_version) -> Sequence[float]
EmbeddingFunction = Callable[[str, str], Sequence[float]]


def _hash_embedding(text: str, dim: int = 64) -> list[float]:
    """兜底 embedding：用 SHA256 哈希投影到 dim 维向量。

    仅用于无 LLMProvider 时的占位测试，保证可重现。
    生产环境必须注入真实 EmbeddingFunction（Agent 7 提供）。
    """
    import hashlib

    vec = [0.0] * dim
    h = hashlib.sha256(text.encode("utf-8")).digest()
    # 32 字节循环填入 dim 维
    for i in range(dim):
        vec[i] = (h[i % len(h)] / 255.0) * 2 - 1.0
    # L2 归一化（余弦相似度场景要求）
    norm = sum(x * x for x in vec) ** 0.5
    if norm > 0:
        vec = [x / norm for x in vec]
    return vec


class EmbeddingService:
    """资产文本向量化服务（对外契约 API）。

    通过 `embedding_function` 注入底层 LLM embedding 调用，
    默认用哈希占位（测试用），生产环境由 Agent 7 LLMProvider 注入。

    双写过渡：
    - active_version：当前激活版本，召回使用
    - shadow_version：迁移期阴影版本，双写但不影响召回（直到切换）
    - 迁移完成（drop_old=True）后，shadow 升级为 active，旧 active 废弃
    """

    DEFAULT_DIM = 64  # 默认维度（占位），生产由模型决定

    def __init__(
        self,
        *,
        embedding_function: EmbeddingFunction | None = None,
        active_version: str | None = None,
        shadow_version: str | None = None,
        dim: int | None = None,
    ) -> None:
        self._embedding_function = embedding_function
        self._active_version = active_version or os.environ.get(
            "EMBEDDING_ACTIVE_VERSION", "v1"
        )
        self._shadow_version = shadow_version or os.environ.get(
            "EMBEDDING_SHADOW_VERSION", ""
        )
        # 空字符串视为未配置
        if not self._shadow_version:
            self._shadow_version = ""
        self._dim = dim or int(os.environ.get("EMBEDDING_DIM", str(self.DEFAULT_DIM)))

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def get_active_version(self) -> str:
        """返回当前激活 embedding 版本。"""
        return self._active_version

    def get_shadow_version(self) -> str:
        """返回当前阴影版本（双写过渡用，空字符串表示无阴影）。"""
        return self._shadow_version

    def get_dim(self, model_version: str | None = None) -> int:
        """返回指定版本的向量维度。"""
        return self._dim

    def embed(self, text: str, *, model_version: str | None = None) -> EmbeddingResult:
        """计算单条文本的 embedding。

        - model_version=None → 用 active_version
        - model_version 显式指定 → 用指定版本（双写过渡期对 shadow_version 也调用）
        """
        mv = model_version or self._active_version
        vec = self._call_embedding(text, mv)
        return EmbeddingResult(vector=list(vec), dim=len(vec), model_version=mv)

    def embed_batch(
        self, texts: Sequence[str], *, model_version: str | None = None
    ) -> list[EmbeddingResult]:
        """批量计算 embedding。

        默认实现是循环调用 embed，子类可覆写为批量接口。
        """
        mv = model_version or self._active_version
        return [self.embed(t, model_version=mv) for t in texts]

    def embed_dual_write(self, text: str) -> dict[str, EmbeddingResult]:
        """双写过渡：同时计算 active 与 shadow 两套向量。

        返回 {model_version: EmbeddingResult}，由 outbox worker 分别写入两套向量库。
        无 shadow_version 时只返回 active 一条。
        """
        results: dict[str, EmbeddingResult] = {
            self._active_version: self.embed(text, model_version=self._active_version)
        }
        if self._shadow_version:
            results[self._shadow_version] = self.embed(
                text, model_version=self._shadow_version
            )
        return results

    # ------------------------------------------------------------------
    # 双写过渡控制
    # ------------------------------------------------------------------

    def switch_active_version(self, new_active: str) -> None:
        """切换激活版本（全量迁移完成后调用）。

        旧 active 将被废弃，建议同时调用 drop_old_version 清理向量库。
        """
        old = self._active_version
        self._active_version = new_active
        if self._shadow_version == new_active:
            self._shadow_version = ""
        logger.info("embedding active version 切换：%s → %s", old, new_active)

    def start_shadow_write(self, shadow_version: str) -> None:
        """启动阴影版本双写（迁移开始时调用）。"""
        if shadow_version == self._active_version:
            raise ValueError("shadow_version 不能等于 active_version")
        self._shadow_version = shadow_version
        logger.info(
            "启动 embedding 双写：active=%s shadow=%s",
            self._active_version,
            shadow_version,
        )

    def stop_shadow_write(self) -> None:
        """停止阴影版本双写（迁移完成或回滚时调用）。"""
        self._shadow_version = ""

    # ------------------------------------------------------------------
    # RRF 融合（供 RecallService 调用）
    # ------------------------------------------------------------------

    @staticmethod
    def fuse_rrf(
        hits_per_version: dict[str, list[tuple[str, float]]],
        *,
        k: int = 60,
        top_k: int = 10,
    ) -> list[tuple[str, float]]:
        """Reciprocal Rank Fusion 融合多版本召回结果。

        hits_per_version: {model_version: [(asset_id, score), ...]}
        返回融合后 [(asset_id, fused_score), ...]，按 fused_score 降序取 top_k。

        RRF 公式：score(d) = Σ 1 / (k + rank_i(d))，rank_i 为 d 在第 i 个列表中的排名（1-based）
        对应技术方案 3.2b：召回流程"向量检索 + BM25 + RRF 混合排序"。
        """
        scores: dict[str, float] = {}
        for hits in hits_per_version.values():
            for rank, (asset_id, _score) in enumerate(hits, start=1):
                scores[asset_id] = scores.get(asset_id, 0.0) + 1.0 / (k + rank)
        fused = sorted(scores.items(), key=lambda kv: kv[1], reverse=True)
        return fused[:top_k]

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _call_embedding(self, text: str, model_version: str) -> Sequence[float]:
        if self._embedding_function is not None:
            return self._embedding_function(text, model_version)
        # 兜底：哈希 embedding（仅测试用）
        return _hash_embedding(text, self._dim)


def create_embedding_service(
    *,
    embedding_function: EmbeddingFunction | None = None,
    active_version: str | None = None,
    shadow_version: str | None = None,
    dim: int | None = None,
) -> EmbeddingService:
    """工厂：从环境变量 / 显式参数创建 EmbeddingService。"""
    return EmbeddingService(
        embedding_function=embedding_function,
        active_version=active_version,
        shadow_version=shadow_version,
        dim=dim,
    )


__all__ = [
    "EmbeddingFunction",
    "EmbeddingResult",
    "EmbeddingService",
    "create_embedding_service",
]
