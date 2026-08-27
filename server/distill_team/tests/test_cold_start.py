"""SubTask 8.5 + 8.4：冷启动旁路 + Deep 六维评分 + 晋升门禁。

覆盖：
- 冷启动期（资产 < 50）门禁降为 ≥ 2
- 正常期（资产 ≥ 50）门禁 ≥ 3
- 冷启动产出标记 confidence=low + cold_start=true
- 六维评分（每维 0.0-1.0）
"""

from __future__ import annotations

import pytest

from server.distill_team.cold_start import ColdStartBypass
from server.distill_team.deep import DeepScorer, PromotionGate
from server.distill_team.models import (
    DistilledPrompt,
    GateResult,
    SixDimScore,
)
from server.distill_team.rem import REMRecognizer
from server.distill_team.clustering import compute_cluster_fingerprint

from .conftest import upsert_asset


def _make_rem_cluster(asset_ids, owners, modules, category=None, is_convention=False):
    """构造 REMCluster 测试夹具。"""
    from server.distill_team.models import Cluster
    cluster = Cluster(
        cluster_id="test-cluster",
        fingerprint=compute_cluster_fingerprint(asset_ids),
        asset_ids=asset_ids,
        owners=owners,
        module_paths=modules,
        category=category,
        centroid_asset_id=asset_ids[0] if asset_ids else None,
        is_convention=is_convention,
    )
    rem = REMRecognizer()
    rem_clusters = rem.recognize([cluster])
    return rem_clusters[0]


# ---------------------------------------------------------------------------
# 8.4 Deep 六维评分
# ---------------------------------------------------------------------------


class TestDeepScorer:
    """Deep 六维评分测试。"""

    def test_score_empty_cluster_returns_zeros(
        self, database, asset_index
    ):
        scorer = DeepScorer(database, asset_index)
        rem = _make_rem_cluster([], [], [])
        score = scorer.score(rem)
        assert score.frequency == 0.0
        assert score.source_diversity == 0.0

    def test_score_source_diversity_normalized(
        self, database, asset_index
    ):
        """3 个 owner → source_diversity = 3/5 = 0.6。"""
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        upsert_asset(asset_index, id="a2", owner="bob", content="# rule")
        upsert_asset(asset_index, id="a3", owner="carol", content="# rule")

        rem = _make_rem_cluster(
            ["a1", "a2", "a3"],
            owners=["alice", "bob", "carol"],
            modules=["m1"],
        )
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem)
        assert 0.0 < score.source_diversity <= 1.0
        # 3 / 5 = 0.6
        assert abs(score.source_diversity - 0.6) < 0.01

    def test_score_actionability_with_keywords(
        self, database, asset_index
    ):
        """含应当/禁止 → actionability 高。"""
        upsert_asset(
            asset_index, id="a1", owner="alice",
            content="# rule\n应当检查\n禁止硬编码"
        )
        rem = _make_rem_cluster(["a1"], owners=["alice"], modules=["m1"])
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem)
        assert score.actionability == 1.0  # 1/1 资产命中

    def test_score_snr_with_template_lines(
        self, database, asset_index
    ):
        """frontmatter 行多 → snr 低。"""
        upsert_asset(
            asset_index, id="a1", owner="alice",
            content="---\nid: a1\ntype: rule\n---\n# title\n应当检查"
        )
        rem = _make_rem_cluster(["a1"], owners=["alice"], modules=["m1"])
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem)
        # 5 行总，3 行模板（---/id:/type:/# title）→ snr = 1 - 3/5 = 0.4
        # 实际：'---' / 'id: a1' / 'type: rule' / '---' / '# title' / '应当检查'
        # 模板行：'---'×2 / 'id: a1' / 'type: rule' / '# title'（H1）= 5 行
        # 总行 6 → snr = 1 - 5/6 ≈ 0.167
        assert 0.0 <= score.snr <= 0.5

    def test_score_total_in_range(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        rem = _make_rem_cluster(["a1"], owners=["alice"], modules=["m1"])
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem)
        assert 0.0 <= score.total <= 1.0


# ---------------------------------------------------------------------------
# 8.5 + 8.4 晋升门禁
# ---------------------------------------------------------------------------


