"""distill_team 域内测试 fixtures。

复用 infra_db/tests/conftest.py 的 database / asset_index / embedding_service / vector_store
fixture 定义（避免跨包 conftest 不可见）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest


# ---------------------------------------------------------------------------
# 复用 infra_db fixture 定义（database / vector_store / embedding_service / asset_index）
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


# ---------------------------------------------------------------------------
# distill_team 专属 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def distill_repo(tmp_path: Path) -> Path:
    """带 DREAMS 目录与 prompts/seeds 目录的测试仓库。"""
    repo = tmp_path / "distill_repo"
    (repo / "DREAMS").mkdir(parents=True, exist_ok=True)
    (repo / "prompts" / "seeds").mkdir(parents=True, exist_ok=True)
    return repo


@pytest.fixture
def head_resolver_factory():
    """返回一个可配置的 head_resolver 工厂。

    用法：
        resolver = head_resolver_factory()
        resolver.set_head("abc")
        # head_resolver 调用时返回当前 head
    """

    class _Resolver:
        def __init__(self) -> None:
            self._head = "initial-commit-sha"

        def __call__(self) -> str:
            return self._head

        def set_head(self, sha: str) -> None:
            self._head = sha

    return _Resolver()


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
):
    """构造 Asset VO。"""
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
    )


def upsert_asset(
    asset_index,
    *,
    id: str,
    owner: str,
    module_path: str = "",
    category: str | None = None,
    content: str = "# test",
    git_path: str | None = None,
    git_commit: str = "commit-1",
    tags: list[str] | None = None,
    type: str = "rule",
    scope: str = "team",
    is_convention: bool = False,
) -> str:
    """便捷写入资产到 asset_index。

    is_convention=True 时在 content 头部加 frontmatter 标记。
    """
    if is_convention:
        content = (
            "---\nid: " + id + "\ntype: " + type + "\nis_convention: true\n---\n" + content
        )
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
    )
    return asset_index.upsert(asset, git_commit=git_commit, content_snapshot=content)


@pytest.fixture
def asset_factory():
    """返回 make_asset_vo 工厂函数。"""
    return make_asset_vo


@pytest.fixture
def upsert_helper():
    """返回 upsert_asset 辅助函数。"""
    return upsert_asset
