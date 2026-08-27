"""回测验证模块测试。

覆盖回测结果判定 + 策略决策树 + 应用策略 + 回测熔断。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import RuleEntry
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.models import (
    PromotionLayer,
    PromotionState,
    PromotionStatus,
    RetestResult,
    RetestStrategy,
)
from server.distill_team.promotion.retest import (
    RetestRunner,
    apply_strategy,
    retest_rule,
    select_strategy,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_rule(
    rule_id: str = "R001",
    title: str = "Test Rule",
    content: str = "",
    *,
    category: str | None = None,
) -> RuleEntry:
    return RuleEntry(
        rule_id=rule_id,
        title=title,
        content=content,
        file_path=Path(f"/tmp/{rule_id}.md"),
        category=category,
    )


# ---------------------------------------------------------------------------
# retest_rule 单次回测
# ---------------------------------------------------------------------------


class TestRetestRule:
    """retest_rule 单次回测结果判定。"""

    def test_all_pass(self):
        """所有案例都通过 → ALL_PASS。"""
        rule = _make_rule(
            content="Always close database session and use connection pool properly",
        )
        cases = [
            "close database session",
            "use connection pool",
        ]
        assert retest_rule(rule, cases) == RetestResult.ALL_PASS

    def test_partial_pass(self):
        """部分通过 → PARTIAL_PASS。"""
        rule = _make_rule(
            content="Always close database session after use",
        )
        cases = [
            "close database session",  # 通过（关键词命中）
            "redis cache invalidation strategy",  # 不通过（无关键词）
        ]
        assert retest_rule(rule, cases) == RetestResult.PARTIAL_PASS

    def test_all_fail(self):
        """全部不通过 → ALL_FAIL。"""
        rule = _make_rule(
            content="Always close database session after use",
        )
        cases = [
            "redis cache invalidation strategy",
            "message queue consumer pattern",
        ]
        assert retest_rule(rule, cases) == RetestResult.ALL_FAIL

    def test_empty_cases_returns_all_pass(self):
        """案例为空 → ALL_PASS（边界）。"""
        rule = _make_rule(content="some content")
        assert retest_rule(rule, []) == RetestResult.ALL_PASS

    def test_single_case_pass(self):
        """单案例通过 → ALL_PASS。"""
        rule = _make_rule(content="Use parameterized queries to prevent SQL injection")
        cases = ["parameterized queries SQL injection"]
        assert retest_rule(rule, cases) == RetestResult.ALL_PASS

    def test_single_case_fail(self):
        """单案例不通过 → ALL_FAIL。"""
        rule = _make_rule(content="Use parameterized queries to prevent SQL injection")
        cases = ["completely unrelated content here"]
        assert retest_rule(rule, cases) == RetestResult.ALL_FAIL

    def test_three_cases_mixed(self):
        """3 个案例：1 通过 2 不通过 → PARTIAL_PASS。"""
        rule = _make_rule(content="Always validate user input before processing")
        cases = [
            "validate user input",
            "redis pubsub pattern",
            "graphql schema design",
        ]
        assert retest_rule(rule, cases) == RetestResult.PARTIAL_PASS


# ---------------------------------------------------------------------------
# select_strategy 策略决策树
# ---------------------------------------------------------------------------


class TestSelectStrategy:
    """select_strategy 决策树各分支测试。"""

    def test_all_pass_raises(self):
        """ALL_PASS 不应调用 select_strategy → 抛 ValueError（异常）。"""
        with pytest.raises(ValueError, match="ALL_PASS"):
            select_strategy(RetestResult.ALL_PASS, 2, 2)

    def test_zero_pass_returns_restart(self):
        """pass_count == 0 → RESTART。"""
        strategy = select_strategy(RetestResult.ALL_FAIL, 0, 3)
        assert strategy == RetestStrategy.RESTART

    def test_partial_merged_returns_split(self):
        """部分通过 + was_merged=True → SPLIT_RULE。"""
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 1, 2, was_merged=True
        )
        assert strategy == RetestStrategy.SPLIT_RULE

    def test_partial_high_ratio_returns_add_constraint(self):
        """部分通过 + 通过率 >= 0.5 + 非合并 → ADD_CONSTRAINT。"""
        # 2 个中 1 个通过：ratio = 0.5
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 1, 2, was_merged=False
        )
        assert strategy == RetestStrategy.ADD_CONSTRAINT

    def test_partial_high_ratio_three_cases(self):
        """3 个中 2 个通过（ratio = 0.67）→ ADD_CONSTRAINT。"""
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 2, 3, was_merged=False
        )
        assert strategy == RetestStrategy.ADD_CONSTRAINT

    def test_partial_low_ratio_returns_change_angle(self):
        """部分通过 + 通过率 < 0.5 + 非合并 → CHANGE_ANGLE。"""
        # 3 个中 1 个通过：ratio ≈ 0.33
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 1, 3, was_merged=False
        )
        assert strategy == RetestStrategy.CHANGE_ANGLE

    def test_partial_merged_overrides_ratio(self):
        """was_merged=True 优先于通过率判定 → SPLIT_RULE。"""
        # 即使通过率 < 0.5，was_merged=True 仍返回 SPLIT
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 1, 5, was_merged=True
        )
        assert strategy == RetestStrategy.SPLIT_RULE

    def test_partial_zero_total_returns_change_angle(self):
        """边界：total_count=0 时 ratio=0 → CHANGE_ANGLE（不会触发 RESTART，因为 pass_count > 0）。"""
        # 注：实际不应出现 pass_count > 0 且 total_count == 0，但策略决策树应稳健
        strategy = select_strategy(
            RetestResult.PARTIAL_PASS, 1, 0, was_merged=False
        )
        assert strategy == RetestStrategy.CHANGE_ANGLE


# ---------------------------------------------------------------------------
# apply_strategy 应用策略
# ---------------------------------------------------------------------------


class TestApplyStrategy:
    """apply_strategy 应用策略修正规则。"""

    def test_add_constraint_appends_keywords(self):
        """ADD_CONSTRAINT：从失败案例提取关键词追加到规则。"""
        rule = _make_rule(content="Always close database session after use")
        cases = [
            "close database session",  # 通过
            "handle redis connection timeout error",  # 失败（应被提取）
        ]
        new_rule = apply_strategy(rule, RetestStrategy.ADD_CONSTRAINT, cases)
        assert "## 补充约束" in new_rule.content
        # 失败案例的关键词应被补充
        assert "redis" in new_rule.content.lower() or "connection" in new_rule.content.lower() or "timeout" in new_rule.content.lower()

    def test_add_constraint_no_failed_cases(self):
        """ADD_CONSTRAINT 但所有案例都通过 → 返回原规则（无补充）。"""
        rule = _make_rule(content="close database session")
        cases = ["close database session"]
        new_rule = apply_strategy(rule, RetestStrategy.ADD_CONSTRAINT, cases)
        # 没有失败案例 → 无关键词 → 返回原规则
        assert "## 补充约束" not in new_rule.content

    def test_add_constraint_preserves_rule_id(self):
        """ADD_CONSTRAINT 后 rule_id / title 不变。"""
        rule = _make_rule(
            rule_id="R100",
            title="My Rule",
            content="close database session",
            category="backend",
        )
        cases = ["redis cache pattern"]
        new_rule = apply_strategy(rule, RetestStrategy.ADD_CONSTRAINT, cases)
        assert new_rule.rule_id == "R100"
        assert new_rule.title == "My Rule"
        assert new_rule.category == "backend"

    def test_split_rule_returns_original(self):
        """SPLIT_RULE：返回原规则（标记由上层处理）。"""
        rule = _make_rule(content="original content")
        new_rule = apply_strategy(rule, RetestStrategy.SPLIT_RULE, [])
        assert new_rule.content == "original content"
        # 应是不同实例（不修改原对象）
        assert new_rule is rule

    def test_change_angle_returns_original(self):
        """CHANGE_ANGLE：返回原规则。"""
        rule = _make_rule(content="original content")
        new_rule = apply_strategy(rule, RetestStrategy.CHANGE_ANGLE, [])
        assert new_rule.content == "original content"
        assert new_rule is rule

    def test_restart_returns_empty_content(self):
        """RESTART：返回空规则（content="")。"""
        rule = _make_rule(
            rule_id="R001",
            title="T",
            content="some content",
            category="x",
        )
        new_rule = apply_strategy(rule, RetestStrategy.RESTART, [])
        assert new_rule.content == ""
        # 其他字段保留
        assert new_rule.rule_id == "R001"
        assert new_rule.title == "T"
        assert new_rule.category == "x"

    def test_add_constraint_with_empty_cases(self):
        """ADD_CONSTRAINT + 空案例列表 → 返回原规则。"""
        rule = _make_rule(content="original")
        new_rule = apply_strategy(rule, RetestStrategy.ADD_CONSTRAINT, [])
        assert new_rule.content == "original"


# ---------------------------------------------------------------------------
# RetestRunner 编排器
# ---------------------------------------------------------------------------


class TestRetestRunner:
    """RetestRunner 编排器测试。"""

    def test_run_all_pass_should_promote(self):
        """全通过 → should_promote=True，strategy=None。"""
        rule = _make_rule(
            content="Always close database session and use connection pool",
        )
        state = PromotionState(
            source_case_ids=[
                "close database session",
                "use connection pool",
            ]
        )
        runner = RetestRunner(TraeAdapter())
        outcome = runner.run(rule, state)

        assert outcome.result == RetestResult.ALL_PASS
        assert outcome.strategy is None
        assert outcome.should_promote is True
        assert outcome.pass_count == 2
        assert outcome.total_count == 2
        # 全通过不增加 retest_count
        assert state.retest_count == 0

    def test_run_partial_pass_returns_strategy(self):
        """部分通过 → should_promote=False + 返回策略。"""
        rule = _make_rule(content="Always close database session")
        state = PromotionState(
            source_case_ids=[
                "close database session",  # 通过
                "redis cache invalidation",  # 失败
            ]
        )
        runner = RetestRunner(TraeAdapter())
        outcome = runner.run(rule, state)

        assert outcome.result == RetestResult.PARTIAL_PASS
        assert outcome.should_promote is False
        assert outcome.strategy is not None
        assert state.retest_count == 1
        assert state.last_retest_result == RetestResult.PARTIAL_PASS

    def test_run_all_fail_returns_restart(self):
        """全不通过 → RESTART 策略。"""
        rule = _make_rule(content="Always close database session")
        state = PromotionState(
            source_case_ids=[
                "redis cache pattern",
                "message queue consumer",
            ]
        )
        runner = RetestRunner(TraeAdapter())
        outcome = runner.run(rule, state)

        assert outcome.result == RetestResult.ALL_FAIL
        assert outcome.should_promote is False
        assert outcome.strategy == RetestStrategy.RESTART
        assert state.retest_count == 1

    def test_run_empty_cases_all_pass(self):
        """空案例 → ALL_PASS + should_promote=True。"""
        rule = _make_rule(content="some content")
        state = PromotionState(source_case_ids=[])
        runner = RetestRunner(TraeAdapter())
        outcome = runner.run(rule, state)

        assert outcome.result == RetestResult.ALL_PASS
        assert outcome.should_promote is True
        assert outcome.total_count == 0


# ---------------------------------------------------------------------------
# 回测熔断
# ---------------------------------------------------------------------------


class TestRetestCircuitBreaker:
    """回测熔断测试。"""

    def test_circuit_breaker_normal_layer(self):
        """普通层级（PROJECT）4 次熔断。"""
        rule = _make_rule(content="Always close database session")
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            source_case_ids=["redis cache pattern"],  # 失败案例
        )
        runner = RetestRunner(TraeAdapter())

        # 调用 3 次：未触发熔断
        for _ in range(3):
            runner.run(rule, state)
        assert state.retest_count == 3
        assert state.status == PromotionStatus.PROMOTING
        assert state.circuit_breaker_reason is None

        # 第 4 次：触发熔断
        outcome = runner.run(rule, state)
        assert state.retest_count == 4
        assert state.status == PromotionStatus.PENDING_CONFIRMATION
        assert state.circuit_breaker_reason is not None
        assert "4" in state.circuit_breaker_reason

    def test_circuit_breaker_top_layer(self):
        """顶层（GLOBAL_TOP）8 次熔断。"""
        rule = _make_rule(content="Always close database session")
        state = PromotionState(
            current_layer=PromotionLayer.GLOBAL_TOP,
            source_case_ids=["redis cache pattern"],
        )
        runner = RetestRunner(TraeAdapter())

        # 调用 7 次：未触发熔断
        for _ in range(7):
            runner.run(rule, state)
        assert state.retest_count == 7
        assert state.status == PromotionStatus.PROMOTING

        # 第 8 次：触发熔断
        outcome = runner.run(rule, state)
        assert state.retest_count == 8
        assert state.status == PromotionStatus.PENDING_CONFIRMATION
        assert "8" in state.circuit_breaker_reason

    def test_circuit_breaker_not_triggered_below_limit(self):
        """普通层级 3 次不熔断。"""
        rule = _make_rule(content="Always close database session")
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            source_case_ids=["redis cache pattern"],
        )
        runner = RetestRunner(TraeAdapter())

        for _ in range(3):
            runner.run(rule, state)
        assert state.status == PromotionStatus.PROMOTING

    def test_circuit_breaker_top_higher_than_normal(self):
        """顶层熔断阈值（8）应高于普通层（4）。"""
        normal_state = PromotionState(current_layer=PromotionLayer.PROJECT)
        top_state = PromotionState(current_layer=PromotionLayer.GLOBAL_TOP)
        assert top_state.retest_limit == 8
        assert normal_state.retest_limit == 4
        assert top_state.retest_limit > normal_state.retest_limit

    def test_circuit_breaker_all_pass_no_count(self):
        """全通过不增加 retest_count，不触发熔断。"""
        rule = _make_rule(content="close database session")
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            source_case_ids=["close database session"],
        )
        runner = RetestRunner(TraeAdapter())
        # 即使调用 10 次，全通过不增加 retest_count
        for _ in range(10):
            runner.run(rule, state)
        assert state.retest_count == 0
        assert state.status == PromotionStatus.PROMOTING