class TestPromotionGate:
    """晋升门禁测试。"""

    def test_is_cold_start_true_when_assets_lt_50(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice", content="# rule")
        gate = PromotionGate(database, asset_index)
        assert gate.is_cold_start() is True

    def test_is_cold_start_false_when_assets_ge_50(
        self, database, asset_index
    ):
        for i in range(50):
            upsert_asset(
                asset_index, id=f"a{i}", owner=f"u{i % 5}",
                content=f"# rule {i}", git_commit=f"c{i}"
            )
        gate = PromotionGate(database, asset_index)
        assert gate.is_cold_start() is False

    def test_get_cold_start_progress(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice")
        gate = PromotionGate(database, asset_index)
        needed, current, is_cold = gate.get_cold_start_progress()
        assert needed == 50
        assert current == 1
        assert is_cold is True

    def test_gate_cold_start_lowered_threshold(
        self, database, asset_index
    ):
        """冷启动期：来源多样性 ≥ 2 即通过。"""
        upsert_asset(asset_index, id="a1", owner="alice")
        upsert_asset(asset_index, id="a2", owner="bob")

        rem = _make_rem_cluster(
            ["a1", "a2"], owners=["alice", "bob"], modules=["m1"]
        )
        gate = PromotionGate(database, asset_index)
        score = SixDimScore(source_diversity=0.4)
        result = gate.check(rem, score, recall_count=2)

        assert result.cold_start is True
        assert result.required_source_diversity == 2
        assert result.required_recall_count == 2
        assert result.passed is True

    def test_gate_cold_start_fail_below_lowered_threshold(
        self, database, asset_index
    ):
        """冷启动期：来源多样性 < 2 不通过。"""
        upsert_asset(asset_index, id="a1", owner="alice")

        rem = _make_rem_cluster(["a1"], owners=["alice"], modules=["m1"])
        gate = PromotionGate(database, asset_index)
        score = SixDimScore()
        result = gate.check(rem, score, recall_count=0)

        assert result.cold_start is True
        assert result.passed is False
        assert any("来源多样性" in r for r in result.reasons)

    def test_gate_normal_period_strict_threshold(
        self, database, asset_index
    ):
        """正常期：来源多样性 ≥ 3 + 召回 ≥ 3 + 总分 ≥ 0.6。"""
        for i in range(50):
            upsert_asset(
                asset_index, id=f"a{i}", owner=f"u{i % 5}",
                content=f"# rule {i}", git_commit=f"c{i}"
            )
        rem = _make_rem_cluster(
            ["a0", "a1", "a2"],
            owners=["u0", "u1", "u2"],
            modules=["m1"],
        )
        gate = PromotionGate(database, asset_index)
        # 总分不足 → 不通过（source_diversity=0.6 → total≈0.15 < 0.6）
        score = SixDimScore(source_diversity=0.6)
        result = gate.check(rem, score, recall_count=5)
        assert result.cold_start is False
        assert result.required_source_diversity == 3
        assert result.passed is False
        assert any("总分" in r for r in result.reasons)

    def test_gate_normal_period_pass(
        self, database, asset_index
    ):
        """正常期：全部满足 → 通过。"""
        for i in range(50):
            upsert_asset(
                asset_index, id=f"a{i}", owner=f"u{i % 5}",
                content=f"# rule {i}", git_commit=f"c{i}"
            )
        rem = _make_rem_cluster(
            ["a0", "a1", "a2"],
            owners=["u0", "u1", "u2"],
            modules=["m1"],
        )
        gate = PromotionGate(database, asset_index)
        score = SixDimScore(
            frequency=1.0, source_diversity=0.6, generalizability=0.5,
            stability=1.0, actionability=1.0, snr=0.8,
        )
        result = gate.check(rem, score, recall_count=5)
        assert result.cold_start is False
        assert result.passed is True


# ---------------------------------------------------------------------------
# 8.5 冷启动标记
# ---------------------------------------------------------------------------


class TestColdStartBypass:
    """冷启动旁路标记测试。"""

    def test_apply_cold_start_marking(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice")
        gate = PromotionGate(database, asset_index)
        bypass = ColdStartBypass(gate)

        assert bypass.is_cold_start() is True

        # 构造一个 medium confidence 的 prompt
        score = SixDimScore()
        gate_result = GateResult(
            passed=True, score=score,
            required_source_diversity=2, required_recall_count=2,
            actual_source_diversity=2, actual_recall_count=2,
        )
        prompt = DistilledPrompt(
            prompt_id="p1", title="t", content="c", category="rule-x",
            cluster_id="cl1", score=score, gate=gate_result,
            confidence="medium",
        )
        marked = bypass.apply_cold_start_marking(prompt)
        assert marked.cold_start is True
        assert marked.confidence == "low"
        assert marked.gate.cold_start is True

    def test_get_progress(
        self, database, asset_index
    ):
        upsert_asset(asset_index, id="a1", owner="alice")
        upsert_asset(asset_index, id="a2", owner="bob")
        gate = PromotionGate(database, asset_index)
        bypass = ColdStartBypass(gate)

        needed, current, is_cold = bypass.get_progress()
        assert needed == 50
        assert current == 2
        assert is_cold is True
