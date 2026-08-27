"""SubTask 2.7：EmbeddingService + 双写过渡 + RRF 融合。"""

from __future__ import annotations

import pytest

from server.infra_db.embedding import EmbeddingService


def test_embed_returns_active_version_by_default():
    """embed 默认返回 active_version 的向量。"""
    svc = EmbeddingService(active_version="v1", dim=32)
    result = svc.embed("hello")
    assert result.model_version == "v1"
    assert len(result.vector) == 32
    assert result.dim == 32


def test_embed_with_explicit_model_version():
    """embed 支持显式 model_version。"""
    svc = EmbeddingService(active_version="v1", dim=16)
    result = svc.embed("hello", model_version="v2")
    assert result.model_version == "v2"


def test_embed_batch():
    """embed_batch 批量计算。"""
    svc = EmbeddingService(active_version="v1", dim=8)
    results = svc.embed_batch(["a", "b", "c"])
    assert len(results) == 3
    assert all(r.model_version == "v1" for r in results)


def test_get_active_and_shadow_version():
    """get_active_version / get_shadow_version 反映配置。"""
    svc = EmbeddingService(active_version="v1", shadow_version="v2")
    assert svc.get_active_version() == "v1"
    assert svc.get_shadow_version() == "v2"

    svc2 = EmbeddingService(active_version="v1")
    assert svc2.get_shadow_version() == ""


def test_embed_dual_write():
    """双写：同时返回 active + shadow 两套向量。"""
    svc = EmbeddingService(active_version="v1", shadow_version="v2", dim=16)
    results = svc.embed_dual_write("hello")
    assert "v1" in results
    assert "v2" in results
    assert len(results) == 2


def test_embed_dual_write_no_shadow():
    """无 shadow_version 时 dual_write 只返回 active。"""
    svc = EmbeddingService(active_version="v1", dim=16)
    results = svc.embed_dual_write("hello")
    assert len(results) == 1
    assert "v1" in results


def test_start_shadow_write_validates_not_equal():
    """start_shadow_write 不允许 shadow == active。"""
    svc = EmbeddingService(active_version="v1", dim=16)
    with pytest.raises(ValueError, match="不能等于"):
        svc.start_shadow_write("v1")


def test_switch_active_version_clears_shadow():
    """switch_active_version 后，旧 shadow 升级为 active 则清空 shadow。"""
    svc = EmbeddingService(active_version="v1", shadow_version="v2", dim=16)
    svc.switch_active_version("v2")
    assert svc.get_active_version() == "v2"
    assert svc.get_shadow_version() == ""


def test_stop_shadow_write():
    """stop_shadow_write 清空 shadow。"""
    svc = EmbeddingService(active_version="v1", shadow_version="v2", dim=16)
    svc.stop_shadow_write()
    assert svc.get_shadow_version() == ""


# ---------------------------------------------------------------------------
# RRF 融合（RecallService 调用）
# ---------------------------------------------------------------------------


def test_fuse_rrf_basic():
    """RRF 融合两套召回结果。"""
    hits_per_version = {
        "v1": [("a1", 0.9), ("a2", 0.8), ("a3", 0.7)],
        "v2": [("a2", 0.95), ("a1", 0.85), ("a4", 0.6)],
    }
    fused = EmbeddingService.fuse_rrf(hits_per_version, k=60, top_k=10)
    # a1 与 a2 同时出现在两套 → 排名靠前
    top_ids = [aid for aid, _ in fused]
    assert "a1" in top_ids[:2]
    assert "a2" in top_ids[:2]
    # a3 只在 v1，a4 只在 v2，分数较低
    assert top_ids.index("a3") > top_ids.index("a2")
    assert top_ids.index("a4") > top_ids.index("a2")


def test_fuse_rrf_single_version():
    """单版本 RRF 等价于按原顺序排名。"""
    hits_per_version = {
        "v1": [("a1", 0.9), ("a2", 0.8), ("a3", 0.7)],
    }
    fused = EmbeddingService.fuse_rrf(hits_per_version, top_k=3)
    assert [aid for aid, _ in fused] == ["a1", "a2", "a3"]


def test_fuse_rrf_empty():
    """空输入 RRF 返回空。"""
    fused = EmbeddingService.fuse_rrf({}, top_k=10)
    assert fused == []


def test_fuse_rrf_top_k_limit():
    """top_k 截断生效。"""
    hits_per_version = {
        "v1": [("a1", 0.9), ("a2", 0.8), ("a3", 0.7), ("a4", 0.6)],
    }
    fused = EmbeddingService.fuse_rrf(hits_per_version, top_k=2)
    assert len(fused) == 2


def test_fuse_rrf_unique_assets_in_one_version():
    """资产只在一个版本出现：分数低但仍计入。"""
    hits_per_version = {
        "v1": [("a1", 0.9)],
        "v2": [("a2", 0.95)],
    }
    fused = EmbeddingService.fuse_rrf(hits_per_version, top_k=10)
    ids = [aid for aid, _ in fused]
    assert "a1" in ids
    assert "a2" in ids
    # 两者 RRF 分数相同（都是 1/(60+1)），顺序由 sorted 稳定决定
