"""Schema 初始化工具：在任意 DB 后端创建表结构。

- 通用表（asset_index / agent_binding / ...）：用 SQLAlchemy metadata.create_all
- PG 专属 DDL（分区表 / 物化视图 / TTL 函数）：检测后端为 PG 时执行
- SQLite（测试）：跳过 PG 专属 DDL，recall_log 退化为普通表

调用方式：
    db = create_database(sync_url="sqlite:///./test.db")
    init_schema(db.sync_engine)

Alembic 迁移（SubTask 2.8）是生产路径，本工具用于测试 / 冷启动兜底。
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

from server.infra_db.models import Base, IndexSyncState
from server.infra_db.schema import (
    RECALL_LOG_PARTITION_DDL,
    REFRESH_ASSET_RECALL_STATS_SQL,
    RECALL_TTL_FUNCTIONS_SQL,
    ASSET_RECALL_STATS_MV_DDL,
    make_monthly_partition_sql,
)

logger = logging.getLogger(__name__)


def is_postgresql(engine: Engine) -> bool:
    """检测当前 engine 是否为 PostgreSQL 后端。"""
    return engine.dialect.name in ("postgresql", "psycopg", "psycopg2", "asyncpg")


def init_schema(engine: Engine, *, with_pg_ddl: bool = True) -> None:
    """在指定 engine 上初始化全部 schema。

    - SQLAlchemy ORM 表统一用 create_all 创建
    - PG 后端额外执行分区表 + 物化视图 + TTL 函数 DDL
    - SQLite 后端跳过 PG 专属 DDL（recall_log 为普通表）
    """
    if with_pg_ddl and is_postgresql(engine):
        _init_pg_specific(engine)
    # ORM 表创建（PG 已创建 recall_log 分区表会被 create_all 跳过 IF NOT EXISTS）
    Base.metadata.create_all(engine)
    _ensure_singleton_sync_state(engine)


def _init_pg_specific(engine: Engine) -> None:
    """PG 专属 DDL：分区表 / 物化视图 / TTL 函数。"""
    with engine.begin() as conn:
        # recall_log 分区表（覆盖 ORM 创建的普通表）
        # 先 drop ORM 默认创建的普通表（如果已存在），再创建分区表
        inspector = inspect(conn)
        if "recall_log" in inspector.get_table_names():
            conn.execute(text("DROP TABLE IF EXISTS recall_log CASCADE;"))
        conn.execute(text(RECALL_LOG_PARTITION_DDL))
        # 创建当月与下月分区（保证当前可写入）
        now = datetime.now(timezone.utc)
        conn.execute(text(make_monthly_partition_sql(now.year, now.month)))
        next_month = now.month + 1 if now.month < 12 else 1
        next_year = now.year if now.month < 12 else now.year + 1
        conn.execute(text(make_monthly_partition_sql(next_year, next_month)))
        # 物化视图 + 唯一索引
        conn.execute(text(ASSET_RECALL_STATS_MV_DDL))
        # TTL 函数
        conn.execute(text(RECALL_TTL_FUNCTIONS_SQL))


def _ensure_singleton_sync_state(engine: Engine) -> None:
    """确保 index_sync_state 单行记录存在。

    updated_at 为 NOT NULL 且仅有 Python 端 default（无 server_default），
    原始 SQL INSERT 不会触发 ORM default，故显式写入 CURRENT_TIMESTAMP。
    last_synced_at 可空，初始不写入（保持 NULL）。
    """
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO index_sync_state "
                "(id, last_synced_commit, status, lag_periods, updated_at) "
                "VALUES ('singleton', '', 'ok', 0, CURRENT_TIMESTAMP) "
                "ON CONFLICT (id) DO NOTHING;"
            )
        )


def refresh_asset_recall_stats(engine: Engine) -> None:
    """刷新 asset_recall_stats 物化视图（PG 专属，治理看板数据源）。"""
    if not is_postgresql(engine):
        return
    with engine.begin() as conn:
        conn.execute(text(REFRESH_ASSET_RECALL_STATS_SQL))


def run_recall_ttl(engine: Engine, months_to_keep: int = 6) -> int:
    """执行 recall_log 6 个月 TTL，返回删除的分区数。"""
    if not is_postgresql(engine):
        return 0
    with engine.begin() as conn:
        result = conn.execute(
            text("SELECT drop_old_recall_partitions(:months);"),
            {"months": months_to_keep},
        )
        return int(result.scalar() or 0)


__all__ = [
    "init_schema",
    "is_postgresql",
    "refresh_asset_recall_stats",
    "run_recall_ttl",
]
