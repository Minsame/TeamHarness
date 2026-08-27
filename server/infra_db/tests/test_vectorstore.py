"""SubTask 2.2：VectorStore Provider 三实现（InMemory / Qdrant / PGVector）。

Qdrant / PGVector 因依赖外部服务（Qdrant 服务 / pgvector 扩展），
测试中通过 mock client / mock engine 验证调用契约，不连真实服务。
InMemory 完整端到端测试。
"""

from __future__ import annotations

import pytest

from server.infra_db.vectorstore import (
    InMemoryVectorStore,
    VectorRecord,
    VectorSearchHit,
    _cosine,
)


# ---------------------------------------------------------------------------
# InMemory 完整测试
# ---------------------------------------------------------------------------


def test_inmemory_upsert_search_delete():
    """InMemory upsert / search / delete 完整流程。"""
    store = InMemoryVectorStore()
    store.ensure_collection("v1", dim=3)
    record = VectorRecord(
        asset_id="a1",
        vector=[1.0, 0.0, 0.0],
        dim=3,
        metadata={"module_path": "modules/backend", "category": "rule-backend"},
    )
    point_id = store.upsert(record, "v1")
    assert point_id

    # 检索：相同向量命中
    hits = store.search([1.0, 0.0, 0.0], model_version="v1", top_k=5)
    assert len(hits) == 1
    assert hits[0].asset_id == "a1"
    assert hits[0].score == pytest.approx(1.0, abs=1e-6)

    # 元数据过滤
    hits = store.search(
        [1.0, 0.0, 0.0],
        model_version="v1",
        top_k=5,
        filter={"module_path": "modules/backend"},
    )
    assert len(hits) == 1
    hits = store.search(
        [1.0, 0.0, 0.0],
        model_version="v1",
        top_k=5,
        filter={"module_path": "modules/frontend"},
    )
    assert len(hits) == 0

    # get
    rec = store.get("a1", "v1")
    assert rec is not None
    assert rec.dim == 3
    assert rec.metadata["category"] == "rule-backend"

    # delete
    store.delete("a1", "v1")
    assert store.get("a1", "v1") is None
    assert store.search([1.0, 0.0, 0.0], model_version="v1", top_k=5) == []


def test_inmemory_isolation_between_model_versions():
    """不同 model_version 的向量互不影响（双写过渡关键）。"""
    store = InMemoryVectorStore()
    store.upsert(
        VectorRecord(asset_id="a1", vector=[1.0, 0.0], dim=2, metadata={}),
        model_version="v1",
    )
    store.upsert(
        VectorRecord(asset_id="a1", vector=[0.0, 1.0], dim=2, metadata={}),
        model_version="v2",
    )
    # 检索 v1 只命中 v1 向量
    hits_v1 = store.search([1.0, 0.0], model_version="v1", top_k=5)
    assert len(hits_v1) == 1
    assert hits_v1[0].score == pytest.approx(1.0, abs=1e-6)
    # 检索 v2 只命中 v2 向量
    hits_v2 = store.search([0.0, 1.0], model_version="v2", top_k=5)
    assert len(hits_v2) == 1
    assert hits_v2[0].score == pytest.approx(1.0, abs=1e-6)
    # 跨版本检索应 0 分（向量不同）
    cross = store.search([1.0, 0.0], model_version="v2", top_k=5)
    assert len(cross) == 1
    assert cross[0].score == pytest.approx(0.0, abs=1e-6)


def test_inmemory_top_k_limit():
    """top_k 截断生效。"""
    store = InMemoryVectorStore()
    for i in range(5):
        store.upsert(
            VectorRecord(
                asset_id=f"a{i}",
                vector=[float(i), 0.0],
                dim=2,
                metadata={},
            ),
            model_version="v1",
        )
    hits = store.search([0.0, 0.0], model_version="v1", top_k=3)
    assert len(hits) == 3


def test_cosine_similarity():
    """余弦相似度基本性质。"""
    assert _cosine([1, 0], [1, 0]) == pytest.approx(1.0)
    assert _cosine([1, 0], [0, 1]) == pytest.approx(0.0)
    assert _cosine([1, 0], [-1, 0]) == pytest.approx(-1.0)
    assert _cosine([], []) == 0.0
    assert _cosine([0, 0], [1, 0]) == 0.0  # 零向量返回 0


# ---------------------------------------------------------------------------
# Qdrant mock 测试（验证调用契约，不连真实 Qdrant）
# ---------------------------------------------------------------------------


