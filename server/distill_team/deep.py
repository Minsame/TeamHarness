"""Deep 六维评分 + 晋升门禁（SubTask 8.4）。

六维评分（每维 0.0-1.0）：
1. 频率（frequency）：近 30 天被召回次数（recall_log 表）归一化
2. 来源多样性（source_diversity）：去重 owner 数归一化
3. 泛化性（generalizability）：跨 module_path 数归一化
4. 稳定性（stability）：内容 hash 变更频率倒数（变更越少越稳定）
5. 可操作性（actionability）：含明确指令/步骤关键词密度
6. 信噪比（snr）：非模板文本占比

晋升门禁：
- 正常期：来源多样性 ≥ 3 + 被召回 ≥ 3 次 + 总分 ≥ 0.6
- 冷启动期（资产 < 50）：来源多样性 ≥ 2 + 被召回 ≥ 2 次（无总分要求）
"""

from __future__ import annotations

import json
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select

from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow, RecallLog
from server.distill_team.models import GateResult, SixDimScore
from server.distill_team.rem import REMCluster

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 评分参数
# ---------------------------------------------------------------------------


# 频率归一化：召回次数 / 阈值（≥ 阈值 → 1.0）
FREQUENCY_NORM_THRESHOLD = 5
# 来源多样性归一化：owner 数 / 阈值
SOURCE_DIVERSITY_NORM_THRESHOLD = 5
# 泛化性归一化：module_path 数 / 阈值
GENERALIZABILITY_NORM_THRESHOLD = 3
# 稳定性归一化：近 30 天 hash 变更次数倒数（变更 0 次 → 1.0，变更 ≥ 3 次 → 0.0）
STABILITY_CHANGE_THRESHOLD = 3

# 可操作性关键词（含明确指令/步骤）
_ACTIONABILITY_KEYWORDS = (
    "应当", "禁止", "必须", "不得", "步骤", "流程", "检查", "执行",
    "should", "must", "never", "step", "check", "run", "execute",
)

# 模板文本特征（用于信噪比计算，识别为低信噪比）
_TEMPLATE_PATTERNS = (
    r"^---\s*$",  # frontmatter 分隔
    r"^id:\s*",
    r"^type:\s*",
    r"^owner:\s*",
    r"^scope:\s*",
    r"^tags:\s*",
    r"^related_to:\s*",
    r"^category:\s*",
    r"^version:\s*",
    r"^created_at:\s*",
    r"^updated_at:\s*",
    r"^#\s",  # H1 标题
)


# ---------------------------------------------------------------------------
# DeepScorer
# ---------------------------------------------------------------------------


