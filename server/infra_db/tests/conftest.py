"""infra_db 域内测试 fixtures。

- 用 SQLite 内存库（StaticPool）避免依赖真实 PG
- InMemoryVectorStore 避免依赖 Qdrant/PGVector
- 提供预初始化 schema 的 Database / AssetIndex / EmbeddingService / OutboxWorker
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest
from sqlalchemy import text

# 测试用 SQLite 内存库（统一同步 / 异步路径都指同一内存库需要 StaticPool）
TEST_DB_SYNC_URL = "sqlite:///:memory:"
TEST_DB_ASYNC_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def database() -> Iterator:
    """提供初始化 schema 的 Database（SQLite 内存库）。"""
    # 清理可能的环境变量
    for k in ("DATABASE_URL", "DATABASE_SYNC_URL", "DB_ECHO"):
        os.environ.pop(k, None)
    from server.infra_db.db import create_database
    from server.infra_db.schema_initializer import init_schema

    db = create_database(sync_url=TEST_DB_SYNC_URL, async_url=TEST_DB_ASYNC_URL)
    init_schema(db.sync_engine, with_pg_ddl=False)
    yield db
    # 清理
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(db.close())
        loop.close()
    except Exception:
        pass


@pytest.fixture
def vector_store():
    """InMemoryVectorStore 实例。"""
    from server.infra_db.vectorstore import InMemoryVectorStore

    return InMemoryVectorStore()


@pytest.fixture
def embedding_service():
    """EmbeddingService（默认哈希 embedding，测试用）。"""
    from server.infra_db.embedding import EmbeddingService

    return EmbeddingService(active_version="v1", shadow_version="", dim=32)


@pytest.fixture
def asset_index(database, embedding_service):
    """AssetIndex 实例（注入 embedding 版本配置）。"""
    from server.infra_db.asset_index import AssetIndex

    return AssetIndex(
        database,
        active_embedding_version=embedding_service.get_active_version(),
        shadow_embedding_version=embedding_service.get_shadow_version(),
    )


@pytest.fixture
def counts_checker(database):
    from server.infra_db.counts_check import CountsChecker

    return CountsChecker(database)


@pytest.fixture
def outbox_worker(database, embedding_service, vector_store):
    """OutboxWorker 实例（不自启动后台线程，测试用 run_once）。"""
    from server.infra_db.outbox import OutboxWorker

    return OutboxWorker(database, embedding_service, vector_store, worker_id="test-worker")


@pytest.fixture
def sync_service_factory(database, asset_index, embedding_service, counts_checker):
    """SyncService 工厂（每个测试可注入不同的 mock GitProvider）。"""
    from server.infra_db.sync import SyncService

    def _factory(git_provider, repo_root: str = "") -> SyncService:
        return SyncService(
            database=database,
            git_provider=git_provider,
            asset_index=asset_index,
            embedding_service=embedding_service,
            counts_checker=counts_checker,
            repo_root=repo_root,
        )

    return _factory


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def make_asset(
    *,
    id: str = "asset-1",
    type: str = "rule",
    owner: str = "tester",
    scope: str = "team",
    module_path: str = "",
    category: str | None = None,
    content: str = "test content",
    git_path: str = "rules/test.md",
    tags: list[str] | None = None,
) -> "AssetVO":
    """构造 Asset VO（测试用）。"""
    from server.common.models import Asset as AssetVO, AssetType, Scope

    return AssetVO(
        id=id,
        type=AssetType(type),
        owner=owner,
        scope=Scope(scope),
        content=content,
        content_file_ref=git_path,
        module_path=module_path,
        category=category,
        tags=tags or [],
    )
