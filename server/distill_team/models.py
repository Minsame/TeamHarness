"""distill_team 数据模型（值对象 + 枚举）。

纯数据类，不含业务逻辑。供 service / clustering / deep / cold_start 等模块共享。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ---------------------------------------------------------------------------
# 枚举（用 str 子类便于 JSON 序列化）
# ---------------------------------------------------------------------------


class JobTriggerSource(str):
    """distillation_job.trigger_source 取值。"""

    INCREMENTAL = "incremental"  # Light 增量聚类触发
    FULL = "full"  # 全量聚类（每周日 cron）触发
    DELTA = "delta"  # 快照完成后增量 delta 触发
    CONVENTION = "convention"  # is_convention 单成员旁路触发
    MANUAL = "manual"  # 手动触发


class JobStatus(str):
    """distillation_job.status 取值。"""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"  # SKIP 审查区，待人工抽查


class Stage(str):
    """提炼三阶段。"""

    LIGHT = "light"
    REM = "rem"
    DEEP = "deep"
    SKIP = "skip"


# ---------------------------------------------------------------------------
# 聚类
# ---------------------------------------------------------------------------


@dataclass
class Cluster:
    """资产簇（Light/Full 聚类产出）。

    fingerprint：簇的唯一指纹（用于增量去重，避免重复提炼同一簇）。
    """

    cluster_id: str
    fingerprint: str
    asset_ids: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    module_paths: list[str] = field(default_factory=list)
    category: str | None = None
    # 中心资产 id（向量化后取 top-1 作为代表）
    centroid_asset_id: str | None = None
    # 簇内平均相似度（聚类质量指标）
    cohesion: float = 0.0
    # 是否触发 is_convention 单成员旁路
    is_convention: bool = False

    @property
    def size(self) -> int:
        return len(self.asset_ids)

    @property
    def owner_count(self) -> int:
        """来源多样性：去重后 owner 数。"""
        return len({o for o in self.owners if o})


# ---------------------------------------------------------------------------
# Deep 六维评分
# ---------------------------------------------------------------------------


@dataclass
class SixDimScore:
    """Deep 阶段六维评分。

    维度：频率 / 来源多样性 / 泛化性 / 稳定性 / 可操作性 / 信噪比
    每维 0.0-1.0，加权求和得 total。
    """

    frequency: float = 0.0  # 频率：近 30 天被召回次数（归一化）
    source_diversity: float = 0.0  # 来源多样性：去重 owner 数（归一化）
    generalizability: float = 0.0  # 泛化性：跨 module_path 数（归一化）
    stability: float = 0.0  # 稳定性：内容 hash 变更频率倒数
    actionability: float = 0.0  # 可操作性：含明确指令/步骤的密度
    snr: float = 0.0  # 信噪比：信息密度（非模板文本占比）

    # 权重（对应技术方案，可调）
    WEIGHTS: tuple[float, float, float, float, float, float] = field(
        default=(0.15, 0.25, 0.15, 0.15, 0.15, 0.15),
        repr=False,
        compare=False,
    )

    @property
    def total(self) -> float:
        """加权总分。"""
        dims = (
            self.frequency,
            self.source_diversity,
            self.generalizability,
            self.stability,
            self.actionability,
            self.snr,
        )
        return sum(w * d for w, d in zip(self.WEIGHTS, dims, strict=True))

    def to_dict(self) -> dict[str, float]:
        return {
            "frequency": self.frequency,
            "source_diversity": self.source_diversity,
            "generalizability": self.generalizability,
            "stability": self.stability,
            "actionability": self.actionability,
            "snr": self.snr,
            "total": self.total,
        }


@dataclass
class GateResult:
    """晋升门禁判定结果。"""

    passed: bool
    score: SixDimScore
    # 实际生效的门禁阈值（冷启动期 vs 正常期不同）
    required_source_diversity: int
    required_recall_count: int
    # 命中信号
    actual_source_diversity: int = 0
    actual_recall_count: int = 0
    # 冷启动标记
    cold_start: bool = False
    # 不通过原因
    reasons: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# 提炼产物
# ---------------------------------------------------------------------------


@dataclass
class DistilledPrompt:
    """二级提炼产出的 Prompt。"""

    prompt_id: str
    title: str
    content: str
    category: str | None
    cluster_id: str
    score: SixDimScore
    gate: GateResult
    # 置信度：low / medium / high（冷启动期强制 low）
    confidence: str = "medium"
    cold_start: bool = False
    # 反例检验结论（SKIP 机制用）
    counter_example_pass: bool = True
    # 是否进入 SKIP 审查区（人工抽查 10%）
    in_skip_review: bool = False
    skip_reason: str = ""
    # 输入资产 id 清单
    source_asset_ids: list[str] = field(default_factory=list)
    created_at: datetime | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "prompt_id": self.prompt_id,
            "title": self.title,
            "content": self.content,
            "category": self.category,
            "cluster_id": self.cluster_id,
            "score": self.score.to_dict(),
            "gate": {
                "passed": self.gate.passed,
                "required_source_diversity": self.gate.required_source_diversity,
                "required_recall_count": self.gate.required_recall_count,
                "actual_source_diversity": self.gate.actual_source_diversity,
                "actual_recall_count": self.gate.actual_recall_count,
                "cold_start": self.gate.cold_start,
                "reasons": self.gate.reasons,
            },
            "confidence": self.confidence,
            "cold_start": self.cold_start,
            "counter_example_pass": self.counter_example_pass,
            "in_skip_review": self.in_skip_review,
            "skip_reason": self.skip_reason,
            "source_asset_ids": self.source_asset_ids,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }


# ---------------------------------------------------------------------------
# Job 快照隔离
# ---------------------------------------------------------------------------


@dataclass
class JobSnapshot:
    """提炼 job 启动时快照（启动时 commit SHA + 资产 id 清单）。

    核心保证（缺陷 5.3）：job 全程基于该快照 SHA 读取资产，
    完成时比对 HEAD 与快照 SHA，若有新 commit 则触发增量 delta job。
    """

    job_id: str
    snapshot_commit: str
    head_commit_at_start: str
    asset_ids: list[str] = field(default_factory=list)
    asset_count: int = 0
    started_at: datetime | None = None


@dataclass
class JobDelta:
    """job 完成时计算的增量 delta。

    - new_commit：HEAD 比对快照 commit 的新 commit（无则空）
    - changed_asset_ids：新 commit 引入的资产 id 变更
    - need_delta_job：是否需要触发增量 job
    """

    job_id: str
    snapshot_commit: str
    head_commit_after: str
    new_commit: str = ""
    changed_asset_ids: list[str] = field(default_factory=list)
    need_delta_job: bool = False


# ---------------------------------------------------------------------------
# 冷启动进度
# ---------------------------------------------------------------------------


@dataclass
class ColdStartProgress:
    """冷启动进度（get_cold_start_progress 返回值）。"""

    assets_needed: int  # 冷启动阈值（默认 50）
    current_count: int  # 当前资产数
    is_cold_start: bool  # 是否处于冷启动期
    # 距离冷启动结束还差多少资产
    remaining: int = 0


# ---------------------------------------------------------------------------
# 采纳率降级
# ---------------------------------------------------------------------------


@dataclass
class AdoptionStatus:
    """近 30 天召回统计 + 降级判定。"""

    recall_count_30d: int
    threshold: int  # 默认 1
    degraded: bool  # recall < threshold → True
    reason: str = ""


__all__ = [
    "AdoptionStatus",
    "Cluster",
    "ColdStartProgress",
    "DistilledPrompt",
    "GateResult",
    "JobDelta",
    "JobSnapshot",
    "JobStatus",
    "JobTriggerSource",
    "SixDimScore",
    "Stage",
]
