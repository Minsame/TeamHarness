"""升维循环模块测试。

覆盖升维检验清单 + 升维循环 + 升维抽象 + 熔断。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import (
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.models import (
    DEFAULT_LIMITS,
    PromotionLayer,
    PromotionState,
    PromotionStatus,
)
from server.distill_team.promotion.promote import (
    PROMOTION_CHECKLIST,
    PromotionManager,
    abstract_rule,
    check_promotion_eligibility,
)


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_layout(tmp_path: Path) -> MemoryLayout:
    """用 tmp_path 构造 MemoryLayout，避免依赖真实 home 目录。"""
    return MemoryLayout(
        project_rules_dir=tmp_path / "project_rules",
        global_rules_dir=tmp_path / "global_rules",
        user_profile_path=tmp_path / "profile.md",
        archive_path=tmp_path / "archive.md",
        graph_path=tmp_path / "graph.md",
        cross_project_root=tmp_path / "cross",
        rules_file_ext=".md",
        supports_frontmatter=True,
        hotspot_section_marker="## 热点规则",
    )


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
# check_promotion_eligibility 升维检验清单
# ---------------------------------------------------------------------------


class TestCheckPromotionEligibility:
    """升维检验：判断规则是否需要升维。"""

    def test_already_top_layer(self):
        """已达顶层 → 无需升维。"""
        rule = _make_rule(content="Some generic content")
        state = PromotionState(current_layer=PromotionLayer.GLOBAL_TOP)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is False
        assert decision.target_layer == PromotionLayer.GLOBAL_TOP
        assert "已达顶层" in decision.reason
        # 顶层时检验清单为空（直接返回）
        assert decision.checklist_results == []

    def test_project_specific_stays_project(self):
        """规则含项目特定标识 → 留在项目级。"""
        rule = _make_rule(
            content="在 d:\\Code\\TeamHarness\\server 中使用 SQLAlchemy session",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is False
        assert decision.target_layer == PromotionLayer.PROJECT
        assert "特定项目" in decision.reason
        # 检验清单应执行
        assert len(decision.checklist_results) > 0

    def test_general_rule_promotes_to_rules_file(self):
        """通用规则 → 升维到 RULES_FILE。"""
        rule = _make_rule(
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is True
        assert decision.target_layer == PromotionLayer.RULES_FILE
        assert "规则文件" in decision.reason

    def test_top_layer_eligible_from_rules_file(self):
        """规则文件级 + 足够通用简短 → 升维到顶层。"""
        # 内容 <= 200 字符且无项目特定标识
        rule = _make_rule(
            content="Always validate user input before processing.",
        )
        state = PromotionState(current_layer=PromotionLayer.RULES_FILE)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is True
        assert decision.target_layer == PromotionLayer.GLOBAL_TOP
        assert "顶层" in decision.reason

    def test_top_layer_ineligible_long_content(self):
        """规则文件级 + 内容过长 → 不升维到顶层。"""
        # 内容 > 200 字符
        long_content = "a" * 250
        rule = _make_rule(content=long_content)
        state = PromotionState(current_layer=PromotionLayer.RULES_FILE)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is False
        assert decision.target_layer == PromotionLayer.RULES_FILE
        assert "顶层标准" in decision.reason

    def test_top_layer_ineligible_with_project_specific(self):
        """规则文件级 + 含项目特定标识 → 不升维到顶层。"""
        rule = _make_rule(content="Use .trae/rules/ for project rules")
        state = PromotionState(current_layer=PromotionLayer.RULES_FILE)
        decision = check_promotion_eligibility(rule, state)

        assert decision.should_promote is False
        assert decision.target_layer == PromotionLayer.RULES_FILE

    def test_checklist_has_six_items(self):
        """检验清单有 6 项。"""
        rule = _make_rule(content="Generic content", category="x")
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)
        assert len(decision.checklist_results) == 6
        # 每项是 (检验项描述, 是否通过)
        for item_text, passed in decision.checklist_results:
            assert isinstance(item_text, str)
            assert isinstance(passed, bool)

    def test_checklist_matches_promotion_checklist(self):
        """检验清单项与 PROMOTION_CHECKLIST 一致。"""
        assert len(PROMOTION_CHECKLIST) == 6
        rule = _make_rule(content="Generic", category="x")
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)
        for i, (text, _) in enumerate(decision.checklist_results):
            assert text == PROMOTION_CHECKLIST[i]

    def test_checklist_category_present_passes_item5(self):
        """有 category 时第 5 项（归属清晰）通过。"""
        rule = _make_rule(content="Generic content", category="backend")
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)
        # 第 5 项索引 4：规则归属的文件类别是否清晰
        assert decision.checklist_results[4][1] is True

    def test_checklist_category_absent_fails_item5(self):
        """无 category 时第 5 项不通过。"""
        rule = _make_rule(content="Generic content")
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        decision = check_promotion_eligibility(rule, state)
        assert decision.checklist_results[4][1] is False


# ---------------------------------------------------------------------------
# abstract_rule 升维抽象（泛化）
# ---------------------------------------------------------------------------


class TestAbstractRule:
    """abstract_rule 升维抽象测试。"""

    def test_generalize_for_rules_file(self):
        """升维到 RULES_FILE：去除项目特定路径。"""
        rule = _make_rule(
            content=(
                "在 d:\\Code\\TeamHarness\\server\\app.py 中使用 "
                "SQLAlchemy session 管理数据库连接"
            ),
        )
        abstracted = abstract_rule(rule, PromotionLayer.RULES_FILE)

        # 项目特定路径应被替换为 ...
        assert "d:\\Code\\TeamHarness\\server\\app.py" not in abstracted.content
        assert "..." in abstracted.content
        # 通用部分保留
        assert "SQLAlchemy" in abstracted.content

    def test_abstract_to_principle_for_top(self):
        """升维到 GLOBAL_TOP：取第一段作为核心原则。"""
        content = "First principle line\n\nSecond paragraph\nThird line"
        rule = _make_rule(content=content)
        abstracted = abstract_rule(rule, PromotionLayer.GLOBAL_TOP)

        # 应只保留第一段
        assert "First principle line" in abstracted.content
        assert "Second paragraph" not in abstracted.content

    def test_abstract_same_layer_returns_original(self):
        """目标层级 = 当层级 → 原样返回（内容不变）。"""
        rule = _make_rule(content="Some content here")
        abstracted = abstract_rule(rule, PromotionLayer.PROJECT)
        assert abstracted.content == "Some content here"

    def test_abstract_preserves_rule_id_and_title(self):
        """泛化后 rule_id / title / category 保留。"""
        rule = _make_rule(
            rule_id="R001",
            title="My Rule",
            content="在 .trae/rules/ 中使用 SQLAlchemy",
            category="backend",
        )
        abstracted = abstract_rule(rule, PromotionLayer.RULES_FILE)
        assert abstracted.rule_id == "R001"
        assert abstracted.title == "My Rule"
        assert abstracted.category == "backend"

    def test_abstract_returns_new_instance(self):
        """泛化返回新 RuleEntry 实例（不改原对象）。"""
        rule = _make_rule(content="original .trae content")
        original_content = rule.content
        abstracted = abstract_rule(rule, PromotionLayer.RULES_FILE)
        # 原对象不变
        assert rule.content == original_content
        # 新对象是不同实例
        assert abstracted is not rule

    def test_abstract_top_single_line(self):
        """顶层抽象：内容只有一行时取该行。"""
        rule = _make_rule(content="Single line principle")
        abstracted = abstract_rule(rule, PromotionLayer.GLOBAL_TOP)
        assert abstracted.content == "Single line principle"

    def test_abstract_top_empty_content(self):
        """顶层抽象：空内容 → 返回原内容。"""
        rule = _make_rule(content="")
        abstracted = abstract_rule(rule, PromotionLayer.GLOBAL_TOP)
        assert abstracted.content == ""


# ---------------------------------------------------------------------------
# PromotionManager 升维循环
# ---------------------------------------------------------------------------


class TestPromotionManager:
    """PromotionManager 升维执行测试。"""

    def test_already_top_layer(self, tmp_path: Path):
        """已达顶层 → PROMOTED，不执行升维。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(content="Some content")
        state = PromotionState(current_layer=PromotionLayer.GLOBAL_TOP)
        result = manager.promote(rule, state, layout)

        assert result.promoted is False
        assert result.target_layer == PromotionLayer.GLOBAL_TOP
        assert result.target_path is None
        assert state.status == PromotionStatus.PROMOTED
        assert "已达顶层" in result.reason

    def test_project_specific_stays_project(self, tmp_path: Path):
        """项目特定规则 → 留在项目级，标记 PROMOTED。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(
            content="在 d:\\Code\\TeamHarness\\server\\app.py 中管理 session",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        result = manager.promote(rule, state, layout)

        assert result.promoted is False
        assert result.target_layer == PromotionLayer.PROJECT
        assert state.status == PromotionStatus.PROMOTED
        assert "特定项目" in result.reason

    def test_general_rule_promotes_to_rules_file(self, tmp_path: Path):
        """通用规则 → 升维到 RULES_FILE，写入 global_rules_dir。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        result = manager.promote(rule, state, layout)

        # 升维成功
        assert result.promoted is True
        assert result.target_layer == PromotionLayer.RULES_FILE
        assert result.target_path is not None
        # 文件已写入 + 状态已更新
        written_files = list(layout.global_rules_dir.glob("*.md"))
        assert len(written_files) >= 1
        assert state.current_layer == PromotionLayer.RULES_FILE
        assert state.promote_count == 1
        assert state.status == PromotionStatus.PROMOTING  # 未到顶层，可继续升维

    def test_general_rule_promotes_to_top(self, tmp_path: Path):
        """从 RULES_FILE 升维到 GLOBAL_TOP。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        # 内容简短且通用
        rule = _make_rule(content="Always validate user input.")
        state = PromotionState(current_layer=PromotionLayer.RULES_FILE)
        result = manager.promote(rule, state, layout)

        # 升维成功
        assert result.promoted is True
        assert result.target_layer == PromotionLayer.GLOBAL_TOP
        assert result.target_path is not None
        # 文件已写入 + 状态已更新
        assert layout.user_profile_path.is_file()
        assert state.current_layer == PromotionLayer.GLOBAL_TOP
        assert state.status == PromotionStatus.PROMOTED  # 已达顶层

    def test_promote_writes_rule_to_global_dir(self, tmp_path: Path):
        """升维后规则文件实际写入 global_rules_dir。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(
            rule_id="R999",
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        result = manager.promote(rule, state, layout)

        # 升维成功
        assert result.promoted is True
        # 规则文件已写入
        written_file = layout.global_rules_dir / "R999.md"
        assert written_file.is_file()
        text = written_file.read_text(encoding="utf-8")
        assert "Always close database connections" in text

    def test_promote_writes_hotspot_to_profile(self, tmp_path: Path):
        """升维到顶层时写入热点规则区。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(rule_id="R888", content="Keep it simple.")
        state = PromotionState(current_layer=PromotionLayer.RULES_FILE)
        result = manager.promote(rule, state, layout)

        # 升维成功
        assert result.promoted is True
        # 热点规则已写入 user_profile
        assert layout.user_profile_path.is_file()
        text = layout.user_profile_path.read_text(encoding="utf-8")
        assert "## 热点规则" in text
        assert "R888" in text


# ---------------------------------------------------------------------------
# 升维熔断
# ---------------------------------------------------------------------------


class TestPromotionCircuitBreaker:
    """升维循环熔断测试。"""

    def test_circuit_breaker_at_six(self, tmp_path: Path):
        """promote_count 达到 6 次 → NOT_TO_TOP。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        # 预设 promote_count = 5，下次升维后变 6 → 触发熔断
        rule = _make_rule(
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            promote_count=5,
        )
        result = manager.promote(rule, state, layout)

        # 触发熔断
        assert result.promoted is True
        assert state.promote_count == 6
        assert state.status == PromotionStatus.NOT_TO_TOP
        assert state.circuit_breaker_reason is not None
        assert "6" in state.circuit_breaker_reason

    def test_circuit_breaker_not_triggered_below_six(self, tmp_path: Path):
        """promote_count < 6 不触发熔断。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(
            current_layer=PromotionLayer.PROJECT,
            promote_count=4,
        )
        result = manager.promote(rule, state, layout)

        # 未触发熔断
        assert result.promoted is True
        assert state.promote_count == 5
        assert state.status == PromotionStatus.PROMOTING

    def test_circuit_breaker_default_max_is_six(self):
        """默认熔断阈值是 6。"""
        assert DEFAULT_LIMITS.promote_max == 6

    def test_circuit_breaker_increments_global_iteration(self, tmp_path: Path):
        """升维也增加 global_iteration。"""
        layout = _make_layout(tmp_path)
        adapter = TraeAdapter()
        manager = PromotionManager(adapter)

        rule = _make_rule(
            content="Always close database connections after use to prevent leaks",
            category="backend",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        result = manager.promote(rule, state, layout)

        # 升维成功
        assert result.promoted is True
        assert state.global_iteration == 1
        assert state.promote_count == 1
