"""infra_db 领域包：DB 派生索引层基础。

对应技术方案 Agent 2 职责：
- PG schema（asset_index / agent_binding / module_stats / recall_log 分区 /
  embedding_task_queue / index_sync_state / adoption_event / asset_recall_stats 物化视图）
- VectorStore Provider 抽象（Qdrant / PGVector 两实现）
- webhook 同步处理（读 INDEX.md 增量扫描 + embedding 计算）
- outbox 模式（asset_index + embedding_task_queue 同事务，异步 worker 写向量库）
- reconciliation cron（每 5 分钟补偿 webhook 丢失，commit SHA 幂等）
- 对账任务（每小时补偿 embedding_id IS NULL）
- embedding 模型双写过渡（新旧两表 + active_embedding_version + RRF 融合）
- Alembic 迁移框架
- recall_log 按月分区 + 6 个月 TTL
- INDEX.md counts 服务端校验（不一致告警，不阻断）

对外提供占位 API 契约（依赖方：Agent 4/5/7/8/9）：
- AssetIndex: upsert / delete / query / get_status
- EmbeddingService: embed / embed_batch / get_active_version
- SyncService: trigger_sync / get_sync_status / reconcile
"""

from server.infra_db.asset_index import AssetIndex
from server.infra_db.db import Database, create_database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.sync import SyncService
from server.infra_db.vectorstore import (
    InMemoryVectorStore,
    PGVectorStore,
    QdrantVectorStore,
    VectorStore,
)

__all__ = [
    "AssetIndex",
    "Database",
    "EmbeddingService",
    "InMemoryVectorStore",
    "PGVectorStore",
    "QdrantVectorStore",
    "SyncService",
    "VectorStore",
    "create_database",
]
