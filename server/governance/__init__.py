"""TeamHarness 治理与可观测性模块（Agent 9）。

职责：
- PR Review 语义去重（≥0.92 相似度，LLM 判断归并 vs 独立）
- 语义归并（移入 archive/<date>/，6 个月后删除文件）
- 治理看板（模块资产数/拆分建议/未登记告警/召回命中率/采纳率）
- 拆分判定（基于 asset_index 实时查询，非人维护 counts）
- 指标采集（Prometheus + Grafana，客户端 /v1/metrics 批量上报）
- 采纳率服务端可采（recall_log + read 次数，客户端上报作辅助）
- adoption_rate stale 标记（连续 7 天无上报）
- 过期归档 + Owner 接管流程
- 仓库大小告警（500MB 阈值）
- module_stats 从 asset_index 实时派生（缺陷 8.1 修复核心）
- teamharness index reconcile 命令
"""

from __future__ import annotations

from server.governance.adoption import AdoptionMetricsService
from server.governance.archive import SemanticArchiveService
from server.governance.archive_lifecycle import ArchiveLifecycleService
from server.governance.dashboard import DashboardService
from server.governance.metrics import GovernanceMetrics
from server.governance.metrics_docs import METRICS_DOCS
from server.governance.module_stats import ModuleStatsService
from server.governance.pr_review_dedup import PRReviewDedupService
from server.governance.reconcile import ReconcileService

__all__ = [
    "AdoptionMetricsService",
    "ArchiveLifecycleService",
    "DashboardService",
    "GovernanceMetrics",
    "METRICS_DOCS",
    "ModuleStatsService",
    "PRReviewDedupService",
    "ReconcileService",
    "SemanticArchiveService",
]
