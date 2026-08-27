"""数据库连接与会话管理。

提供同步与异步两套 SQLAlchemy 引擎封装：
- 异步引擎：生产路径（asyncpg 驱动，PG）
- 同步引擎：测试 / 管理脚本路径（aiosqlite 不能用于同步，故同步用 sqlite）

设计要点：
- DB 派生索引层为可重建数据，从 git 仓库派生
- session 范围与 outbox 事务边界对齐：asset_index + embedding_task_queue 必须同事务
- 通过 `Database.session()` 上下文管理器统一会话语义，避免泄漏
- 测试场景使用 SQLite + StaticPool，保证内存库可跨连接共享
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import Session, sessionmaker


class Database:
    """数据库封装，持有同步与异步引擎 + 会话工厂。

    生产环境用 PostgreSQL（asyncpg 异步 / psycopg 同步），
    测试环境可用 SQLite（aiosqlite 异步 / sqlite 同步）。
    """

    def __init__(
        self,
        *,
        async_engine: AsyncEngine | None = None,
        sync_engine: Engine | None = None,
        echo: bool = False,
    ) -> None:
        self._async_engine = async_engine
        self._sync_engine = sync_engine
        self._echo = echo
        self._async_session_factory: async_sessionmaker[AsyncSession] | None = None
        self._sync_session_factory: sessionmaker[Session] | None = None

    # ------------------------------------------------------------------
    # 引擎配置
    # ------------------------------------------------------------------

    @property
    def async_engine(self) -> AsyncEngine:
        if self._async_engine is None:
            raise RuntimeError("异步引擎未初始化，请先 configure_async")
        return self._async_engine

    @property
    def sync_engine(self) -> Engine:
        if self._sync_engine is None:
            raise RuntimeError("同步引擎未初始化，请先 configure_sync")
        return self._sync_engine

    def configure_async(self, url: str, **kwargs: object) -> None:
        """配置异步引擎。url 例：postgresql+asyncpg://user:pwd@host/db。"""
        kwargs.setdefault("echo", self._echo)
        # SQLite 内存库需 StaticPool 以跨连接共享数据
        if url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            kwargs.setdefault("poolclass", StaticPool)
            kwargs.setdefault("connect_args", {"check_same_thread": False})
        self._async_engine = create_async_engine(url, **kwargs)  # type: ignore[arg-type]
        self._async_session_factory = async_sessionmaker(
            self._async_engine, expire_on_commit=False
        )

    def configure_sync(self, url: str, **kwargs: object) -> None:
        """配置同步引擎。url 例：postgresql+psycopg://user:pwd@host/db。"""
        kwargs.setdefault("echo", self._echo)
        if url.startswith("sqlite"):
            from sqlalchemy.pool import StaticPool

            kwargs.setdefault("poolclass", StaticPool)
            kwargs.setdefault("connect_args", {"check_same_thread": False})
        self._sync_engine = create_engine(url, **kwargs)  # type: ignore[arg-type]
        self._sync_session_factory = sessionmaker(
            self._sync_engine, expire_on_commit=False
        )

    # ------------------------------------------------------------------
    # 会话获取
    # ------------------------------------------------------------------

    def async_session(self) -> AsyncSession:
        """获取异步会话（调用方负责关闭，建议用 async with）。"""
        if self._async_session_factory is None:
            raise RuntimeError("异步会话工厂未初始化")
        return self._async_session_factory()

    @contextmanager
    def session(self) -> Iterator[Session]:
        """同步会话上下文管理器，自动提交/回滚。

        outbox 模式要求 asset_index + embedding_task_queue 同事务，
        调用方在该上下文内连续写两表，统一提交。
        """
        if self._sync_session_factory is None:
            raise RuntimeError("同步会话工厂未初始化")
        sess = self._sync_session_factory()
        try:
            yield sess
            sess.commit()
        except Exception:
            sess.rollback()
            raise
        finally:
            sess.close()

    async def close(self) -> None:
        if self._async_engine is not None:
            await self._async_engine.dispose()
        if self._sync_engine is not None:
            self._sync_engine.dispose()


def create_database(
    *,
    async_url: str | None = None,
    sync_url: str | None = None,
    echo: bool | None = None,
) -> Database:
    """按环境变量创建 Database 实例。

    优先级：显式参数 > 环境变量 > 默认 SQLite 内存库（测试友好）。
    """
    echo = echo if echo is not None else os.environ.get("DB_ECHO", "").lower() in (
        "1",
        "true",
        "yes",
    )
    async_url = async_url or os.environ.get(
        "DATABASE_URL", "sqlite+aiosqlite:///:memory:"
    )
    sync_url = sync_url or os.environ.get(
        "DATABASE_SYNC_URL", "sqlite:///:memory:"
    )
    db = Database(echo=echo)
    db.configure_async(async_url)
    db.configure_sync(sync_url)
    return db
