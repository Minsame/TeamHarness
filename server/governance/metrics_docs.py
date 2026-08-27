"""指标文档化（SubTask 9.7）。

定义 10 个核心治理指标，每个指标含：
- name：指标名（Prometheus metric name）
- description：含义说明
- collector：采集组件（prometheus / sqlite / pg / grafana）
- instrument_location：埋点位置（文件:函数 或 HTTP 端点）
- labels：标签维度
- aggregation：聚合方式（sum / avg / histogram / counter）
- alert_threshold：告警阈值

指标清单（对应技术方案 6.1 指标落地 + 8.x 治理章节）：
1.  teamharness_asset_total           —— 资产总量
2.  teamharness_asset_active          —— 活跃资产数
3.  teamharness_module_count           —— 模块数
4.  teamharness_recall_count_30d      —— 近 30 天召回次数
5.  teamharness_adoption_rate         —— 采纳率
6.  teamharness_adoption_stale_count  —— 采纳率 stale 资产数
7.  teamharness_index_sync_lag_seconds —— 索引同步滞后秒数
8.  teamharness_embedding_queue_pending —— 待处理 embedding 任务数
9.  teamharness_distill_job_running    —— 运行中提炼任务数
10. teamharness_repo_size_bytes       —— 仓库大小（字节）
"""

from __future__ import annotations

from server.governance.models import MetricDefinition


METRICS_DOCS: list[MetricDefinition] = [
    MetricDefinition(
        name="teamharness_asset_total",
        description="资产总量（含 active / superseded / deleted 全状态）",
        collector="sqlite/pg",
        instrument_location="server/governance/dashboard.py:DashboardService.get_overview",
        labels=["type", "scope"],
        aggregation="sum",
        alert_threshold="无（信息指标）",
    ),
    MetricDefinition(
        name="teamharness_asset_active",
        description="活跃资产数（status=active），召回候选集大小",
        collector="sqlite/pg",
        instrument_location="server/governance/dashboard.py:DashboardService.get_overview",
        labels=["type", "module_path"],
        aggregation="sum",
        alert_threshold="单模块 > 20 → 拆分建议（SubTask 9.4）",
    ),
    MetricDefinition(
        name="teamharness_module_count",
        description="项目内模块数（module_path 去重）",
        collector="sqlite/pg",
        instrument_location="server/governance/module_stats.py:ModuleStatsService.compute_all_modules",
        labels=[],
        aggregation="sum",
        alert_threshold="> 5 → 建议按业务模块独立成层",
    ),
    MetricDefinition(
        name="teamharness_recall_count_30d",
        description="近 30 天召回次数（recall_log 表聚合，召回命中率分母）",
        collector="sqlite/pg",
        instrument_location="server/governance/adoption.py:AdoptionMetricsService._count_recall",
        labels=["module_path", "asset_id"],
        aggregation="sum",
        alert_threshold="单资产 < 1 → 采纳率降级（distill_team/adoption.py）",
    ),
    MetricDefinition(
        name="teamharness_adoption_rate",
        description="采纳率 = read_count / recall_count（服务端可采，recall_log 主信号）",
        collector="sqlite/pg",
        instrument_location="server/governance/adoption.py:AdoptionMetricsService.get_metric",
        labels=["asset_id", "module_path"],
        aggregation="avg",
        alert_threshold="< 0.1 → 降级 confidence=low（技术方案 8.14）",
    ),
    MetricDefinition(
        name="teamharness_adoption_stale_count",
        description="采纳率 stale 资产数（连续 7 天无客户端上报）",
        collector="sqlite/pg",
        instrument_location="server/governance/adoption.py:AdoptionMetricsService.mark_stale",
        labels=[],
        aggregation="sum",
        alert_threshold="> 0 → 治理看板告警（SubTask 9.9 红线）",
    ),
    MetricDefinition(
        name="teamharness_index_sync_lag_seconds",
        description="DB 索引同步滞后秒数（last_synced_at 与 now 差值）",
        collector="prometheus",
        instrument_location="server/infra_db/sync.py:SyncService.get_sync_status",
        labels=["sync_source"],
        aggregation="gauge",
        alert_threshold="> 300s → lag_periods++ ，连续 3 周期触发告警",
    ),
    MetricDefinition(
        name="teamharness_embedding_queue_pending",
        description="待处理 embedding 任务数（embedding_task_queue.status=pending）",
        collector="prometheus",
        instrument_location="server/infra_db/outbox.py:OutboxWorker.run_once",
        labels=["status", "model_version"],
        aggregation="sum",
        alert_threshold="pending > 100 → worker 扩容；failed > 0 → 人工介入",
    ),
    MetricDefinition(
        name="teamharness_distill_job_running",
        description="运行中提炼任务数（distillation_job.status=running）",
        collector="prometheus",
        instrument_location="server/distill_team/service.py:TeamDistill.trigger_incremental",
        labels=["trigger_source"],
        aggregation="sum",
        alert_threshold="> 3 → 排队（避免雪崩）；failed > 0 → 告警",
    ),
    MetricDefinition(
        name="teamharness_repo_size_bytes",
        description="仓库大小（字节），含 .git 与归档目录",
        collector="prometheus",
        instrument_location="server/governance/archive_lifecycle.py:ArchiveLifecycleService.check_repo_size",
        labels=["repo_path"],
        aggregation="gauge",
        alert_threshold="> 500MB → 治理看板 critical 告警（SubTask 9.10）",
    ),
]


def get_metric_doc(name: str) -> MetricDefinition | None:
    """按名查询指标定义。"""
    for m in METRICS_DOCS:
        if m.name == name:
            return m
    return None


def to_dict_list() -> list[dict]:
    """全部指标定义转 dict 列表（API 返回用）。"""
    return [m.to_dict() for m in METRICS_DOCS]


__all__ = [
    "METRICS_DOCS",
    "get_metric_doc",
    "to_dict_list",
]
