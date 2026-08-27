"""SubTask 8.15：DREAMS.md 审查界面数据接口测试。"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.distill_team.review_api import DreamsReviewAPI, MonthlySummary
from server.infra_git.dreams import DreamEntry, append_entry, get_monthly_path


@pytest.fixture
def repo_with_dreams(tmp_path: Path) -> Path:
    """构造带 SKIP 条目的 DREAMS 仓库。"""
    repo = tmp_path / "repo"
    (repo / "DREAMS").mkdir(parents=True)
    now = datetime.now(timezone.utc)
    entries = [
        DreamEntry(
            timestamp=now.isoformat(),
            stage="skip",
            title="SKIP: low_quality",
            body="prompt_id: p1\n",
            metadata={"prompt_id": "p1", "needs_human_review": "true"},
        ),
        DreamEntry(
            timestamp=now.isoformat(),
            stage="skip",
            title="SKIP: overfit",
            body="prompt_id: p2\n",
            metadata={"prompt_id": "p2", "needs_human_review": "false"},
        ),
        DreamEntry(
            timestamp=now.isoformat(),
            stage="deep",
            title="Deep: PROMOTE",
            body="prompt_id: p3\n",
            metadata={"prompt_id": "p3"},
        ),
    ]
    for e in entries:
        append_entry(repo, e)
    return repo


class TestDreamsReviewAPI:
    """DREAMS 审查界面数据接口测试。"""

    def test_list_entries_all(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        entries = api.list_entries()
        assert len(entries) == 3

    def test_list_entries_filter_by_stage(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        skip_entries = api.list_entries(stage="skip")
        assert len(skip_entries) == 2
        assert all(e.stage == "skip" for e in skip_entries)

        deep_entries = api.list_entries(stage="deep")
        assert len(deep_entries) == 1

    def test_list_entries_filter_by_month(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        from server.infra_git.dreams import month_key
        m = month_key()
        entries = api.list_entries(month=m)
        assert len(entries) == 3

    def test_get_entry_by_timestamp(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        all_entries = api.list_entries()
        ts = all_entries[0].timestamp
        entry = api.get_entry(ts)
        assert entry is not None
        assert entry.timestamp == ts

    def test_get_entry_not_found(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        assert api.get_entry("nonexistent") is None

    def test_list_skip_review_all(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        skips = api.list_skip_review()
        assert len(skips) == 2

    def test_list_skip_review_needs_human_only(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        needs_review = api.list_skip_review(needs_human_review_only=True)
        assert len(needs_review) == 1
        assert needs_review[0].metadata.get("prompt_id") == "p1"

    def test_get_monthly_summary(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        from server.infra_git.dreams import month_key
        m = month_key()
        summary = api.get_monthly_summary(m)
        assert summary.month == m
        assert summary.total == 3
        assert summary.by_stage.get("skip") == 2
        assert summary.by_stage.get("deep") == 1
        assert summary.skip_total == 2
        assert summary.needs_review == 1

    def test_get_monthly_summary_empty_month(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        summary = api.get_monthly_summary("2099-01")
        assert summary.total == 0
        assert summary.by_stage == {}

    def test_list_months(
        self, repo_with_dreams
    ):
        api = DreamsReviewAPI(repo_with_dreams)
        months = api.list_months()
        from server.infra_git.dreams import month_key
        assert month_key() in months

    def test_empty_repo_returns_empty(
        self, tmp_path
    ):
        api = DreamsReviewAPI(tmp_path)
        assert api.list_entries() == []
        assert api.list_skip_review() == []
        assert api.list_months() == []
