"""Alembic 迁移环境（SubTask 2.8）。

- 从 DATABASE_SYNC_URL 环境变量读取连接（覆盖 alembic.ini 的 sqlalchemy.url）
- 使用 server.infra_db.models.Base.metadata 作为 target_metadata
- PG 后端额外执行 PG 专属 DDL（分区表 / 物化视图 / TTL 函数）
- SQLite 后端跳过 PG 专属 DDL（recall_log 为普通表）
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# 确保 server 包可被导入
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from server.infra_db.models import Base  # noqa: E402

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# 从环境变量覆盖 sqlalchemy.url（避免在 alembic.ini 硬编码凭据）
db_url = os.environ.get("DATABASE_SYNC_URL")
if db_url:
    config.set_main_option("sqlalchemy.url", db_url)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """离线模式：生成 SQL 脚本不连库。"""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """在线模式：连库执行迁移。"""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        is_postgresql = connection.dialect.name in ("postgresql", "psycopg", "psycopg2")
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            # PG 专属 DDL 通过 migration 文件中的 op.execute() 触发，
            # 这里只控制 SQLAlchemy autogenerate 的输出
        )
        with context.begin_transaction():
            context.run_migrations()
            # PG 专属 DDL 由各 revision 中的 op.execute() 处理，
            # env.py 不重复执行，保持迁移可追溯。


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
