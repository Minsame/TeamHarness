"""governance 域内测试 fixtures。

复用 infra_db/tests/conftest.py 的 database / vector_store / embedding_service /
asset_index fixture 定义（避免跨包 conftest 不可见）。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# 复用 infra_db fixture 定义
# ---------------------------------------------------------------------------


TEST_DB_SYNC_URL = "sqlite:///:memory:"
TEST_DB_ASYNC_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def database() -> Iterator:
    """提供初始化 schema 的 Database（SQLite 内存库）。"""
    for k in ("DATABASE_URL", "DATABASE_SYNC_URL", "DB_ECHO"):
        os.environ.pop(k, None)
    from server.infra_db.db import create_database
    from server.infra_db.schema_initializer import init_schema

    db = create_database(sync_url=TEST_DB_SYNC_URL, async_url=TEST_DB_ASYNC_URL)
    init_schema(db.sync_engine, with_pg_ddl=False)
    yield db
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(db.close())
        loop.close()
    except Exception:
        pass


@pytest.fixture
def vector_store():
    from server.infra_db.vectorstore import InMemoryVectorStore

    return InMemoryVectorStore()


@pytest.fixture
def embedding_service():
    from server.infra_db.embedding import EmbeddingService

    return EmbeddingService(active_version="v1", shadow_version="", dim=32)


@pytest.fixture
def asset_index(database, embedding_service):
    from server.infra_db.asset_index import AssetIndex

    return AssetIndex(
        database,
        active_embedding_version=embedding_service.get_active_version(),
        shadow_embedding_version=embedding_service.get_shadow_version(),
    )


@pytest.fixture
def archive_root(tmp_path: Path) -> Path:
    """归档目录（每个测试独立）。"""
    return tmp_path / "archive"


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def make_asset_vo(
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
    version: str = "0.0.1",
    content_hash: str | None = None,
):
    """构造 Asset VO。

    content_hash 显式传入时写入 VO，未传则由 AssetIndex.upsert 内部按内容计算。
    """
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
        version=version,
        content_hash=content_hash,
    )


def upsert_asset(
    asset_index,
    *,
    id: str,
    owner: str = "tester",
    module_path: str = "",
    category: str | None = None,
    content: str = "# test",
    git_path: str | None = None,
    git_commit: str = "commit-1",
    tags: list[str] | None = None,
    type: str = "rule",
    scope: str = "team",
    content_hash: str | None = None,
) -> str:
    """便捷写入资产到 asset_index。

    content_hash 显式传入时写入 VO（用于 content_hash 精确匹配测试）；
    未传则由 AssetIndex.upsert 内部按内容计算。
    """
    asset = make_asset_vo(
        id=id,
        type=type,
        owner=owner,
        scope=scope,
        module_path=module_path,
        category=category,
        content=content,
        git_path=git_path or f"rules/{id}.md",
        tags=tags,
        content_hash=content_hash,
    )
    return asset_index.upsert(
        asset, git_commit=git_commit, content_snapshot=content
    )


def write_recall_log(
    database,
    *,
    asset_id: str,
    agent_id: str = "agent-1",
    module_path: str = "",
    query: str = "test",
    days_ago: int = 0,
):
    """写入一条 recall_log（用于采纳率/召回命中率测试）。"""
    from server.infra_db.models import RecallLog

    recalled_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with database.session() as sess:
        sess.add(
            RecallLog(
                asset_id=asset_id,
                agent_id=agent_id,
                recalled_at=recalled_at,
                module_path=module_path,
                query=query,
                relevance_score=0.9,
                trace_id="trace-test",
            )
        )


def write_adoption_event(
    database,
    *,
    asset_id: str,
    member_id: str = "alice",
    event_type: str = "recall",
    days_ago: int = 0,
    payload: dict | None = None,
):
    """写入一条 adoption_event（用于 stale 标记测试）。"""
    import json

    from server.infra_db.models import AdoptionEvent

    occurred_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    with database.session() as sess:
        sess.add(
            AdoptionEvent(
                asset_id=asset_id,
                member_id=member_id,
                event_type=event_type,
                stale=False,
                occurred_at=occurred_at,
                received_at=occurred_at,
                payload=json.dumps(payload or {}),
            )
        )


@pytest.fixture
def asset_factory():
    """返回 make_asset_vo 工厂函数。"""
    return make_asset_vo


@pytest.fixture
def upsert_helper():
    """返回 upsert_asset 辅助函数。"""
    return upsert_asset


@pytest.fixture
def recall_log_helper():
    """返回 write_recall_log 辅助函数。"""
    return write_recall_log


@pytest.fixture
def adoption_event_helper():
    """返回 write_adoption_event 辅助函数。"""
    return write_adoption_event
