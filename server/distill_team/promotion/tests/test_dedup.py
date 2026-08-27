"""查重模块测试。

覆盖四种查重判定 + 分桶定位 + 查重熔断。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import (
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.dedup import (
    DedupChecker,
    bucket_lookup,
    check_duplicate,
)
from server.distill_team.promotion.models import (
    DedupVerdict,
    PromotionLayer,
    PromotionState,
    PromotionStatus,
)


# ---------------------------------------------------------------------------
# 辅助：构造 MemoryLayout（用 tmp_path 隔离）
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
    rule_id: str,
    title: str,
    content: str,
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
# 四种查重判定
# ---------------------------------------------------------------------------


class TestNoDuplicate:
    """新规则与已有规则无交集 → NO_DUPLICATE。"""

    def test_no_duplicate_with_empty_existing(self):
        """已有规则列表为空 → NO_DUPLICATE（边界）。"""
        new_rule = _make_rule("R001", "规则A", "Use async await for IO operations", category="backend")
        verdict = check_duplicate(new_rule, [])
        assert verdict == DedupVerdict.NO_DUPLICATE

    def test_no_duplicate_with_unrelated_content(self):
        """内容完全不相关 → NO_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "前端规则",
                "Use React hooks for state management in components",
                category="frontend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "后端规则",
            "Always close database connections after use to prevent leaks",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.NO_DUPLICATE

    def test_no_duplicate_low_similarity(self):
        """内容相似度低于 0.6 → NO_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "DB Rule",
                "Use SQLAlchemy ORM for all database queries in service layer",
                category="backend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "Cache Rule",
            "Redis cache invalidation strategy for distributed systems",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.NO_DUPLICATE


class TestExactDuplicate:
    """内容相似度 >= 0.9 且 category 一致 → EXACT_DUPLICATE。"""

    def test_exact_duplicate_same_content(self):
        """内容几乎相同 + category 一致 → EXACT_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "DB Session Rule",
                "Always close the database session after each request to prevent connection leaks",
                category="backend",
            )
        ]
        # 几乎相同的内容（只差一个字符）
        new_rule = _make_rule(
            "R001",
            "DB Session Rule",
            "Always close the database session after each request to prevent connection leak",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.EXACT_DUPLICATE

    def test_exact_duplicate_identical_content(self):
        """完全相同的内容 + 相同 category → EXACT_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "T",
                "Use parameterized queries to prevent SQL injection attacks",
                category="security",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "T",
            "Use parameterized queries to prevent SQL injection attacks",
            category="security",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.EXACT_DUPLICATE

    def test_high_similarity_different_category_returns_cross(self):
        """内容相似度 >= 0.9 但 category 不同 → CROSS_DUPLICATE（非 EXACT）。"""
        existing = [
            _make_rule(
                "R100",
                "T",
                "Always close the database session after each request to prevent leaks",
                category="backend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "T",
            "Always close the database session after each request to prevent leak",
            category="frontend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.CROSS_DUPLICATE


class TestSubsetDuplicate:
    """新经验是已有规则子集 → SUBSET_DUPLICATE。"""

    def test_subset_duplicate(self):
        """新经验的关键词都是已有规则的子集 + 相似度 < 0.9 → SUBSET_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "DB Rule",
                "Use SQLAlchemy session to query database connection pool",
                category="backend",
            )
        ]
        # 新经验内容是 existing 的子集（用词都在 existing 中），但内容相似度 < 0.9
        new_rule = _make_rule(
            "R001",
            "DB Rule",
            "Use SQLAlchemy session",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.SUBSET_DUPLICATE

    def test_subset_requires_same_category(self):
        """subset 判定要求 category 一致；不一致 → 不是 SUBSET。"""
        existing = [
            _make_rule(
                "R100",
                "DB Rule",
                "Use SQLAlchemy session to query database connection pool",
                category="backend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "DB Rule",
            "Use SQLAlchemy session",
            category="frontend",
        )
        verdict = check_duplicate(new_rule, existing)
        # category 不同 → subset 不成立 → 走相似度判定
        assert verdict != DedupVerdict.SUBSET_DUPLICATE

    def test_subset_with_new_keyword_not_subset(self):
        """新经验含 existing 没有的关键词 → 不是 subset。"""
        existing = [
            _make_rule(
                "R100",
                "T",
                "Use SQLAlchemy session to query database",
                category="backend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "T",
            "Use SQLAlchemy session with redis cache",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        # "redis" / "cache" 不在 existing → subset 不成立
        assert verdict != DedupVerdict.SUBSET_DUPLICATE


class TestCrossDuplicate:
    """相似度 0.6-0.9 → CROSS_DUPLICATE。"""

    def test_cross_duplicate_medium_similarity(self):
        """内容相似度在 0.6-0.9 之间且非 subset → CROSS_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "DB Rule",
                "Always close database sessions after use",
                category="backend",
            )
        ]
        # new 含 existing 没有的关键词 "before" / "request" / "ends"
        # 且相似度在 0.6-0.9 之间
        new_rule = _make_rule(
            "R001",
            "DB Rule",
            "Always close database sessions before request ends",
            category="backend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.CROSS_DUPLICATE

    def test_cross_duplicate_high_similarity_different_category(self):
        """高相似度但 category 不同 → CROSS_DUPLICATE。"""
        existing = [
            _make_rule(
                "R100",
                "T",
                "Always close the database session after each request to prevent leaks",
                category="backend",
            )
        ]
        new_rule = _make_rule(
            "R001",
            "T",
            "Always close the database session after each request to prevent leak",
            category="frontend",
        )
        verdict = check_duplicate(new_rule, existing)
        assert verdict == DedupVerdict.CROSS_DUPLICATE


# ---------------------------------------------------------------------------
# 分桶定位
# ---------------------------------------------------------------------------


class TestBucketLookup:
    """分桶定位测试。"""

    def test_bucket_lookup_with_category(self, tmp_path: Path):
        """有 category → 只扫项目级规则目录。"""
        layout = _make_layout(tmp_path)
        dirs = bucket_lookup("backend", layout)
        assert dirs == [layout.project_rules_dir]

    def test_bucket_lookup_without_category(self, tmp_path: Path):
        """category=None → 全量扫描（项目级 + 全局级）。"""
        layout = _make_layout(tmp_path)
        dirs = bucket_lookup(None, layout)
        assert dirs == [layout.project_rules_dir, layout.global_rules_dir]

    def test_bucket_lookup_returns_list(self, tmp_path: Path):
        """返回值是 list（可变，可遍历）。"""
        layout = _make_layout(tmp_path)
        dirs = bucket_lookup("x", layout)
        assert isinstance(dirs, list)
        assert len(dirs) == 1


# ---------------------------------------------------------------------------
# DedupChecker 编排器 + 熔断
# ---------------------------------------------------------------------------


class TestDedupChecker:
    """DedupChecker 编排器测试。"""

    def test_check_returns_dedup_result(self, tmp_path: Path):
        """DedupChecker.check 返回 DedupResult。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule("R001", "T", "Some unique content", category="backend")
        state = PromotionState()
        result = checker.check(new_rule, state, layout)

        assert result.verdict == DedupVerdict.NO_DUPLICATE
        assert result.duplicate_of is None
        assert result.existing_rule is None
        assert result.action == "no_duplicate"
        assert result.state is state
        # state 被更新
        assert state.dedup_count == 1
        assert state.last_dedup_verdict == DedupVerdict.NO_DUPLICATE

    def test_check_finds_existing_rule(self, tmp_path: Path):
        """DedupChecker 解析已有规则并匹配。"""
        layout = _make_layout(tmp_path)
        rules_dir = layout.project_rules_dir
        rules_dir.mkdir(parents=True)
        # 写入一条已有规则
        (rules_dir / "R100.md").write_text(
            "---\nid: R100\ntitle: T\ncategory: backend\n---\n"
            "Always close database connections after use",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        # 新规则与已有规则完全相同
        new_rule = _make_rule(
            "R001",
            "T",
            "Always close database connections after use",
            category="backend",
        )
        state = PromotionState()
        result = checker.check(new_rule, state, layout)

        assert result.verdict == DedupVerdict.EXACT_DUPLICATE
        assert result.duplicate_of == "R100"
        assert result.existing_rule is not None
        assert result.action == "find_untriggered_reason"

    def test_check_top_layer_scans_global(self, tmp_path: Path):
        """顶层规则（is_top_layer=True）跨类别全量扫描。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        layout.global_rules_dir.mkdir(parents=True)
        # 已有规则在 global_rules_dir（跨类别扫描时才会扫到）
        (layout.global_rules_dir / "R200.md").write_text(
            "---\nid: R200\ntitle: T\ncategory: other\n---\n"
            "Unique global rule content for testing",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule(
            "R001",
            "T",
            "Unique global rule content for testing",
            category="other",  # 与 R200 一致，高相似度判为 EXACT
        )
        state = PromotionState(current_layer=PromotionLayer.GLOBAL_TOP)
        result = checker.check(new_rule, state, layout)

        # 应能匹配到 global_rules_dir 下的 R200
        assert result.verdict == DedupVerdict.EXACT_DUPLICATE
        assert result.duplicate_of == "R200"

    def test_check_non_top_layer_skips_global(self, tmp_path: Path):
        """非顶层规则（项目级）只扫项目级目录，不扫 global。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        layout.global_rules_dir.mkdir(parents=True)
        # 已有规则只在 global_rules_dir
        (layout.global_rules_dir / "R200.md").write_text(
            "---\nid: R200\ntitle: T\ncategory: backend\n---\n"
            "Identical backend content",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule(
            "R001",
            "T",
            "Identical backend content",
            category="backend",
        )
        state = PromotionState(current_layer=PromotionLayer.PROJECT)
        result = checker.check(new_rule, state, layout)

        # 项目级目录为空，扫不到 global 的规则 → NO_DUPLICATE
        assert result.verdict == DedupVerdict.NO_DUPLICATE

    def test_check_scans_multiple_dirs_in_bucket(self, tmp_path: Path):
        """顶层时 bucket 含两个目录，两个目录都被扫描。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        layout.global_rules_dir.mkdir(parents=True)
        # 两个目录各写一条规则
        (layout.project_rules_dir / "R100.md").write_text(
            "---\nid: R100\ntitle: T\n---\nProject rule content here",
            encoding="utf-8",
        )
        (layout.global_rules_dir / "R200.md").write_text(
            "---\nid: R200\ntitle: T\n---\nGlobal rule content here",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule(
            "R001",
            "T",
            "Completely unrelated new rule text",
        )
        state = PromotionState(current_layer=PromotionLayer.GLOBAL_TOP)
        result = checker.check(new_rule, state, layout)
        # 两条已有规则都扫到，但都不匹配 → NO_DUPLICATE
        assert result.verdict == DedupVerdict.NO_DUPLICATE


# ---------------------------------------------------------------------------
# 查重熔断
# ---------------------------------------------------------------------------


class TestDedupCircuitBreaker:
    """查重熔断测试：dedup_count 达到 6 次 → PENDING_CONFIRMATION。"""

    def test_circuit_breaker_triggers_at_six(self, tmp_path: Path):
        """连续查重 6 次 → 第 6 次触发熔断，status 变为 PENDING_CONFIRMATION。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule("R001", "T", "Some unique content", category="backend")
        state = PromotionState()

        # 调用 5 次：未触发熔断
        for _ in range(5):
            checker.check(new_rule, state, layout)
        assert state.dedup_count == 5
        assert state.status == PromotionStatus.PROMOTING
        assert state.circuit_breaker_reason is None

        # 第 6 次：触发熔断
        result = checker.check(new_rule, state, layout)
        assert state.dedup_count == 6
        assert state.status == PromotionStatus.PENDING_CONFIRMATION
        assert state.circuit_breaker_reason is not None
        assert "6" in state.circuit_breaker_reason

    def test_circuit_breaker_not_triggered_below_six(self, tmp_path: Path):
        """查重 5 次不触发熔断。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule("R001", "T", "Unique content", category="x")
        state = PromotionState()

        for _ in range(5):
            checker.check(new_rule, state, layout)
        assert state.status == PromotionStatus.PROMOTING

    def test_circuit_breaker_records_last_verdict(self, tmp_path: Path):
        """熔断触发时仍记录最后一次判定结果。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule("R001", "T", "Unique content", category="x")
        state = PromotionState()

        for _ in range(6):
            result = checker.check(new_rule, state, layout)
        # 最后一次结果应是 NO_DUPLICATE（无已有规则）
        assert result.verdict == DedupVerdict.NO_DUPLICATE
        assert state.last_dedup_verdict == DedupVerdict.NO_DUPLICATE

    def test_circuit_breaker_increments_global_iteration(self, tmp_path: Path):
        """每次查重都会增加 global_iteration 计数。"""
        layout = _make_layout(tmp_path)
        layout.project_rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        checker = DedupChecker(adapter)
        new_rule = _make_rule("R001", "T", "Unique content", category="x")
        state = PromotionState()

        for _ in range(3):
            checker.check(new_rule, state, layout)
        # dedup_count + global_iteration 都应同步
        assert state.dedup_count == 3
        assert state.global_iteration == 3