class _FakeQdrantClient:
    """模拟 QdrantClient 接口，记录调用与返回固定结果。"""

    def __init__(self):
        self.collections: dict[str, list] = {}
        self.calls: list[tuple] = []

    def get_collection(self, name):
        self.calls.append(("get_collection", name))
        if name not in self.collections:
            raise RuntimeError("not found")
        return self.collections[name]

    def recreate_collection(self, *, collection_name, vectors_config):
        self.calls.append(("recreate_collection", collection_name))
        self.collections[collection_name] = []

    def upsert(self, *, collection_name, points):
        self.calls.append(("upsert", collection_name, len(points)))
        self.collections.setdefault(collection_name, []).extend(points)
        return {"status": "ok"}

    def search(self, *, collection_name, query_vector, limit, query_filter=None):
        self.calls.append(("search", collection_name, limit))
        # 简单返回所有点（按 score=1.0）
        points = self.collections.get(collection_name, [])
        return [
            type("Hit", (), {"id": p.id, "score": 1.0, "payload": p.payload})()
            for p in points[:limit]
        ]

    def delete(self, *, collection_name, points_selector):
        self.calls.append(("delete", collection_name))
        self.collections.pop(collection_name, None)

    def scroll(self, *, collection_name, scroll_filter, limit, with_vectors, with_payload):
        self.calls.append(("scroll", collection_name))
        points = self.collections.get(collection_name, [])
        matched = [
            type(
                "Point",
                (),
                {
                    "id": p.id,
                    "payload": p.payload,
                    "vector": p.vector,
                },
            )()
            for p in points
            if p.payload.get("asset_id") == scroll_filter.must[0].match.value
        ][:limit]
        return matched, None


def test_qdrant_upsert_search_delete_contract():
    """Qdrant 实现的 upsert / search / delete 调用契约（mock）。"""
    from server.infra_db.vectorstore import QdrantVectorStore

    fake = _FakeQdrantClient()
    store = QdrantVectorStore(client=fake)
    # 第一次 ensure_collection 会建 collection
    store.upsert(
        VectorRecord(asset_id="a1", vector=[1.0, 0.0], dim=2, metadata={"k": "v"}),
        model_version="v1",
    )
    assert "teamharness_v1" in fake.collections
    # 检索
    hits = store.search([1.0, 0.0], model_version="v1", top_k=5)
    assert len(hits) == 1
    assert hits[0].asset_id == "a1"
    # get
    rec = store.get("a1", "v1")
    assert rec is not None
    assert rec.asset_id == "a1"
    # delete
    store.delete("a1", "v1")
    assert ("delete", "teamharness_v1") in fake.calls


# ---------------------------------------------------------------------------
# PGVector mock 测试（验证 SQL 构造，不连真实 PG）
# ---------------------------------------------------------------------------


class _FakeConn:
    """模拟 SQLAlchemy Connection。"""

    def __init__(self):
        self.executed: list[tuple] = []

    def execute(self, stmt, params=None):
        self.executed.append((str(stmt), params))
        # 模拟 fetchone / fetchall
        return self

    def fetchone(self):
        return None

    def fetchall(self):
        return []


class _FakePGEngine:
    """模拟 PG 引擎，提供 begin() 上下文。"""

    def __init__(self):
        self.conn = _FakeConn()

    class _Ctx:
        def __init__(self, engine):
            self.engine = engine

        def __enter__(self):
            return self.engine.conn

        def __exit__(self, *args):
            return False

    def begin(self):
        return self._Ctx(self)


def test_pgvector_ensure_collection_creates_extension_and_table():
    """PGVector ensure_collection 应创建 vector 扩展与表。"""
    from server.infra_db.vectorstore import PGVectorStore

    engine = _FakePGEngine()
    store = PGVectorStore(engine=engine)
    try:
        store.ensure_collection("v1", dim=128)
    except Exception:
        # fake conn 不支持 IF NOT EXISTS 重复检测，但 SQL 已构造
        pass
    # 第一句应是 CREATE EXTENSION IF NOT EXISTS vector
    assert any("CREATE EXTENSION" in s and "vector" in s for s, _ in engine.conn.executed)
    # 应有 CREATE TABLE teamharness_v1
    assert any("teamharness_v1" in s for s, _ in engine.conn.executed)
    # 应有 ivfflat 索引
    assert any("ivfflat" in s for s, _ in engine.conn.executed)


# ---------------------------------------------------------------------------
# 工厂测试
# ---------------------------------------------------------------------------


def test_create_vector_store_memory():
    from server.infra_db.vectorstore import create_vector_store

    store = create_vector_store(kind="memory")
    assert store.backend_name == "memory"


def test_create_vector_store_unknown_kind():
    from server.infra_db.vectorstore import create_vector_store

    with pytest.raises(ValueError, match="未知 VectorStore"):
        create_vector_store(kind="unknown")
