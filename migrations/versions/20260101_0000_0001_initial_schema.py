"""initial schema: DB 派生索引层全表 + PG 专属 DDL

Revision ID: 0001_initial
Revises:
Create Date: 2026-01-01 00:00:00

对应 SubTask 2.1（PG schema 全表）+ SubTask 2.8（Alembic 框架）+
SubTask 2.9（recall_log 按月分区 + 6 个月 TTL）。

包含：
1. 通用 ORM 表（SQLAlchemy metadata.create_all 等价 DDL）
   - asset_index（含 module_path / category / status / 索引）
   - agent_binding（fixed / on-demand，级联删除）
   - module_stats（counts 镜像）
   - recall_log（PG 上为分区表，SQLite 上为普通表）
   - embedding_task_queue（outbox 队列）
   - index_sync_state（singleton 单行表）
   - adoption_event（采纳事件）
   - asset_embedding（PGVector 向量存储）
   - distillation_job（二级提炼任务占位）
2. PG 专属 DDL（仅 PG 执行）
   - recall_log RANGE 分区（按月）
   - asset_recall_stats 物化视图 + 唯一索引
   - create_recall_partition / drop_old_recall_partitions 函数（6 个月 TTL）
3. 初始月度分区（当月 + 下月，保证立即可写入）
4. index_sync_state 单行记录初始化
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgresql() -> bool:
    bind = op.get_bind()
    return bind.dialect.name in ("postgresql", "psycopg", "psycopg2")


def upgrade() -> None:
    # 1. 通用表（与 ORM models.py 一致）
    op.create_table(
        "asset_index",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("type", sa.String(16), nullable=False),
        sa.Column("owner", sa.String(64), nullable=False),
        sa.Column("scope", sa.String(16), nullable=False, server_default="team"),
        sa.Column("content_hash", sa.String(64), nullable=True),
        sa.Column("embedding_id", sa.String(128), nullable=True),
        sa.Column("active_embedding_version", sa.String(32), nullable=True),
        sa.Column("version", sa.String(32), nullable=False, server_default="0.0.1"),
        sa.Column("tags", sa.Text, nullable=False, server_default=""),
        sa.Column("related_to", sa.Text, nullable=False, server_default=""),
        sa.Column("git_path", sa.String(512), nullable=False),
        sa.Column("git_commit", sa.String(64), nullable=False),
        sa.Column("module_path", sa.String(256), nullable=False, server_default=""),
        sa.Column("category", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("schema_version", sa.Integer, nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("content_snapshot", sa.Text, nullable=False, server_default=""),
        sa.UniqueConstraint("id", name="uq_asset_index_id"),
    )
    op.create_index("idx_asset_module", "asset_index", ["module_path"])
    op.create_index("idx_asset_category", "asset_index", ["category"])
    op.create_index("idx_asset_status_type", "asset_index", ["status", "type"])
    op.create_index("idx_asset_owner_scope", "asset_index", ["owner", "scope"])
    op.create_index("idx_asset_index_type", "asset_index", ["type"])
    op.create_index("idx_asset_index_owner", "asset_index", ["owner"])
    op.create_index("idx_asset_index_content_hash", "asset_index", ["content_hash"])
    op.create_index("idx_asset_index_embedding_id", "asset_index", ["embedding_id"])
    op.create_index("idx_asset_index_git_commit", "asset_index", ["git_commit"])

    op.create_table(
        "agent_binding",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("agent_role", sa.String(32), nullable=False, server_default=""),
        sa.Column("asset_id", sa.String(64), sa.ForeignKey("asset_index.id", ondelete="CASCADE"), nullable=False),
        sa.Column("binding_type", sa.String(16), nullable=False, server_default="on-demand"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("enabled", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("agent_id", "asset_id", name="uq_agent_asset"),
    )
    op.create_index("idx_agent_binding_agent_id", "agent_binding", ["agent_id"])
    op.create_index("idx_agent_binding_asset_id", "agent_binding", ["asset_id"])
    op.create_index("idx_agent_binding_enabled", "agent_binding", ["enabled"])
    op.create_index("idx_binding_agent_enabled", "agent_binding", ["agent_id", "enabled"])
    op.create_index("idx_binding_type", "agent_binding", ["binding_type"])

    op.create_table(
        "module_stats",
        sa.Column("module_path", sa.String(256), primary_key=True),
        sa.Column("declared_asset_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("declared_submodule_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("actual_asset_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("actual_submodule_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("counts_consistent", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_synced_commit", sa.String(64), nullable=False, server_default=""),
    )

    # recall_log：SQLite 用普通表；PG 用分区表（下面 _init_pg_specific 覆盖）
    op.create_table(
        "recall_log",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("recalled_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("module_path", sa.String(256), nullable=False, server_default=""),
        sa.Column("query", sa.Text, nullable=False, server_default=""),
        sa.Column("relevance_score", sa.Float, nullable=True),
        sa.Column("trace_id", sa.String(64), nullable=True),
    )
    op.create_index("idx_recall_log_asset_id", "recall_log", ["asset_id"])
    op.create_index("idx_recall_log_agent_id", "recall_log", ["agent_id"])
    op.create_index("idx_recall_log_recalled_at", "recall_log", ["recalled_at"])
    op.create_index("idx_recall_log_module_path", "recall_log", ["module_path"])
    op.create_index("idx_recall_log_asset_time", "recall_log", ["asset_id", "recalled_at"])
    op.create_index("idx_recall_log_module_time", "recall_log", ["module_path", "recalled_at"])

    op.create_table(
        "embedding_task_queue",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("task_type", sa.String(16), nullable=False, server_default="upsert"),
        sa.Column("model_version", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("status", sa.String(24), nullable=False, server_default="pending"),
        sa.Column("leased_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("lease_owner", sa.String(64), nullable=True),
        sa.Column("embedding_id", sa.String(128), nullable=True),
        sa.Column("retry_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_retries", sa.Integer, nullable=False, server_default="3"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_emb_queue_status", "embedding_task_queue", ["status"])
    op.create_index("idx_emb_queue_status_created", "embedding_task_queue", ["status", "created_at"])
    op.create_index("idx_emb_queue_asset", "embedding_task_queue", ["asset_id"])
    op.create_index("idx_emb_queue_created_at", "embedding_task_queue", ["created_at"])

    op.create_table(
        "index_sync_state",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("last_synced_commit", sa.String(64), nullable=False, server_default=""),
        sa.Column("last_synced_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("status", sa.String(16), nullable=False, server_default="ok"),
        sa.Column("last_error", sa.Text, nullable=True),
        sa.Column("lag_periods", sa.Integer, nullable=False, server_default="0"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )

    op.create_table(
        "adoption_event",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("member_id", sa.String(64), nullable=False),
        sa.Column("event_type", sa.String(16), nullable=False),
        sa.Column("stale", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("payload", sa.Text, nullable=False, server_default=""),
    )
    op.create_index("idx_adoption_event_asset_id", "adoption_event", ["asset_id"])
    op.create_index("idx_adoption_event_member_id", "adoption_event", ["member_id"])
    op.create_index("idx_adoption_event_event_type", "adoption_event", ["event_type"])
    op.create_index("idx_adoption_event_stale", "adoption_event", ["stale"])
    op.create_index("idx_adoption_event_occurred_at", "adoption_event", ["occurred_at"])
    op.create_index("idx_adoption_asset_event", "adoption_event", ["asset_id", "event_type"])

    op.create_table(
        "asset_embedding",
        sa.Column("id", sa.BigInteger, primary_key=True, autoincrement=True),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("model_version", sa.String(32), nullable=False, server_default="v1"),
        sa.Column("embedding", sa.Text, nullable=False),
        sa.Column("dim", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("asset_id", "model_version", name="uq_asset_model_version"),
    )
    op.create_index("idx_asset_embedding_asset_id", "asset_embedding", ["asset_id"])
    op.create_index("idx_asset_embedding_model_version", "asset_embedding", ["model_version"])
    op.create_index("idx_embedding_asset_model", "asset_embedding", ["asset_id", "model_version"])

    op.create_table(
        "distillation_job",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("trigger_source", sa.String(32), nullable=False, server_default="incremental"),
        sa.Column("cluster_fingerprint", sa.String(128), nullable=False, server_default=""),
        sa.Column("snapshot_commit", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("score", sa.Float, nullable=True),
        sa.Column("input_asset_ids", sa.Text, nullable=False, server_default=""),
        sa.Column("output_prompt_id", sa.String(64), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_distillation_job_status", "distillation_job", ["status"])
    op.create_index("idx_distill_job_cluster", "distillation_job", ["cluster_fingerprint"])

    # 2. 初始化 index_sync_state 单行
    op.execute(
        "INSERT INTO index_sync_state (id, last_synced_commit, status, lag_periods) "
        "VALUES ('singleton', '', 'ok', 0)"
    )

    # 3. PG 专属 DDL（分区表 + 物化视图 + TTL 函数）
    if _is_postgresql():
        _init_pg_specific()


def _init_pg_specific() -> None:
    """PG 专属：recall_log 分区表 + 物化视图 + TTL 函数 + 初始月度分区。"""
    # 替换 recall_log 为分区表
    op.execute("DROP TABLE IF EXISTS recall_log CASCADE;")
    op.execute(
        """
        CREATE TABLE recall_log (
            id BIGSERIAL,
            asset_id VARCHAR(64) NOT NULL,
            agent_id VARCHAR(64) NOT NULL,
            recalled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            module_path VARCHAR(256) NOT NULL DEFAULT '',
            query TEXT NOT NULL DEFAULT '',
            relevance_score FLOAT,
            trace_id VARCHAR(64)
        ) PARTITION BY RANGE (recalled_at);
        """
    )
    op.execute(
        "CREATE INDEX idx_recall_log_asset_time ON recall_log (asset_id, recalled_at);"
    )
    op.execute(
        "CREATE INDEX idx_recall_log_module_time ON recall_log (module_path, recalled_at);"
    )
    op.execute("CREATE INDEX idx_recall_log_recalled_at ON recall_log (recalled_at);")

    # 创建当月与下月分区
    now = datetime.now(timezone.utc)
    _create_monthly_partition(now.year, now.month)
    next_year, next_month = (now.year + 1, 1) if now.month == 12 else (now.year, now.month + 1)
    _create_monthly_partition(next_year, next_month)

    # asset_recall_stats 物化视图 + 唯一索引
    op.execute(
        """
        CREATE MATERIALIZED VIEW asset_recall_stats AS
        SELECT
            asset_id,
            COUNT(*) AS recall_count,
            COUNT(DISTINCT agent_id) AS unique_agent_count,
            MAX(recalled_at) AS last_recalled_at,
            MIN(recalled_at) AS first_recalled_at
        FROM recall_log
        GROUP BY asset_id
        WITH DATA;
        """
    )
    op.execute(
        "CREATE UNIQUE INDEX idx_asset_recall_stats_asset ON asset_recall_stats (asset_id);"
    )

    # TTL 函数
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION create_recall_partition(month_start timestamptz)
        RETURNS void AS $$
        DECLARE
            part_name text;
            next_month timestamptz;
        BEGIN
            part_name := 'recall_log_' || to_char(month_start, 'YYYYMM');
            next_month := month_start + INTERVAL '1 month';
            EXECUTE format(
                'CREATE TABLE IF NOT EXISTS %I PARTITION OF recall_log FOR VALUES FROM (%L) TO (%L)',
                part_name, month_start, next_month
            );
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        r"""
        CREATE OR REPLACE FUNCTION drop_old_recall_partitions(months_to_keep int DEFAULT 6)
        RETURNS int AS $$
        DECLARE
            dropped int := 0;
            part record;
            cutoff timestamptz := date_trunc('month', NOW() - (months_to_keep || ' months')::interval);
        BEGIN
            FOR part IN
                SELECT tablename FROM pg_tables
                WHERE tablename LIKE 'recall_log_%'
                  AND tablename ~ '^recall_log_[0-9]{6}$'
            LOOP
                IF substring(part.tablename from '[0-9]{6}$')::text < to_char(cutoff, 'YYYYMM') THEN
                    EXECUTE format('DROP TABLE IF EXISTS %I', part.tablename);
                    dropped := dropped + 1;
                END IF;
            END LOOP;
            RETURN dropped;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def _create_monthly_partition(year: int, month: int) -> None:
    """创建月度分区。"""
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    op.execute(
        f"""
        CREATE TABLE IF NOT EXISTS recall_log_{year}{month:02d}
            PARTITION OF recall_log
            FOR VALUES FROM ('{year}-{month:02d}-01 00:00:00+00')
            TO ('{next_year}-{next_month:02d}-01 00:00:00+00');
        """
    )


def downgrade() -> None:
    if _is_postgresql():
        op.execute("DROP MATERIALIZED VIEW IF EXISTS asset_recall_stats;")
        op.execute("DROP FUNCTION IF EXISTS drop_old_recall_partitions(int);")
        op.execute("DROP FUNCTION IF EXISTS create_recall_partition(timestamptz);")
    op.drop_table("distillation_job")
    op.drop_table("asset_embedding")
    op.drop_table("adoption_event")
    op.drop_table("index_sync_state")
    op.drop_table("embedding_task_queue")
    op.drop_table("recall_log")
    op.drop_table("module_stats")
    op.drop_table("agent_binding")
    op.drop_table("asset_index")
