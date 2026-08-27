"""VectorStore Provider 抽象层。

对应技术方案第 4 节 Provider 抽象 + SubTask 2.2：
- VectorStore 抽象接口（upsert / search / delete / get）
- QdrantVectorStore：基于 Qdrant 向量库
- PGVectorStore：基于 PostgreSQL + pgvector 扩展（同库部署）
- InMemoryVectorStore：测试 / 单机模式兜底实现

通过 VECTOR_BACKEND 环境变量切换，生产环境 Qdrant/PGVector 二选一。
embedding 维度由具体模型决定，本层只存储与检索，不计算向量。
"""

from __future__ import annotations

import json
import logging
import math
import os
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Sequence

logger = logging.getLogger(__name__)


@dataclass
class VectorSearchHit:
    """向量检索单条命中。"""

    asset_id: str
    score: float
    # 元数据（如 module_path / category / type），用于过滤与回显
    metadata: dict[str, Any] | None = None


@dataclass
class VectorRecord:
    """向量写入记录。"""

    asset_id: str
    vector: Sequence[float]
    dim: int
    metadata: dict[str, Any] | None = None
    # 指定向量库内部 id（双写过渡期区分新旧版本）；不传则按 asset_id 生成
    point_id: str | None = None


class VectorStore(ABC):
    """向量库抽象接口。

    所有实现需支持 upsert / search / delete / get。
    双写过渡期通过 model_version 区分两套向量（见 SubTask 2.7）。
    """

    #: 后端标识，用于日志与诊断
    backend_name: str = "abstract"

    @abstractmethod
    def ensure_collection(self, model_version: str, dim: int) -> None:
        """确保 collection / 表存在（按 model_version 区分新旧两套）。"""

    @abstractmethod
    def upsert(self, record: VectorRecord, model_version: str) -> str:
        """写入或更新一条向量，返回 embedding_id（point_id）。"""

    @abstractmethod
    def search(
        self,
        query_vector: Sequence[float],
        *,
        model_version: str,
        top_k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        """向量近邻检索。filter 为元数据精确匹配条件。"""

    @abstractmethod
    def delete(self, asset_id: str, model_version: str) -> None:
        """删除指定资产在指定 model_version 下的向量。"""

    @abstractmethod
    def get(self, asset_id: str, model_version: str) -> VectorRecord | None:
        """读取单条向量（不存在返回 None）。"""

    def close(self) -> None:
        """释放后端资源（默认无操作）。"""
        return None


# ---------------------------------------------------------------------------
# InMemory 实现（测试 / 单机模式兜底）
# ---------------------------------------------------------------------------


class InMemoryVectorStore(VectorStore):
    """内存向量库，纯 Python 实现，余弦相似度。

    用于测试 / 单机模式 / 开发环境。生产环境用 Qdrant 或 PGVector。
    """

    backend_name = "memory"

    def __init__(self) -> None:
        # 按 (model_version, asset_id) 索引
        self._store: dict[tuple[str, str], VectorRecord] = {}

    def ensure_collection(self, model_version: str, dim: int) -> None:
        # 内存库无需建表
        return None

    def upsert(self, record: VectorRecord, model_version: str) -> str:
        point_id = record.point_id or f"{model_version}_{record.asset_id}"
        rec = VectorRecord(
            asset_id=record.asset_id,
            vector=list(record.vector),
            dim=record.dim,
            metadata=dict(record.metadata) if record.metadata else None,
            point_id=point_id,
        )
        self._store[(model_version, record.asset_id)] = rec
        return point_id

    def search(
        self,
        query_vector: Sequence[float],
        *,
        model_version: str,
        top_k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        qv = list(query_vector)
        hits: list[VectorSearchHit] = []
        for (mv, aid), rec in self._store.items():
            if mv != model_version:
                continue
            if filter and not _match_filter(rec.metadata, filter):
                continue
            score = _cosine(qv, list(rec.vector))
            hits.append(
                VectorSearchHit(asset_id=aid, score=score, metadata=rec.metadata)
            )
        hits.sort(key=lambda h: h.score, reverse=True)
        return hits[:top_k]

    def delete(self, asset_id: str, model_version: str) -> None:
        self._store.pop((model_version, asset_id), None)

    def get(self, asset_id: str, model_version: str) -> VectorRecord | None:
        return self._store.get((model_version, asset_id))


def _cosine(a: list[float], b: list[float]) -> float:
    """余弦相似度，零向量返回 0。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _match_filter(metadata: dict[str, Any] | None, filter: dict[str, Any]) -> bool:
    """元数据精确匹配（AND 语义）。"""
    if not metadata:
        return False
    for k, v in filter.items():
        if metadata.get(k) != v:
            return False
    return True


# ---------------------------------------------------------------------------
# Qdrant 实现
# ---------------------------------------------------------------------------


class QdrantVectorStore(VectorStore):
    """基于 Qdrant 的向量库实现。

    通过 qdrant-client 异步或同步访问。每个 model_version 对应一个 collection。
    collection 命名：teamharness_<model_version>，向量维度由 ensure_collection 指定。
    """

    backend_name = "qdrant"

    def __init__(
        self,
        *,
        url: str | None = None,
        api_key: str | None = None,
        host: str | None = None,
        port: int = 6333,
        prefer_grpc: bool = False,
        timeout: float = 30.0,
        client: Any = None,
    ) -> None:
        # client 注入用于测试；生产环境按 url / host 创建
        if client is not None:
            self._client = client
        else:
            try:
                from qdrant_client import QdrantClient
            except ImportError as exc:  # pragma: no cover
                raise ImportError(
                    "QdrantVectorStore 需要 qdrant-client，请安装：pip install qdrant-client"
                ) from exc
            if url:
                self._client = QdrantClient(
                    url=url, api_key=api_key, timeout=timeout, prefer_grpc=prefer_grpc
                )
            else:
                self._client = QdrantClient(
                    host=host or "127.0.0.1", port=port, timeout=timeout, prefer_grpc=prefer_grpc
                )
        self._ensured: set[str] = set()

    @staticmethod
    def _collection_name(model_version: str) -> str:
        return f"teamharness_{model_version}"

    def ensure_collection(self, model_version: str, dim: int) -> None:
        if model_version in self._ensured:
            return
        from qdrant_client.http import models as qm

        name = self._collection_name(model_version)
        try:
            self._client.get_collection(name)
        except Exception:
            self._client.recreate_collection(
                collection_name=name,
                vectors_config=qm.VectorParams(size=dim, distance=qm.Distance.COSINE),
            )
        self._ensured.add(model_version)

    def upsert(self, record: VectorRecord, model_version: str) -> str:
        from qdrant_client.http import models as qm

        self.ensure_collection(model_version, record.dim)
        point_id = record.point_id or str(uuid.uuid5(uuid.NAMESPACE_DNS, f"{model_version}:{record.asset_id}"))
        self._client.upsert(
            collection_name=self._collection_name(model_version),
            points=[
                qm.PointStruct(
                    id=point_id,
                    vector=list(record.vector),
                    payload={"asset_id": record.asset_id, **(record.metadata or {})},
                )
            ],
        )
        return point_id

    def search(
        self,
        query_vector: Sequence[float],
        *,
        model_version: str,
        top_k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        from qdrant_client.http import models as qm

        must = [qm.FieldCondition(key=k, match=qm.MatchValue(value=v)) for k, v in (filter or {}).items()]
        qfilter = qm.Filter(must=must) if must else None
        results = self._client.search(
            collection_name=self._collection_name(model_version),
            query_vector=list(query_vector),
            limit=top_k,
            query_filter=qfilter,
        )
        hits: list[VectorSearchHit] = []
        for r in results:
            payload = dict(r.payload or {})
            asset_id = payload.pop("asset_id", "")
            hits.append(VectorSearchHit(asset_id=asset_id, score=r.score, metadata=payload))
        return hits

    def delete(self, asset_id: str, model_version: str) -> None:
        from qdrant_client.http import models as qm

        self._client.delete(
            collection_name=self._collection_name(model_version),
            points_selector=qm.FilterSelector(
                filter=qm.Filter(must=[qm.FieldCondition(key="asset_id", match=qm.MatchValue(value=asset_id))])
            ),
        )

    def get(self, asset_id: str, model_version: str) -> VectorRecord | None:
        from qdrant_client.http import models as qm

        results, _ = self._client.scroll(
            collection_name=self._collection_name(model_version),
            scroll_filter=qm.Filter(
                must=[qm.FieldCondition(key="asset_id", match=qm.MatchValue(value=asset_id))]
            ),
            limit=1,
            with_vectors=True,
            with_payload=True,
        )
        if not results:
            return None
        point = results[0]
        payload = dict(point.payload or {})
        payload.pop("asset_id", None)
        vector = point.vector or []
        if isinstance(vector, dict):
            # 命名向量场景下 vector 可能是 dict，取第一个
            vector = next(iter(vector.values()), [])
        return VectorRecord(
            asset_id=asset_id,
            vector=list(vector),
            dim=len(vector),
            metadata=payload or None,
            point_id=str(point.id),
        )


# ---------------------------------------------------------------------------
# PGVector 实现
# ---------------------------------------------------------------------------


class PGVectorStore(VectorStore):
    """基于 PostgreSQL + pgvector 扩展的向量库实现。

    与元数据 PG 同库部署，向量存于 asset_embedding 表（models.py）。
    - embedding 字段在 PG 上为 vector(N) 类型，本实现通过 pgvector 适配器转换
    - 元数据（module_path / category）存于 asset_index，检索时 JOIN
    - 单机模式可降级为 InMemoryVectorStore（无 pgvector 时）

    注意：asset_embedding.embedding 字段在 ORM 中为 Text，存储 JSON 序列化的向量。
    本实现直接用 SQLAlchemy + raw SQL 调用 pgvector 操作符 (<=>) 实现余弦距离。
    """

    backend_name = "pgvector"

    def __init__(
        self,
        engine: Any = None,
        *,
        sync_engine: Any = None,
        collection_prefix: str = "teamharness",
    ) -> None:
        # engine 与 sync_engine 同义，前者兼容旧调用
        self._engine = engine or sync_engine
        if self._engine is None:
            raise ValueError("PGVectorStore 需要传入 engine")
        self._collection_prefix = collection_prefix
        self._ensured: set[str] = set()

    def _table_name(self, model_version: str) -> str:
        return f"{self._collection_prefix}_{model_version}".replace("-", "_").replace(".", "_")

    def ensure_collection(self, model_version: str, dim: int) -> None:
        if model_version in self._ensured:
            return
        from sqlalchemy import text

        tbl = self._table_name(model_version)
        with self._engine.begin() as conn:
            # 确保 pgvector 扩展存在
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
            except Exception:
                logger.warning("pgvector 扩展创建失败，PGVectorStore 可能不可用")
                raise
            # DDL 不支持绑定参数，dim 直接插值（来自调用方受信输入）
            conn.execute(
                text(
                    f"CREATE TABLE IF NOT EXISTS {tbl} ("
                    "  point_id TEXT PRIMARY KEY,"
                    "  asset_id TEXT NOT NULL,"
                    f"  embedding vector({dim}) NOT NULL,"
                    "  payload JSONB NOT NULL DEFAULT '{}'::jsonb"
                    ");"
                )
            )
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{tbl}_asset "
                    f"ON {tbl} (asset_id);"
                )
            )
            # ivfflat 索引加速余弦检索（lists = sqrt(N)，这里固定取 100）
            conn.execute(
                text(
                    f"CREATE INDEX IF NOT EXISTS idx_{tbl}_emb "
                    f"ON {tbl} USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);"
                )
            )
        self._ensured.add(model_version)

    def upsert(self, record: VectorRecord, model_version: str) -> str:
        from sqlalchemy import text

        self.ensure_collection(model_version, record.dim)
        tbl = self._table_name(model_version)
        point_id = record.point_id or f"{model_version}_{record.asset_id}"
        vec_str = "[" + ",".join(str(float(x)) for x in record.vector) + "]"
        payload = json.dumps({"asset_id": record.asset_id, **(record.metadata or {})})
        with self._engine.begin() as conn:
            conn.execute(
                text(
                    f"INSERT INTO {tbl} (point_id, asset_id, embedding, payload) "
                    "VALUES (:pid, :aid, :emb::vector, :payload::jsonb) "
                    "ON CONFLICT (point_id) DO UPDATE SET "
                    "  asset_id = EXCLUDED.asset_id, "
                    "  embedding = EXCLUDED.embedding, "
                    "  payload = EXCLUDED.payload;"
                ),
                {"pid": point_id, "aid": record.asset_id, "emb": vec_str, "payload": payload},
            )
        return point_id

    def search(
        self,
        query_vector: Sequence[float],
        *,
        model_version: str,
        top_k: int = 10,
        filter: dict[str, Any] | None = None,
    ) -> list[VectorSearchHit]:
        from sqlalchemy import text

        tbl = self._table_name(model_version)
        vec_str = "[" + ",".join(str(float(x)) for x in query_vector) + "]"
        # 元数据过滤用 payload->>key = value
        where_clauses = []
        params: dict[str, Any] = {"emb": vec_str, "top_k": top_k}
        for i, (k, v) in enumerate((filter or {}).items()):
            where_clauses.append(f"payload->>:k{i} = :v{i}")
            params[f"k{i}"] = k
            params[f"v{i}"] = str(v)
        where_sql = (" AND " + " AND ".join(where_clauses)) if where_clauses else ""
        sql = (
            f"SELECT asset_id, payload, 1 - (embedding <=> :emb::vector) AS score "
            f"FROM {tbl}{where_sql} "
            "ORDER BY embedding <=> :emb::vector ASC "
            "LIMIT :top_k;"
        )
        with self._engine.begin() as conn:
            result = conn.execute(text(sql), params)
            hits: list[VectorSearchHit] = []
            for row in result:
                payload = dict(row.payload or {})
                aid = payload.pop("asset_id", row.asset_id)
                hits.append(
                    VectorSearchHit(asset_id=aid, score=float(row.score), metadata=payload)
                )
            return hits

    def delete(self, asset_id: str, model_version: str) -> None:
        from sqlalchemy import text

        tbl = self._table_name(model_version)
        with self._engine.begin() as conn:
            conn.execute(text(f"DELETE FROM {tbl} WHERE asset_id = :aid;"), {"aid": asset_id})

    def get(self, asset_id: str, model_version: str) -> VectorRecord | None:
        from sqlalchemy import text

        tbl = self._table_name(model_version)
        with self._engine.begin() as conn:
            result = conn.execute(
                text(f"SELECT point_id, embedding::text, payload FROM {tbl} WHERE asset_id = :aid;"),
                {"aid": asset_id},
            )
            row = result.fetchone()
            if row is None:
                return None
            # embedding::text 形如 "[0.1,0.2,...]"
            vec_str = (row.embedding or "").strip("[]")
            vector = [float(x) for x in vec_str.split(",") if x.strip()] if vec_str else []
            payload = dict(row.payload or {})
            payload.pop("asset_id", None)
            return VectorRecord(
                asset_id=asset_id,
                vector=vector,
                dim=len(vector),
                metadata=payload or None,
                point_id=row.point_id,
            )


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_vector_store(*, kind: str | None = None, **kwargs: Any) -> VectorStore:
    """按 VECTOR_BACKEND 环境变量或显式 kind 创建 VectorStore。

    kind 取值：qdrant / pgvector / memory。
    单机模式 / 测试默认 memory。
    """
    kind = (kind or os.environ.get("VECTOR_BACKEND") or "memory").lower()
    if kind == "memory":
        return InMemoryVectorStore()
    if kind == "qdrant":
        return QdrantVectorStore(
            url=kwargs.get("url") or os.environ.get("QDRANT_URL"),
            api_key=kwargs.get("api_key") or os.environ.get("QDRANT_API_KEY"),
            host=kwargs.get("host") or os.environ.get("QDRANT_HOST"),
            port=int(kwargs.get("port") or os.environ.get("QDRANT_PORT", "6333")),
            prefer_grpc=bool(kwargs.get("prefer_grpc", False)),
            timeout=float(kwargs.get("timeout", 30.0)),
            client=kwargs.get("client"),
        )
    if kind == "pgvector":
        return PGVectorStore(
            engine=kwargs.get("engine") or kwargs.get("sync_engine"),
            collection_prefix=kwargs.get("collection_prefix", "teamharness"),
        )
    raise ValueError(f"未知 VectorStore 类型：{kind}")


__all__ = [
    "InMemoryVectorStore",
    "PGVectorStore",
    "QdrantVectorStore",
    "VectorRecord",
    "VectorSearchHit",
    "VectorStore",
    "create_vector_store",
]
