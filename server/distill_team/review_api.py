"""DREAMS.md 审查界面数据接口（SubTask 8.15）。

为治理看板 / 人工审查界面提供数据：
- list_entries：按月/阶段过滤条目
- get_entry：按 timestamp 查单条
- list_skip_review：SKIP 审查区条目（人工抽查用）
- get_monthly_summary：月度统计（总数 + 按阶段分布 + 待审查数）

数据源：repo_root/DREAMS/<YYYY-MM>.md 文件，复用 infra_git/dreams.py 解析。
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

from server.infra_git.dreams import (
    DreamEntry,
    list_months,
    month_key,
    parse_entries,
    read_archived_month,
    read_month,
)

logger = logging.getLogger(__name__)


@dataclass
class MonthlySummary:
    """月度审查统计。"""

    month: str
    total: int = 0
    by_stage: dict[str, int] = field(default_factory=dict)
    needs_review: int = 0  # SKIP 审查区待人工抽查数
    skip_total: int = 0  # SKIP 总数


class DreamsReviewAPI:
    """DREAMS.md 审查界面数据接口。

    用法：
        api = DreamsReviewAPI(repo_root=Path("./repo"))
        entries = api.list_entries(month="2026-08", stage="skip")
        summary = api.get_monthly_summary("2026-08")
    """

    def __init__(self, repo_root: Path) -> None:
        self._repo_root = Path(repo_root)

    # ------------------------------------------------------------------
    # 列表查询
    # ------------------------------------------------------------------

    def list_entries(
        self,
        *,
        month: str | None = None,
        stage: str | None = None,
    ) -> list[DreamEntry]:
        """列出审查条目。

        - month=None → 列出所有月份
        - month="2026-08" → 仅该月
        - stage="skip" / "light" / "rem" / "deep" → 按阶段过滤
        """
        months = [month] if month else list_months(self._repo_root)
        entries: list[DreamEntry] = []
        for m in months:
            entries.extend(self._read_month_entries(m))
        if stage:
            entries = [e for e in entries if e.stage == stage]
        return entries

    def get_entry(self, timestamp: str) -> DreamEntry | None:
        """按 timestamp 查单条（timestamp 为唯一键）。"""
        for m in list_months(self._repo_root):
            for entry in self._read_month_entries(m):
                if entry.timestamp == timestamp:
                    return entry
        return None

    def list_skip_review(
        self,
        *,
        month: str | None = None,
        needs_human_review_only: bool = False,
    ) -> list[DreamEntry]:
        """SKIP 审查区条目（人工抽查用）。

        - needs_human_review_only=True → 仅返回 metadata.needs_human_review=true 的条目
        """
        entries = self.list_entries(month=month, stage="skip")
        if needs_human_review_only:
            entries = [
                e for e in entries
                if e.metadata.get("needs_human_review", "").lower() == "true"
            ]
        return entries

    # ------------------------------------------------------------------
    # 月度统计
    # ------------------------------------------------------------------

    def get_monthly_summary(self, month: str | None = None) -> MonthlySummary:
        """月度统计：总数 + 按阶段分布 + SKIP 待审查数。"""
        m = month or month_key()
        entries = self._read_month_entries(m)
        by_stage = Counter(e.stage for e in entries)
        skip_entries = [e for e in entries if e.stage == "skip"]
        needs_review = sum(
            1 for e in skip_entries
            if e.metadata.get("needs_human_review", "").lower() == "true"
        )
        return MonthlySummary(
            month=m,
            total=len(entries),
            by_stage=dict(by_stage),
            needs_review=needs_review,
            skip_total=len(skip_entries),
        )

    def list_months(self) -> list[str]:
        """列出所有有审查条目的月份。"""
        return list_months(self._repo_root)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_month_entries(self, month: str) -> list[DreamEntry]:
        """读取某月全部条目（含归档）。"""
        # 先尝试当前文件
        content = read_month(self._repo_root, month)
        if not content:
            # 尝试归档
            try:
                content = read_archived_month(self._repo_root, month)
            except Exception:
                content = ""
        if not content:
            return []
        return parse_entries(content)


__all__ = ["DreamsReviewAPI", "MonthlySummary"]
