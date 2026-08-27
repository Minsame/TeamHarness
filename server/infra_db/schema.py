"""PG 专属 DDL：分区表 + 物化视图 + TTL 函数。

对应 SubTask 2.1（PG schema 全表）+ SubTask 2.9（recall_log 按月分区 + 6 个月 TTL）。

包含：
1. recall_log 改为按月 RANGE 分区表（PG 专属，SQLite 不支持）
2. asset_recall_stats 物化视图（每资产召回次数聚合）
3. create_recall_partition(month) 函数：动态创建月度分区
4. drop_old_recall_partitions(months) 函数：6 个月 TTL 删除老分区
5. refresh_asset_recall_stats() 函数：刷新物化视图

设计原则：
- SQLAlchemy ORM 模型（models.py）描述通用结构，可跨 PG/SQLite
- 本模块提供 PG 专属 SQL 字符串，仅在生产 PG 上执行
- Alembic 迁移调用 execute_pg_ddl() 在 PG 上应用
- 测试用 SQLite 时跳过 PG 专属 DDL
"""

from __future__ import annotations

from typing import Iterable


# ---------------------------------------------------------------------------
# recall_log 按月 RANGE 分区（PG 专属）
# ---------------------------------------------------------------------------

# 注意：SQLAlchemy ORM 把 recall_log 当普通表声明（models.py），
# PG 上需先 DROP 普通表再 CREATE 为分区表。Alembic 初始 migration 处理顺序：
# 1. CREATE TABLE recall_log (...) PARTITION BY RANGE (recalled_at)
# 2. 创建初始月度分区
RECALL_LOG_PARTITION_DDL = """
CREATE TABLE IF NOT EXISTS recall_log (
    id BIGSERIAL,
    asset_id VARCHAR(64) NOT NULL,
    agent_id VARCHAR(64) NOT NULL,
    recalled_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    module_path VARCHAR(256) NOT NULL DEFAULT '',
    query TEXT NOT NULL DEFAULT '',
    relevance_score FLOAT,
    trace_id VARCHAR(64)
) PARTITION BY RANGE (recalled_at);
CREATE INDEX IF NOT EXISTS idx_recall_log_asset_time
    ON recall_log (asset_id, recalled_at);
CREATE INDEX IF NOT EXISTS idx_recall_log_module_time
    ON recall_log (module_path, recalled_at);
CREATE INDEX IF NOT EXISTS idx_recall_log_recalled_at
    ON recall_log (recalled_at);
"""


def make_monthly_partition_sql(year: int, month: int) -> str:
    """生成年月分区 SQL。

    分区名：recall_log_YYYYMM
    范围：[当月1号 00:00:00 UTC, 下月1号 00:00:00 UTC)
    """
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1
    return f"""
CREATE TABLE IF NOT EXISTS recall_log_{year}{month:02d}
    PARTITION OF recall_log
    FOR VALUES FROM ('{year}-{month:02d}-01 00:00:00+00')
    TO ('{next_year}-{next_month:02d}-01 00:00:00+00');
"""


def make_drop_partition_sql(year: int, month: int) -> str:
    """生成删除分区 SQL（6 个月 TTL）。"""
    return f"DROP TABLE IF EXISTS recall_log_{year}{month:02d};"


# ---------------------------------------------------------------------------
# asset_recall_stats 物化视图（PG 专属）
# ---------------------------------------------------------------------------

# 物化视图：每资产召回次数聚合（二级提炼晋升门禁"被召回次数"数据源 + 治理看板召回命中率）
# 注：物化视图不能直接索引，需创建独立索引
ASSET_RECALL_STATS_MV_DDL = """
CREATE MATERIALIZED VIEW IF NOT EXISTS asset_recall_stats AS
SELECT
    asset_id,
    COUNT(*) AS recall_count,
    COUNT(DISTINCT agent_id) AS unique_agent_count,
    MAX(recalled_at) AS last_recalled_at,
    MIN(recalled_at) AS first_recalled_at
FROM recall_log
GROUP BY asset_id
WITH DATA;

CREATE UNIQUE INDEX IF NOT EXISTS idx_asset_recall_stats_asset
    ON asset_recall_stats (asset_id);
"""

