"""SubTask 2.1 / 2.9：schema 全表创建 + 分区 DDL 校验。"""

from __future__ import annotations

import pytest
from sqlalchemy import inspect, text

from server.infra_db.models import (
    AdoptionEvent,
    AgentBinding,
    AssetIndex,
    DistillationJob,
    EmbeddingTaskQueue,
    EmbeddingVector,
    IndexSyncState,
    ModuleStats,
    RecallLog,
)
from server.infra_db.schema import (
    ASSET_RECALL_STATS_MV_DDL,
    RECALL_LOG_PARTITION_DDL,
    RECALL_TTL_FUNCTIONS_SQL,
    make_drop_partition_sql,
    make_monthly_partition_sql,
)


REQUIRED_TABLES = [
    "asset_index",
    "agent_binding",
    "module_stats",
    "recall_log",
    "embedding_task_queue",
    "index_sync_state",
    "adoption_event",
    "asset_embedding",
    "distillation_job",
]


def test_all_tables_created(database):
    """全部 9 张表 + index_sync_state singleton 应可创建。"""
    inspector = inspect(database.sync_engine)
    actual = set(inspector.get_table_names())
    for tbl in REQUIRED_TABLES:
        assert tbl in actual, f"表缺失：{tbl}"


def test_asset_index_has_module_path_category_status(database):
    """asset_index 必须含 module_path / category / status 字段。"""
    inspector = inspect(database.sync_engine)
    columns = {c["name"] for c in inspector.get_columns("asset_index")}
    assert "module_path" in columns
    assert "category" in columns
    assert "status" in columns
    assert "active_embedding_version" in columns
    assert "embedding_id" in columns
    assert "content_snapshot" in columns


def test_asset_index_indexes(database):
    """asset_index 必须建关键索引（module_path / category / status+type）。"""
    inspector = inspect(database.sync_engine)
    indexes = {idx["name"] for idx in inspector.get_indexes("asset_index")}
    assert "idx_asset_module" in indexes
    assert "idx_asset_category" in indexes
    assert "idx_asset_status_type" in indexes


def test_agent_binding_unique_constraint(database):
    """agent_binding (agent_id, asset_id, binding_version) 唯一约束。

    SubTask 5.8 写时复制：唯一键含 binding_version，允许同一资产不同版本多行共存。
    """
    inspector = inspect(database.sync_engine)
    constraints = {
        c["name"] for c in inspector.get_unique_constraints("agent_binding")
    }
    assert "uq_agent_asset_version" in constraints


def test_index_sync_state_singleton_initialized(database):
    """index_sync_state 单行记录应在 init_schema 后存在。"""
    with database.session() as sess:
        state = sess.get(IndexSyncState, "singleton")
        assert state is not None
        assert state.last_synced_commit == ""
        assert state.status == "ok"
        assert state.lag_periods == 0


# ---------------------------------------------------------------------------
# SubTask 2.9：recall_log 按月分区 + 6 个月 TTL（PG 专属 DDL 校验）
# ---------------------------------------------------------------------------


def test_recall_log_partition_ddl_syntax():
    """recall_log 分区表 DDL 应为 PARTITION BY RANGE 形式。"""
    assert "PARTITION BY RANGE (recalled_at)" in RECALL_LOG_PARTITION_DDL
    assert "CREATE TABLE" in RECALL_LOG_PARTITION_DDL


def test_monthly_partition_sql_format():
    """月度分区 SQL 应含 PARTITION OF 与时间范围。"""
    sql = make_monthly_partition_sql(2026, 1)
    assert "recall_log_202601" in sql
    assert "PARTITION OF recall_log" in sql
    assert "2026-01-01 00:00:00+00" in sql
    assert "2026-02-01 00:00:00+00" in sql


def test_monthly_partition_sql_december_rollover():
    """12 月分区应正确滚动到次年 1 月。"""
    sql = make_monthly_partition_sql(2026, 12)
    assert "recall_log_202612" in sql
    assert "2027-01-01 00:00:00+00" in sql


def test_drop_partition_sql():
    """删除分区 SQL 应含 DROP TABLE。"""
    sql = make_drop_partition_sql(2025, 6)
    assert "DROP TABLE IF EXISTS recall_log_202506" in sql


def test_asset_recall_stats_mv_ddl():
    """asset_recall_stats 物化视图 DDL 应含聚合字段。"""
    assert "CREATE MATERIALIZED VIEW" in ASSET_RECALL_STATS_MV_DDL
    assert "recall_count" in ASSET_RECALL_STATS_MV_DDL
    assert "unique_agent_count" in ASSET_RECALL_STATS_MV_DDL


def test_ttl_functions_sql():
    """TTL 函数应含 create_recall_partition 与 drop_old_recall_partitions。"""
    assert "CREATE OR REPLACE FUNCTION create_recall_partition" in RECALL_TTL_FUNCTIONS_SQL
    assert "CREATE OR REPLACE FUNCTION drop_old_recall_partitions" in RECALL_TTL_FUNCTIONS_SQL
    assert "months_to_keep" in RECALL_TTL_FUNCTIONS_SQL


def test_recall_log_writable_on_sqlite(database):
    """SQLite（无分区）下 recall_log 仍可写入与查询。"""
    from datetime import datetime, timezone

    with database.session() as sess:
        log = RecallLog(
            asset_id="asset-1",
            agent_id="agent-1",
            module_path="modules/backend",
            query="test query",
            recalled_at=datetime.now(timezone.utc),
        )
        sess.add(log)
    # 验证写入
    with database.session() as sess:
        from sqlalchemy import select

        rows = list(sess.scalars(select(RecallLog).where(RecallLog.asset_id == "asset-1")))
        assert len(rows) == 1
        assert rows[0].agent_id == "agent-1"


def test_pg_specific_ddl_skipped_on_sqlite(database):
    """SQLite 上不应有 asset_recall_stats 物化视图。"""
    inspector = inspect(database.sync_engine)
    tables = set(inspector.get_table_names())
    assert "asset_recall_stats" not in tables
