"""governance 域数据模型（值对象 + 枚举）。

纯数据类，不含业务逻辑。供 pr_review_dedup / archive / dashboard /
module_stats / adoption / metrics / archive_lifecycle / reconcile 共享。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# PR Review 语义去重（SubTask 9.1）
# ---------------------------------------------------------------------------


@dataclass
class DuplicateMatch:
    """单条相似资产命中（≥0.92 相似度）。"""

    asset_id: str
    git_path: str
    module_path: str
    similarity: float
    # LLM 判断结果：merge（归并）/ independent（独立）/ unknown（未调 LLM）
    llm_decision: str = "unknown"
    llm_rationale: str = ""


@dataclass
class DedupSuggestion:
    """单条资产的 PR Review 去重建议。"""

    new_asset_id: str
    new_asset_path: str
    duplicates: list[DuplicateMatch] = field(default_factory=list)
    # 综合建议：merge / keep_separate / needs_review
    suggestion: str = "needs_review"
    # LLM 调用错误（若有）
    llm_error: str = ""

    @property
    def has_duplicates(self) -> bool:
        return bool(self.duplicates)


@dataclass
class PRReviewDedupResult:
    """PR Review 语义去重整体结果。"""

    pr_id: str
    suggestions: list[DedupSuggestion] = field(default_factory=list)
    total_duplicates: int = 0
    llm_calls: int = 0
    llm_errors: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "pr_id": self.pr_id,
            "suggestions": [
                {
                    "new_asset_id": s.new_asset_id,
                    "new_asset_path": s.new_asset_path,
                    "suggestion": s.suggestion,
                    "llm_error": s.llm_error,
                    "duplicates": [
                        {
                            "asset_id": d.asset_id,
                            "git_path": d.git_path,
                            "module_path": d.module_path,
                            "similarity": d.similarity,
                            "llm_decision": d.llm_decision,
                            "llm_rationale": d.llm_rationale,
                        }
                        for d in s.duplicates
                    ],
                }
                for s in self.suggestions
            ],
            "total_duplicates": self.total_duplicates,
            "llm_calls": self.llm_calls,
            "llm_errors": self.llm_errors,
        }


# ---------------------------------------------------------------------------
# 语义归并（SubTask 9.2）
# ---------------------------------------------------------------------------


@dataclass
class ArchiveRecord:
    """单条归档记录。"""

    asset_id: str
    original_path: str
    archive_path: str
    archived_at: datetime
    # 6 个月后可物理删除（hard_delete_at）
    hard_delete_at: datetime
    reason: str = "semantic_merge"

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "original_path": self.original_path,
            "archive_path": self.archive_path,
            "archived_at": self.archived_at.isoformat(),
            "hard_delete_at": self.hard_delete_at.isoformat(),
            "reason": self.reason,
        }


@dataclass
class ArchiveResult:
    """语义归并结果。"""

    archived: list[ArchiveRecord] = field(default_factory=list)
    failed: list[dict[str, str]] = field(default_factory=list)

    @property
    def archived_count(self) -> int:
        return len(self.archived)


# ---------------------------------------------------------------------------
# 模块统计实时派生（SubTask 9.11 + 9.4）
# ---------------------------------------------------------------------------


@dataclass
class ModuleStatsSnapshot:
    """单模块的实时派生统计（缺陷 8.1 修复核心）。

    全部字段从 asset_index 实时派生，不依赖人维护的 INDEX.md counts。
    declared_* 字段从 module_stats 表读取（INDEX.md 镜像），用于 counts 一致性校验。
    """

    module_path: str
    actual_asset_count: int = 0
    actual_submodule_count: int = 0
    # 按资产类型分布（rule/memory/skill/tool/prompt）
    by_type: dict[str, int] = field(default_factory=dict)
    # 按状态分布（active/deleted/superseded）
    by_status: dict[str, int] = field(default_factory=dict)
    # INDEX.md 声明值（镜像，用于一致性校验，可能为 None 表示无声明）
    declared_asset_count: int | None = None
    declared_submodule_count: int | None = None
    # counts 是否一致（declared 与 actual 不一致 → False）
    counts_consistent: bool = True
    last_synced_at: datetime | None = None
    last_synced_commit: str = ""

    @property
    def has_mismatch(self) -> bool:
        """declared 与 actual 是否不一致。"""
        if self.declared_asset_count is not None and self.declared_asset_count != self.actual_asset_count:
            return True
        if (
            self.declared_submodule_count is not None
            and self.declared_submodule_count != self.actual_submodule_count
        ):
            return True
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_path": self.module_path,
            "actual_asset_count": self.actual_asset_count,
            "actual_submodule_count": self.actual_submodule_count,
            "by_type": dict(self.by_type),
            "by_status": dict(self.by_status),
            "declared_asset_count": self.declared_asset_count,
            "declared_submodule_count": self.declared_submodule_count,
            "counts_consistent": self.counts_consistent,
            "has_mismatch": self.has_mismatch,
            "last_synced_at": self.last_synced_at.isoformat() if self.last_synced_at else None,
            "last_synced_commit": self.last_synced_commit,
        }


@dataclass
class SplitSuggestion:
    """模块拆分建议（基于实时派生 counts，非人维护 counts）。"""

    module_path: str
    signal: str  # module_count_too_many / asset_count_too_many / submodule_count_too_many
    threshold: int
    actual: int
    suggestion: str  # 建议动作描述
    severity: str = "warning"  # info / warning / critical

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_path": self.module_path,
            "signal": self.signal,
            "threshold": self.threshold,
            "actual": self.actual,
            "suggestion": self.suggestion,
            "severity": self.severity,
        }


# ---------------------------------------------------------------------------
# 治理看板（SubTask 9.3）
# ---------------------------------------------------------------------------


@dataclass
class DashboardAlert:
    """治理看板告警。"""

    level: str  # info / warning / critical
    category: str  # orphan_asset / counts_mismatch / adoption_stale / repo_size / archive_overdue
    message: str
    module_path: str = ""
    asset_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "level": self.level,
            "category": self.category,
            "message": self.message,
            "module_path": self.module_path,
            "asset_id": self.asset_id,
            "extra": dict(self.extra),
        }


@dataclass
class ModuleRecallHitRate:
    """单模块召回命中率（基于 recall_log 实时聚合）。"""

    module_path: str
    recall_count: int = 0
    read_count: int = 0
    asset_count: int = 0
    # 命中率 = read / recall（资产被读取的比例）
    hit_rate: float = 0.0
    # 每资产平均召回次数（识别"资产多但命中率低"的模块）
    avg_recall_per_asset: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_path": self.module_path,
            "recall_count": self.recall_count,
            "read_count": self.read_count,
            "asset_count": self.asset_count,
            "hit_rate": self.hit_rate,
            "avg_recall_per_asset": self.avg_recall_per_asset,
        }


@dataclass
class DashboardData:
    """治理看板整体数据。"""

    module_stats: list[ModuleStatsSnapshot] = field(default_factory=list)
    split_suggestions: list[SplitSuggestion] = field(default_factory=list)
    orphan_asset_alerts: list[DashboardAlert] = field(default_factory=list)
    recall_hit_rates: list[ModuleRecallHitRate] = field(default_factory=list)
    adoption_rates: list[dict[str, Any]] = field(default_factory=list)
    repo_size_alerts: list[DashboardAlert] = field(default_factory=list)
    generated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "module_stats": [m.to_dict() for m in self.module_stats],
            "split_suggestions": [s.to_dict() for s in self.split_suggestions],
            "orphan_asset_alerts": [a.to_dict() for a in self.orphan_asset_alerts],
            "recall_hit_rates": [r.to_dict() for r in self.recall_hit_rates],
            "adoption_rates": list(self.adoption_rates),
            "repo_size_alerts": [a.to_dict() for a in self.repo_size_alerts],
            "generated_at": self.generated_at,
        }


# ---------------------------------------------------------------------------
# 采纳率服务端可采（SubTask 9.8 + 9.9）
# ---------------------------------------------------------------------------


@dataclass
class AdoptionMetric:
    """单资产的采纳率指标（服务端可采）。"""

    asset_id: str
    module_path: str = ""
    # 服务端可采：recall_log 实时聚合（近 30 天）
    recall_count_30d: int = 0
    read_count_30d: int = 0
    # 采纳率 = read / recall（被读取的召回占比）
    adoption_rate: float = 0.0
    # 客户端上报事件数（辅助信号）
    client_events_30d: int = 0
    # 最后客户端上报时间（用于 stale 判定）
    last_client_event_at: datetime | None = None
    # stale 标记：连续 7 天无客户端上报 → True
    stale: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "module_path": self.module_path,
            "recall_count_30d": self.recall_count_30d,
            "read_count_30d": self.read_count_30d,
            "adoption_rate": self.adoption_rate,
            "client_events_30d": self.client_events_30d,
            "last_client_event_at": self.last_client_event_at.isoformat()
            if self.last_client_event_at
            else None,
            "stale": self.stale,
        }


# ---------------------------------------------------------------------------
# 指标文档化（SubTask 9.7）
# ---------------------------------------------------------------------------


@dataclass
class MetricDefinition:
    """单个核心指标的定义文档。"""

    name: str  # 指标名（如 teamharness_asset_total）
    description: str  # 含义说明
    collector: str  # 采集组件（prometheus / sqlite / pg / grafana）
    instrument_location: str  # 埋点位置（文件:函数 或 HTTP 端点）
    labels: list[str] = field(default_factory=list)  # 标签维度
    aggregation: str = ""  # 聚合方式（sum / avg / histogram / counter）
    alert_threshold: str = ""  # 告警阈值

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "collector": self.collector,
            "instrument_location": self.instrument_location,
            "labels": list(self.labels),
            "aggregation": self.aggregation,
            "alert_threshold": self.alert_threshold,
        }


# ---------------------------------------------------------------------------
# 仓库大小告警（SubTask 9.10 内）
# ---------------------------------------------------------------------------


@dataclass
class RepoSizeAlert:
    """仓库大小告警。"""

    repo_path: str
    size_bytes: int
    size_mb: float
    threshold_mb: int = 500
    exceeded: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "repo_path": self.repo_path,
            "size_bytes": self.size_bytes,
            "size_mb": self.size_mb,
            "threshold_mb": self.threshold_mb,
            "exceeded": self.exceeded,
        }


# ---------------------------------------------------------------------------
# Owner 接管流程（SubTask 9.10 内）
# ---------------------------------------------------------------------------


@dataclass
class OwnerTakeoverResult:
    """Owner 接管结果。"""

    old_owner: str
    new_owner: str
    asset_ids: list[str] = field(default_factory=list)
    # 失败的资产 + 错误原因
    failed: list[dict[str, str]] = field(default_factory=list)
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "old_owner": self.old_owner,
            "new_owner": self.new_owner,
            "asset_ids": list(self.asset_ids),
            "failed": list(self.failed),
            "success": self.success,
            "error": self.error,
        }


# ---------------------------------------------------------------------------
# reconcile 命令结果（SubTask 9.12）
# ---------------------------------------------------------------------------


@dataclass
class ReconcileResult:
    """teamharness index reconcile 结果。"""

    commit_sha: str = ""
    modules_checked: int = 0
    modules_with_mismatch: int = 0
    mismatches: list[dict[str, Any]] = field(default_factory=list)
    # 是否触发 DB 索引补同步
    db_resync_triggered: bool = False
    db_resync_result: str = ""
    elapsed_seconds: float = 0.0
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "commit_sha": self.commit_sha,
            "modules_checked": self.modules_checked,
            "modules_with_mismatch": self.modules_with_mismatch,
            "mismatches": list(self.mismatches),
            "db_resync_triggered": self.db_resync_triggered,
            "db_resync_result": self.db_resync_result,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
        }


__all__ = [
    "AdoptionMetric",
    "ArchiveRecord",
    "ArchiveResult",
    "DashboardAlert",
    "DashboardData",
    "DedupSuggestion",
    "DuplicateMatch",
    "MetricDefinition",
    "ModuleRecallHitRate",
    "ModuleStatsSnapshot",
    "OwnerTakeoverResult",
    "PRReviewDedupResult",
    "ReconcileResult",
    "RepoSizeAlert",
    "SplitSuggestion",
]