class DeepScorer:
    """六维评分器。

    用法：
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem_cluster, snapshot_commit="abc")
    """

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
    ) -> None:
        self._db = database
        self._asset_index = asset_index

    def score(
        self,
        rem_cluster: REMCluster,
        *,
        snapshot_commit: str = "",
        days: int = 30,
    ) -> SixDimScore:
        """计算六维评分。"""
        asset_ids = rem_cluster.cluster.asset_ids
        if not asset_ids:
            return SixDimScore()

        # 拉取资产行
        rows = self._fetch_rows(asset_ids)
        if not rows:
            return SixDimScore()

        # 1. 频率：近 N 天召回次数
        recall_counts = self._fetch_recall_counts(asset_ids, days=days)
        total_recall = sum(recall_counts.values())
        frequency = min(1.0, total_recall / FREQUENCY_NORM_THRESHOLD)

        # 2. 来源多样性：去重 owner 数
        owner_count = rem_cluster.cross_member_count
        source_diversity = min(1.0, owner_count / SOURCE_DIVERSITY_NORM_THRESHOLD)

        # 3. 泛化性：跨 module_path 数
        module_count = rem_cluster.cross_module_count
        generalizability = min(1.0, module_count / GENERALIZABILITY_NORM_THRESHOLD)

        # 4. 稳定性：近 N 天 hash 变更频率倒数
        change_counts = self._fetch_change_counts(asset_ids, days=days)
        max_change = max(change_counts.values()) if change_counts else 0
        if max_change == 0:
            stability = 1.0
        else:
            stability = max(0.0, 1.0 - max_change / STABILITY_CHANGE_THRESHOLD)

        # 5. 可操作性：含明确指令关键词密度
        actionability = self._compute_actionability(rows)

        # 6. 信噪比：非模板文本占比
        snr = self._compute_snr(rows)

        return SixDimScore(
            frequency=frequency,
            source_diversity=source_diversity,
            generalizability=generalizability,
            stability=stability,
            actionability=actionability,
            snr=snr,
        )

    # ------------------------------------------------------------------
    # 内部：数据拉取
    # ------------------------------------------------------------------

    def _fetch_rows(self, asset_ids: list[str]) -> list[AssetIndexRow]:
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.id.in_(asset_ids))
                .where(AssetIndexRow.status == "active")
            )
            return list(sess.scalars(stmt))

    def _fetch_recall_counts(
        self, asset_ids: list[str], *, days: int
    ) -> dict[str, int]:
        """从 recall_log 统计每资产近 N 天召回次数。"""
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._db.session() as sess:
            stmt = (
                select(RecallLog.asset_id, func.count(RecallLog.id))
                .where(RecallLog.asset_id.in_(asset_ids))
                .where(RecallLog.recalled_at >= cutoff)
                .group_by(RecallLog.asset_id)
            )
            return {row[0]: int(row[1]) for row in sess.execute(stmt)}

    def _fetch_change_counts(
        self, asset_ids: list[str], *, days: int
    ) -> dict[str, int]:
        """统计近 N 天 content_hash 变更次数（近似：updated_at 在窗口内的次数）。

        简化实现：取 asset_index.updated_at 在窗口内的资产数（每次 upsert 触发一次更新）。
        真实场景应查 git log 或专门审计表，此处用 updated_at 近似。
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow.id)
                .where(AssetIndexRow.id.in_(asset_ids))
                .where(AssetIndexRow.updated_at >= cutoff)
            )
            counts: dict[str, int] = {aid: 0 for aid in asset_ids}
            for row in sess.scalars(stmt):
                counts[row] = counts.get(row, 0) + 1
            return counts

    def _compute_actionability(self, rows: list[AssetIndexRow]) -> float:
        """计算可操作性：含明确指令关键词的资产占比。"""
        if not rows:
            return 0.0
        hit = 0
        for r in rows:
            content = (r.content_snapshot or "").lower()
            if any(kw in content for kw in _ACTIONABILITY_KEYWORDS):
                hit += 1
        return hit / len(rows)

    def _compute_snr(self, rows: list[AssetIndexRow]) -> float:
        """计算信噪比：非模板文本占比（0.0-1.0）。"""
        if not rows:
            return 0.0
        total_lines = 0
        template_lines = 0
        for r in rows:
            content = r.content_snapshot or ""
            for line in content.splitlines():
                stripped = line.strip()
                if not stripped:
                    continue
                total_lines += 1
                if any(re.match(p, stripped) for p in _TEMPLATE_PATTERNS):
                    template_lines += 1
        if total_lines == 0:
            return 0.0
        return 1.0 - (template_lines / total_lines)


# ---------------------------------------------------------------------------
# PromotionGate：晋升门禁
# ---------------------------------------------------------------------------


class PromotionGate:
    """晋升门禁判定器。

    用法：
        gate = PromotionGate(database, asset_index)
        result = gate.check(rem_cluster, score, snapshot_commit="abc")
    """

    # 正常期门禁
    NORMAL_REQUIRED_SOURCE_DIVERSITY = 3
    NORMAL_REQUIRED_RECALL_COUNT = 3
    NORMAL_REQUIRED_TOTAL_SCORE = 0.6

    # 冷启动期门禁
    COLD_START_REQUIRED_SOURCE_DIVERSITY = 2
    COLD_START_REQUIRED_RECALL_COUNT = 2

    # 冷启动阈值：active 资产 < 50
    COLD_START_ASSET_THRESHOLD = 50

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
    ) -> None:
        self._db = database
        self._asset_index = asset_index

    def is_cold_start(self) -> bool:
        """判定是否处于冷启动期（active 资产 < 50）。"""
        rows = self._asset_index.query(
            AssetFilter(statuses=["active"]), limit=100000
        )
        return len(rows) < self.COLD_START_ASSET_THRESHOLD

    def get_cold_start_progress(self) -> tuple[int, int, bool]:
        """返回 (assets_needed, current_count, is_cold_start)。"""
        rows = self._asset_index.query(
            AssetFilter(statuses=["active"]), limit=100000
        )
        current = len(rows)
        is_cold = current < self.COLD_START_ASSET_THRESHOLD
        return (self.COLD_START_ASSET_THRESHOLD, current, is_cold)

    def check(
        self,
        rem_cluster: REMCluster,
        score: SixDimScore,
        *,
        recall_count: int | None = None,
        cold_start: bool | None = None,
    ) -> GateResult:
        """判定晋升门禁。

        - cold_start=None → 自动判定（基于资产数）
        - recall_count=None → 从 recall_log 统计近 30 天
        """
        if cold_start is None:
            cold_start = self.is_cold_start()

        # 实际信号
        actual_source_diversity = rem_cluster.cross_member_count
        if recall_count is None:
            recall_counts = self._fetch_recall_counts(
                rem_cluster.cluster.asset_ids, days=30
            )
            actual_recall_count = sum(recall_counts.values())
        else:
            actual_recall_count = recall_count

        # 门禁阈值
        if cold_start:
            required_sd = self.COLD_START_REQUIRED_SOURCE_DIVERSITY
            required_rc = self.COLD_START_REQUIRED_RECALL_COUNT
        else:
            required_sd = self.NORMAL_REQUIRED_SOURCE_DIVERSITY
            required_rc = self.NORMAL_REQUIRED_RECALL_COUNT

        reasons: list[str] = []
        passed = True

        if actual_source_diversity < required_sd:
            passed = False
            reasons.append(
                f"来源多样性 {actual_source_diversity} < {required_sd}"
            )
        if actual_recall_count < required_rc:
            passed = False
            reasons.append(
                f"被召回次数 {actual_recall_count} < {required_rc}"
            )
        # 冷启动期不要求总分（避免冷启动期门禁过严）
        if not cold_start and score.total < self.NORMAL_REQUIRED_TOTAL_SCORE:
            passed = False
            reasons.append(
                f"总分 {score.total:.2f} < {self.NORMAL_REQUIRED_TOTAL_SCORE}"
            )

        return GateResult(
            passed=passed,
            score=score,
            required_source_diversity=required_sd,
            required_recall_count=required_rc,
            actual_source_diversity=actual_source_diversity,
            actual_recall_count=actual_recall_count,
            cold_start=cold_start,
            reasons=reasons,
        )

    def _fetch_recall_counts(
        self, asset_ids: list[str], *, days: int
    ) -> dict[str, int]:
        cutoff = datetime.now(timezone.utc) - timedelta(days=days)
        with self._db.session() as sess:
            stmt = (
                select(RecallLog.asset_id, func.count(RecallLog.id))
                .where(RecallLog.asset_id.in_(asset_ids))
                .where(RecallLog.recalled_at >= cutoff)
                .group_by(RecallLog.asset_id)
            )
            return {row[0]: int(row[1]) for row in sess.execute(stmt)}


__all__ = ["DeepScorer", "PromotionGate"]
