"""SubTask 8.3 + 8.4：REM 跨成员模式识别 + Deep 评分集成测试。"""

from __future__ import annotations

import pytest

from server.distill_team.clustering import compute_cluster_fingerprint
from server.distill_team.deep import DeepScorer
from server.distill_team.models import Cluster
from server.distill_team.rem import REMRecognizer, REMCluster

from .conftest import upsert_asset


def _make_cluster(asset_ids, owners, modules, category=None, is_convention=False):
    return Cluster(
        cluster_id="test-cluster",
        fingerprint=compute_cluster_fingerprint(asset_ids),
        asset_ids=asset_ids,
        owners=owners,
        module_paths=modules,
        category=category,
        centroid_asset_id=asset_ids[0] if asset_ids else None,
        is_convention=is_convention,
    )


class TestREMRecognizer:
    """REM 跨成员模式识别测试。"""

    def test_recognize_single_member_not_cross(self):
        cluster = _make_cluster(["a1"], ["alice"], ["m1"])
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert len(result) == 1
        assert result[0].is_cross_member is False
        assert result[0].cross_member_count == 1

    def test_recognize_cross_member(self):
        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"]
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].is_cross_member is True
        assert result[0].cross_member_count == 2
        assert "alice" in result[0].cross_owners
        assert "bob" in result[0].cross_owners

    def test_recognize_cross_module(self):
        cluster = _make_cluster(
            ["a1", "a2"], ["alice"], ["m1", "m2"]
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].is_cross_module is True
        assert result[0].cross_module_count == 2

    def test_recognize_strong_cross_member_with_category(self):
        """同 category + 跨成员 → 强跨成员信号。"""
        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"],
            category="rule-backend",
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].is_strong_cross_member is True

    def test_recognize_no_category_not_strong(self):
        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"],
            category=None,
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].is_strong_cross_member is False

    def test_recognize_dedup_owners(self):
        """同 owner 多资产 → 去重后计 1。"""
        cluster = _make_cluster(
            ["a1", "a2", "a3"], ["alice", "alice", "bob"], ["m1"]
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].cross_member_count == 2

    def test_filter_cross_member(self):
        clusters = [
            _make_cluster(["a1"], ["alice"], ["m1"]),
            _make_cluster(["a1", "a2"], ["alice", "bob"], ["m1"]),
        ]
        rem = REMRecognizer()
        rem_clusters = rem.recognize(clusters)
        filtered = rem.filter_cross_member(rem_clusters)
        assert len(filtered) == 1
        assert filtered[0].is_cross_member is True

    def test_recognize_common_topic(self):
        """category 为空时 common_topic 取兜底文案。"""
        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"],
            category=None,
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert "跨成员模式" in result[0].common_topic

    def test_recognize_common_topic_with_category(self):
        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"],
            category="rule-backend",
        )
        rem = REMRecognizer()
        result = rem.recognize([cluster])
        assert result[0].common_topic == "rule-backend"


class TestDeepScorerIntegration:
    """Deep 评分与 REM 集成测试。"""

    def test_score_with_real_recall_log(
        self, database, asset_index
    ):
        """写入 recall_log 后评分。"""
        from datetime import datetime, timezone
        from server.infra_db.models import RecallLog

        upsert_asset(asset_index, id="a1", owner="alice", content="# rule\n应当检查")
        upsert_asset(asset_index, id="a2", owner="bob", content="# rule\n应当检查")

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

        cluster = _make_cluster(
            ["a1", "a2"], ["alice", "bob"], ["m1"]
        )
        rem = REMRecognizer().recognize([cluster])[0]
        scorer = DeepScorer(database, asset_index)
        score = scorer.score(rem)

        # 4 次召回 / 5 阈值 = 0.8
        assert abs(score.frequency - 0.8) < 0.01
        # 2 owner / 5 = 0.4
        assert abs(score.source_diversity - 0.4) < 0.01
        # actionability 命中"应当" → 1.0
        assert score.actionability == 1.0
