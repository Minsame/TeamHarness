"""cost_estimate 测试（SubTask 7.11）。"""

from __future__ import annotations

from typing import Any

import pytest

from server.distill_personal.cost_estimate import (
    CostEstimateResult,
    CostEstimator,
    DEFAULT_PRICING,
    DEEP_STAGE_RATIO,
    INPUT_RATIO,
    LIGHT_STAGE_RATIO,
    OUTPUT_RATIO,
    REM_STAGE_RATIO,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_stage_ratios_sum_expected() -> None:
    """三阶段 token 系数应为 0.2 / 0.3 / 1.5（对齐技术方案 8.6）。"""
    assert LIGHT_STAGE_RATIO == 0.2
    assert REM_STAGE_RATIO == 0.3
    assert DEEP_STAGE_RATIO == 1.5


def test_input_output_ratios_sum_to_one() -> None:
    """input:output 比例应为 0.75 / 0.25。"""
    assert INPUT_RATIO == 0.75
    assert OUTPUT_RATIO == 0.25
    assert INPUT_RATIO + OUTPUT_RATIO == 1.0


def test_default_pricing_has_common_models() -> None:
    """默认单价表含常见模型。"""
    for model in ("gpt-4o-mini", "gpt-4o", "claude-3-5-sonnet", "deepseek-chat", "qwen-plus"):
        assert model in DEFAULT_PRICING
        assert "input" in DEFAULT_PRICING[model]
        assert "output" in DEFAULT_PRICING[model]


# ---------------------------------------------------------------------------
# CostEstimator.estimate（本地估算，无 LLMProviderClient）
# ---------------------------------------------------------------------------


def test_estimate_local_no_client() -> None:
    """无 llm_client 时返回 local-estimate 结果。"""
    estimator = CostEstimator()
    result = estimator.estimate(
        sessions=10,
        avg_tokens=2000,
        model="gpt-4o-mini",
        member_id="alice",
    )
    assert result.source == "local-estimate"
    assert result.sessions == 10
    assert result.avg_tokens_per_session == 2000
    assert result.model == "gpt-4o-mini"
    # total_tokens = sessions * avg_tokens * (0.2 + 0.3 + 1.5) = 10 * 2000 * 2.0 = 40000
    assert result.total_tokens == 40000
    # 阶段 token
    assert result.stages["light"] == 10 * 2000 * 0.2  # 4000
    assert result.stages["rem"] == 10 * 2000 * 0.3    # 6000
    assert result.stages["deep"] == 10 * 2000 * 1.5   # 30000
    # input/output
    assert result.input_tokens == int(40000 * 0.75)   # 30000
    assert result.output_tokens == 40000 - 30000       # 10000
    # 成本：gpt-4o-mini 0.15/0.60 per million
    expected_cost = (
        30000 / 1_000_000 * 0.15
        + 10000 / 1_000_000 * 0.60
    )
    assert abs(result.estimated_cost_usd - expected_cost) < 1e-6


def test_estimate_unknown_model_uses_default_pricing() -> None:
    """未知模型用兜底单价 {input:1.0, output:5.0}。"""
    estimator = CostEstimator()
    result = estimator.estimate(
        sessions=1,
        avg_tokens=1000,
        model="unknown-model",
    )
    # total = 1000 * 2.0 = 2000
    # input = 1500, output = 500
    # cost = 1500/1M * 1.0 + 500/1M * 5.0
    expected_cost = 1500 / 1_000_000 * 1.0 + 500 / 1_000_000 * 5.0
    assert abs(result.estimated_cost_usd - expected_cost) < 1e-6
    assert result.pricing_per_million == {"input": 1.0, "output": 5.0}


def test_estimate_zero_sessions() -> None:
    """0 sessions 应返回 0 成本。"""
    estimator = CostEstimator()
    result = estimator.estimate(sessions=0, avg_tokens=2000, model="gpt-4o-mini")
    assert result.total_tokens == 0
    assert result.estimated_cost_usd == 0.0


def test_estimate_custom_pricing_overrides_default() -> None:
    """自定义 pricing 覆盖默认单价表。"""
    custom = {"my-model": {"input": 0.0, "output": 0.0}}
    estimator = CostEstimator(pricing=custom)
    result = estimator.estimate(sessions=10, avg_tokens=2000, model="my-model")
    assert result.estimated_cost_usd == 0.0
    assert result.pricing_per_million == {"input": 0.0, "output": 0.0}


# ---------------------------------------------------------------------------
# CostEstimator.estimate（远程 budget 查询）
# ---------------------------------------------------------------------------


class _FakeLLMClient:
    """模拟 LLMProviderClient.get_budget。"""

    def __init__(self, budget: dict[str, Any]) -> None:
        self._budget = budget
        self.get_budget_calls: list[str] = []

    def get_budget(self, *, member_id: str = "") -> Any:
        self.get_budget_calls.append(member_id)
        from server.distill_personal.llm_provider import LLMBudget
        return LLMBudget(
            member_id=member_id,
            daily_token_budget=self._budget.get("daily_token_budget", 100_000),
            used=self._budget.get("used", 0),
            reset_at=self._budget.get("reset_at", ""),
            degraded=self._budget.get("degraded", False),
        )


def test_estimate_with_remote_budget_succeeds() -> None:
    """有 llm_client 且 member_id 非空 → source=remote-budget。"""
    fake = _FakeLLMClient(budget={"daily_token_budget": 50000, "used": 10000})
    estimator = CostEstimator(llm_client=fake)
    result = estimator.estimate(
        sessions=5,
        avg_tokens=2000,
        model="gpt-4o-mini",
        member_id="alice",
    )
    assert result.source == "remote-budget"
    assert result.budget_info["member_id"] == "alice"
    assert result.budget_info["daily_token_budget"] == 50000
    assert result.budget_info["used"] == 10000
    assert "covered_sessions_by_remaining" in result.budget_info
    assert len(fake.get_budget_calls) == 1


def test_estimate_with_remote_budget_no_member_id_skips_query() -> None:
    """member_id 为空时不查询 budget，仍走 local-estimate。"""
    fake = _FakeLLMClient(budget={"daily_token_budget": 50000})
    estimator = CostEstimator(llm_client=fake)
    result = estimator.estimate(
        sessions=5,
        avg_tokens=2000,
        model="gpt-4o-mini",
        member_id="",
    )
    assert result.source == "local-estimate"
    assert result.budget_info == {}
    assert len(fake.get_budget_calls) == 0


def test_estimate_with_remote_budget_failure_falls_back() -> None:
    """get_budget 抛异常时回退本地估算，budget_info 含 error。"""
    class _ExplodingClient:
        def get_budget(self, *, member_id: str = "") -> Any:
            raise RuntimeError("network error")

    estimator = CostEstimator(llm_client=_ExplodingClient())
    result = estimator.estimate(
        sessions=5,
        avg_tokens=2000,
        model="gpt-4o-mini",
        member_id="alice",
    )
    assert result.source == "local-estimate"
    assert "error" in result.budget_info
    assert "network error" in result.budget_info["error"]


def test_estimate_with_zero_remaining_budget() -> None:
    """剩余 budget=0 时 covered_sessions_by_remaining=0。"""
    fake = _FakeLLMClient(budget={"daily_token_budget": 50000, "used": 50000})
    estimator = CostEstimator(llm_client=fake)
    result = estimator.estimate(
        sessions=5,
        avg_tokens=2000,
        model="gpt-4o-mini",
        member_id="alice",
    )
    assert result.source == "remote-budget"
    assert result.budget_info["covered_sessions_by_remaining"] == 0


# ---------------------------------------------------------------------------
# CostEstimateResult.to_dict
# ---------------------------------------------------------------------------


def test_cost_estimate_result_to_dict() -> None:
    """CostEstimateResult.to_dict 字段完整。"""
    result = CostEstimateResult(
        model="gpt-4o-mini",
        sessions=10,
        avg_tokens_per_session=2000,
        stages={"light": 4000, "rem": 6000, "deep": 30000},
        total_tokens=40000,
        input_tokens=30000,
        output_tokens=10000,
        estimated_cost_usd=0.0123,
        pricing_per_million={"input": 0.15, "output": 0.60},
        source="local-estimate",
    )
    d = result.to_dict()
    assert d["model"] == "gpt-4o-mini"
    assert d["sessions"] == 10
    assert d["stages"] == {"light": 4000, "rem": 6000, "deep": 30000}
    assert d["total_tokens"] == 40000
    # estimated_cost_usd 应保留 4 位小数
    assert d["estimated_cost_usd"] == 0.0123
    assert d["source"] == "local-estimate"
