"""SubTask 8.9：SKIP 机制 + DREAMS.md SKIP 审查区写入。

覆盖：
- LLM 返回 SKIP → 写入 DREAMS.md SKIP 审查区
- counter_example_pass=false → 强制 SKIP
- overfit=true → 强制 SKIP
- 启发式 fallback（LLM 未注入）
- SKIP 抽样（10% 人工审查）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.distill_team.llm_schema import DistillPromptRunner, should_human_review
from server.distill_team.models import (
    DistilledPrompt,
    GateResult,
    SixDimScore,
)
from server.distill_team.rem import REMRecognizer
from server.distill_team.clustering import compute_cluster_fingerprint
from server.distill_team.models import Cluster

from .conftest import upsert_asset


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


class MockLLM:
    """Mock LLM Provider，按预设返回内容。"""

    def __init__(self, response_content: str) -> None:
        self._response = response_content

    def chat(self, messages, *, schema=None):
        return {"content": self._response, "usage": {}}


def _make_rem_cluster(asset_ids, owners, category=None):
    cluster = Cluster(
        cluster_id="test-cluster",
        fingerprint=compute_cluster_fingerprint(asset_ids),
        asset_ids=asset_ids,
        owners=owners,
        module_paths=["m1"],
        category=category,
        centroid_asset_id=asset_ids[0] if asset_ids else None,
    )
    rem = REMRecognizer()
    return rem.recognize([cluster])[0]


def _make_gate(cold_start=False, passed=True):
    score = SixDimScore()
    return GateResult(
        passed=passed, score=score,
        required_source_diversity=2, required_recall_count=2,
        actual_source_diversity=2, actual_recall_count=2,
        cold_start=cold_start,
    )


# ---------------------------------------------------------------------------
# 启发式 fallback
# ---------------------------------------------------------------------------


class TestHeuristicFallback:
    """LLM 未注入 → 启发式 fallback。"""

    def test_heuristic_promote_when_high_score(
        self, distill_repo
    ):
        """score.total >= 0.6 → PROMOTE。"""
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore(
            frequency=1.0, source_diversity=1.0, generalizability=1.0,
            stability=1.0, actionability=1.0, snr=1.0,
        )
        gate = _make_gate(passed=True)
        runner = DistillPromptRunner(repo_root=distill_repo, llm=None)
        prompt = runner.run(rem, score, gate, assets_content=[])
        assert prompt.in_skip_review is False
        # total=1.0 → PROMOTE
        assert prompt.skip_reason == ""

    def test_heuristic_skip_when_low_score(
        self, distill_repo
    ):
        """score.total < 0.6 → SKIP。"""
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore()  # total = 0
        gate = _make_gate(passed=True)
        runner = DistillPromptRunner(repo_root=distill_repo, llm=None)
        prompt = runner.run(rem, score, gate, assets_content=[])
        # 启发式 SKIP，可能进审查区（按抽样率）
        # 抽样率默认 0.1，单次可能不进
        # 验证 skip_reason 非空
        assert prompt.skip_reason != ""


# ---------------------------------------------------------------------------
# LLM SKIP 机制
# ---------------------------------------------------------------------------


class TestLLMSkip:
    """LLM 返回 SKIP 的处理。"""

    def test_llm_skip_writes_dreams_entry(
        self, distill_repo
    ):
        """LLM 返回 SKIP → 写入 DREAMS.md SKIP 审查区。"""
        skip_response = json.dumps({
            "step1_topic": "测试主题",
            "step2_pattern": "测试模式",
            "step3_draft": {"title": "测试", "content": "# 测试"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": False,
            "step5_overfit_reason": "",
            "step6_decision": "SKIP",
            "skip_reason": "low_quality",
        })
        llm = MockLLM(skip_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore()
        gate = _make_gate(passed=True)

        runner = DistillPromptRunner(
            repo_root=distill_repo, llm=llm, skip_review_sample_rate=1.0
        )
        prompt = runner.run(rem, score, gate, assets_content=[])

        assert prompt.in_skip_review is True
        assert prompt.skip_reason == "low_quality"

        # DREAMS 文件已写入 SKIP 条目
        from server.infra_git.dreams import get_monthly_path, month_key
        from datetime import datetime, timezone
        dreams_path = get_monthly_path(distill_repo, datetime.now(timezone.utc))
        assert dreams_path.exists()
        content = dreams_path.read_text(encoding="utf-8")
        assert "[skip]" in content
        assert "low_quality" in content

    def test_llm_promote_no_dreams_entry(
        self, distill_repo
    ):
        """LLM 返回 PROMOTE → 不写 DREAMS。"""
        promote_response = json.dumps({
            "step1_topic": "主题",
            "step2_pattern": "模式",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": False,
            "step5_overfit_reason": "",
            "step6_decision": "PROMOTE",
            "skip_reason": "",
        })
        llm = MockLLM(promote_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore(source_diversity=0.6)
        gate = _make_gate(passed=True)

        runner = DistillPromptRunner(repo_root=distill_repo, llm=llm)
        prompt = runner.run(rem, score, gate, assets_content=[])

        assert prompt.in_skip_review is False
        assert prompt.skip_reason == ""

    def test_llm_counter_example_fail_forces_skip(
        self, distill_repo
    ):
        """counter_example_pass=false → 强制 SKIP（即使 decision=PROMOTE）。"""
        inconsistent_response = json.dumps({
            "step1_topic": "主题",
            "step2_pattern": "模式",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [{"scenario": "A", "hit": True}],
            "step4_counter_example_pass": False,
            "step5_overfit": False,
            "step5_overfit_reason": "",
            "step6_decision": "PROMOTE",  # 故意不一致
            "skip_reason": "",
        })
        llm = MockLLM(inconsistent_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore()
        gate = _make_gate(passed=True)

        runner = DistillPromptRunner(
            repo_root=distill_repo, llm=llm, skip_review_sample_rate=1.0
        )
        prompt = runner.run(rem, score, gate, assets_content=[])

        # 一致性兜底：counter_example_pass=false → 强制 SKIP
        assert prompt.in_skip_review is True
        assert "counter_example_failed" in prompt.skip_reason

    def test_llm_overfit_forces_skip(
        self, distill_repo
    ):
        """overfit=true → 强制 SKIP。"""
        overfit_response = json.dumps({
            "step1_topic": "主题",
            "step2_pattern": "模式",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": True,
            "step5_overfit_reason": "过拟合到 modules/legacy",
            "step6_decision": "PROMOTE",  # 故意不一致
            "skip_reason": "",
        })
        llm = MockLLM(overfit_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore()
        gate = _make_gate(passed=True)

        runner = DistillPromptRunner(
            repo_root=distill_repo, llm=llm, skip_review_sample_rate=1.0
        )
        prompt = runner.run(rem, score, gate, assets_content=[])

        assert prompt.in_skip_review is True
        assert "overfit" in prompt.skip_reason
        assert prompt.counter_example_pass is True  # 反例通过，但 overfit


# ---------------------------------------------------------------------------
# SKIP 抽样
# ---------------------------------------------------------------------------


class TestSkipSampling:
    """SKIP 抽样测试。"""

    def test_should_human_review_with_seed(self):
        """固定种子 → 确定性抽样。"""
        # seed=1 时 0.1 阈值
        results = [should_human_review(skip_count_this_week=i, seed=42) for i in range(100)]
        # 大约 10% 为 True
        true_count = sum(results)
        assert 5 <= true_count <= 20  # 容忍随机波动

    def test_skip_review_sample_rate_zero(
        self, distill_repo
    ):
        """sample_rate=0 → 永不进审查区。"""
        skip_response = json.dumps({
            "step1_topic": "t", "step2_pattern": "p",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": False, "step5_overfit_reason": "",
            "step6_decision": "SKIP", "skip_reason": "low",
        })
        llm = MockLLM(skip_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        runner = DistillPromptRunner(
            repo_root=distill_repo, llm=llm, skip_review_sample_rate=0.0
        )
        prompt = runner.run(rem, SixDimScore(), _make_gate(), assets_content=[])
        assert prompt.in_skip_review is False
        assert prompt.skip_reason == "low"  # SKIP 但不进审查区

    def test_skip_review_sample_rate_one(
        self, distill_repo
    ):
        """sample_rate=1.0 → 所有 SKIP 进审查区。"""
        skip_response = json.dumps({
            "step1_topic": "t", "step2_pattern": "p",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": False, "step5_overfit_reason": "",
            "step6_decision": "SKIP", "skip_reason": "low",
        })
        llm = MockLLM(skip_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        runner = DistillPromptRunner(
            repo_root=distill_repo, llm=llm, skip_review_sample_rate=1.0
        )
        prompt = runner.run(rem, SixDimScore(), _make_gate(), assets_content=[])
        assert prompt.in_skip_review is True


# ---------------------------------------------------------------------------
# repo_root 未配置
# ---------------------------------------------------------------------------


class TestNoRepoRoot:
    """repo_root=None 时 SKIP 不写 DREAMS（不抛异常）。"""

    def test_skip_no_repo_root(self):
        skip_response = json.dumps({
            "step1_topic": "t", "step2_pattern": "p",
            "step3_draft": {"title": "T", "content": "# T"},
            "step4_counter_examples": [],
            "step4_counter_example_pass": True,
            "step5_overfit": False, "step5_overfit_reason": "",
            "step6_decision": "SKIP", "skip_reason": "low",
        })
        llm = MockLLM(skip_response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        runner = DistillPromptRunner(repo_root=None, llm=llm,
                                     skip_review_sample_rate=1.0)
        # 不抛异常
        prompt = runner.run(rem, SixDimScore(), _make_gate(), assets_content=[])
        assert prompt.skip_reason == "low"


# ---------------------------------------------------------------------------
# JSON 解析容错
# ---------------------------------------------------------------------------


class TestLLMJsonParse:
    """LLM 返回非合法 JSON → 启发式 fallback。"""

    def test_invalid_json_falls_back(self, distill_repo):
        llm = MockLLM("not a json")
        rem = _make_rem_cluster(["a1"], ["alice"])
        runner = DistillPromptRunner(repo_root=distill_repo, llm=llm)
        prompt = runner.run(rem, SixDimScore(), _make_gate(), assets_content=[])
        # 启发式产出
        # total=0 → SKIP
        assert prompt.skip_reason != ""

    def test_markdown_fenced_json_parsed(self, distill_repo):
        """LLM 返回带 ```json 围栏的 JSON → 正常解析。"""
        response = """```json
{
  "step1_topic": "t",
  "step2_pattern": "p",
  "step3_draft": {"title": "T", "content": "# T"},
  "step4_counter_examples": [],
  "step4_counter_example_pass": true,
  "step5_overfit": false,
  "step5_overfit_reason": "",
  "step6_decision": "PROMOTE",
  "skip_reason": ""
}
```"""
        llm = MockLLM(response)
        rem = _make_rem_cluster(["a1"], ["alice"])
        score = SixDimScore(source_diversity=0.6)
        runner = DistillPromptRunner(repo_root=distill_repo, llm=llm)
        prompt = runner.run(rem, score, _make_gate(), assets_content=[])
        # 成功解析 → PROMOTE
        assert prompt.skip_reason == ""