REFRESH_ASSET_RECALL_STATS_SQL = "REFRESH MATERIALIZED VIEW CONCURRENTLY asset_recall_stats;"


# ---------------------------------------------------------------------------
# 6 个月 TTL 函数（PG 专属）
# ---------------------------------------------------------------------------

# create_recall_partition(month_start timestamptz) → 创建月度分区
# drop_old_recall_partitions(months_to_keep int) → 删除超过 months_to_keep 个月的分区
# 这两个函数由 reconciliation cron 与定时任务调用
RECALL_TTL_FUNCTIONS_SQL = r"""
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
        -- 解析分区名末尾的 YYYYMM 与 cutoff 比较
        IF substring(part.tablename from '[0-9]{6}$')::text < to_char(cutoff, 'YYYYMM') THEN
            EXECUTE format('DROP TABLE IF EXISTS %I', part.tablename);
            dropped := dropped + 1;
        END IF;
    END LOOP;
    RETURN dropped;
END;
$$ LANGUAGE plpgsql;
"""


# ---------------------------------------------------------------------------
# 对账与孤儿补偿 SQL（PG 专属，复用 ORM 不便时直接执行）
# ---------------------------------------------------------------------------

# 查询 embedding_id IS NULL 的资产（对账任务用，缺陷 1.1 一致性窗口）
FIND_NULL_EMBEDDING_SQL = """
SELECT a.id, a.git_path, a.git_commit, q.id AS task_id
FROM asset_index a
LEFT JOIN embedding_task_queue q
    ON q.asset_id = a.id AND q.status = 'done'
WHERE a.status = 'active'
  AND a.embedding_id IS NULL
  AND a.indexed_at < NOW() - INTERVAL '1 hour'
ORDER BY a.indexed_at ASC
LIMIT 500;
"""

# reconciliation 连续滞后周期累加（连续 3 周期触发告警）
INCREMENT_LAG_PERIODS_SQL = """
UPDATE index_sync_state
SET lag_periods = lag_periods + 1
WHERE id = 'singleton' AND status = 'lagging';
"""

RESET_LAG_PERIODS_SQL = """
UPDATE index_sync_state SET lag_periods = 0 WHERE id = 'singleton';
"""


# ---------------------------------------------------------------------------
# 统一执行入口（Alembic / 启动时调用）
# ---------------------------------------------------------------------------


def all_pg_ddl() -> list[str]:
    """返回全部 PG 专属 DDL（按执行顺序）。"""
    return [
        RECALL_LOG_PARTITION_DDL,
        ASSET_RECALL_STATS_MV_DDL,
        RECALL_TTL_FUNCTIONS_SQL,
    ]


def pg_only_statements() -> Iterable[tuple[str, str]]:
    """返回 (名称, SQL) 列表，便于 Alembic 按需选择执行。"""
    return [
        ("recall_log_partition", RECALL_LOG_PARTITION_DDL),
        ("asset_recall_stats_mv", ASSET_RECALL_STATS_MV_DDL),
        ("recall_ttl_functions", RECALL_TTL_FUNCTIONS_SQL),
    ]


__all__ = [
    "ASSET_RECALL_STATS_MV_DDL",
    "FIND_NULL_EMBEDDING_SQL",
    "INCREMENT_LAG_PERIODS_SQL",
    "REFRESH_ASSET_RECALL_STATS_SQL",
    "RECALL_LOG_PARTITION_DDL",
    "RECALL_TTL_FUNCTIONS_SQL",
    "RESET_LAG_PERIODS_SQL",
    "all_pg_ddl",
    "make_drop_partition_sql",
    "make_monthly_partition_sql",
    "pg_only_statements",
]
