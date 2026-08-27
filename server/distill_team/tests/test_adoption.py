"""SubTask 8.14：采纳率降级测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.distill_team.adoption import AdoptionRateChecker
from server.distill_team.models import (
    DistilledPrompt,
    GateResult,
    SixDimScore,
)
from server.infra_db.models import RecallLog

from .conftest import upsert_asset


class TestAdoptionRateChecker:
    """采纳率降级测试。"""

    def test_check_empty_assets_degraded(
        self, database
    ):
        checker = AdoptionRateChecker(database)
        status = checker.check([])
        assert status.degraded is True
        assert status.recall_count_30d == 0

    def test_check_no_recall_degraded(
        self, database, asset_index
    ):
        """无召回记录 → 降级。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        upsert_asset(asset_index, id="a2", owner="bob")
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1", "a2"], threshold=1)
        assert status.degraded is True
        assert status.recall_count_30d == 0
        assert "降级" in status.reason

    def test_check_with_recall_not_degraded(
        self, database, asset_index
    ):
        """有召回记录 → 不降级。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        # 写入 recall_log
        now = datetime.now(timezone.utc)
        with database.session() as sess:
            sess.add(RecallLog(
                asset_id="a1", agent_id="agent-1",
                recalled_at=now, module_path="m1",
                query="test", relevance_score=0.9,
                trace_id="t1",
            ))
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1"], threshold=1)
        assert status.degraded is False
        assert status.recall_count_30d == 1

    def test_check_old_recall_excluded(
        self, database, asset_index
    ):
        """31 天前的召回不计入（窗口 30 天）。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        old = datetime.now(timezone.utc) - timedelta(days=31)
        with database.session() as sess:
            sess.add(RecallLog(
                asset_id="a1", agent_id="agent-1",
                recalled_at=old, module_path="m1",
                query="old", relevance_score=0.5,
            ))
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1"], days=30, threshold=1)
        assert status.degraded is True
        assert status.recall_count_30d == 0

    def test_apply_degradation_sets_low_confidence(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice")
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1"], threshold=1)
        assert status.degraded is True

        score = SixDimScore()
        gate = GateResult(
            passed=True, score=score,
            required_source_diversity=2, required_recall_count=2,
        )
        prompt = DistilledPrompt(
            prompt_id="p1", title="t", content="c", category="rule-x",
            cluster_id="cl1", score=score, gate=gate,
            confidence="high",
        )
        marked = checker.apply_degradation(prompt, status)
        assert marked.confidence == "low"

    def test_apply_degradation_no_op_when_not_degraded(
        self, database, asset_index
    ):
        """不降级时 confidence 保持。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        now = datetime.now(timezone.utc)
        with database.session() as sess:
            sess.add(RecallLog(
                asset_id="a1", agent_id="agent-1",
                recalled_at=now, module_path="m1", query="q",
            ))
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1"], threshold=1)
        assert status.degraded is False

        score = SixDimScore()
        gate = GateResult(
            passed=True, score=score,
            required_source_diversity=2, required_recall_count=2,
        )
        prompt = DistilledPrompt(
            prompt_id="p1", title="t", content="c", category="rule-x",
            cluster_id="cl1", score=score, gate=gate,
            confidence="high",
        )
        marked = checker.apply_degradation(prompt, status)
        assert marked.confidence == "high"  # 保持不变

    def test_check_threshold_zero_never_degraded(
        self, database, asset_index
    ):
        """threshold=0 → 永不降级（recall >= 0 总成立）。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1"], threshold=0)
        assert status.degraded is False

    def test_check_aggregates_multiple_assets(
        self, database, asset_index
    ):
        """多资产合计召回次数。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        upsert_asset(asset_index, id="a2", owner="bob")
        now = datetime.now(timezone.utc)
        with database.session() as sess:
            for i in range(3):
                sess.add(RecallLog(
                    asset_id="a1", agent_id=f"agent-{i}",
                    recalled_at=now, module_path="m1", query="q",
                ))
            sess.add(RecallLog(
                asset_id="a2", agent_id="agent-x",
                recalled_at=now, module_path="m1", query="q",
            ))
        checker = AdoptionRateChecker(database)
        status = checker.check(["a1", "a2"], threshold=4)
        assert status.recall_count_30d == 4
        assert status.degraded is False
