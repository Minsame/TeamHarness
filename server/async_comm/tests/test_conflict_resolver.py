"""Task 19 测试：ConflictResolver 冲突解决器。

覆盖：
- 初始化与阈值校验
- resolve 三区间决策（confirmed / revised / needs_human_review）
- resolve_with_detail 返回 ResolutionResult
- batch_resolve 批量解决
- default_similarity 字符级 Jaccard
- 自定义 similarity_func 注入
- 确定性（相同输入多次一致）
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from server.async_comm.conflict_resolver import (
    ConflictResolver,
    ResolutionResult,
    default_similarity,
)
from server.async_comm.constants import (
    DEFAULT_AUTO_CONFIRM_THRESHOLD,
    DEFAULT_CONFLICT_THRESHOLD,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REVISED,
)


class TestConflictResolverInit:
    """初始化与阈值校验。"""

    def test_default_thresholds(self):
        """默认 auto_confirm=0.8 / conflict=0.3。"""
        resolver = ConflictResolver()
        assert resolver.auto_confirm_threshold == DEFAULT_AUTO_CONFIRM_THRESHOLD
        assert resolver.conflict_threshold == DEFAULT_CONFLICT_THRESHOLD

    def test_custom_thresholds(self):
        """自定义阈值。"""
        resolver = ConflictResolver(
            auto_confirm_threshold=0.9, conflict_threshold=0.1
        )
        assert resolver.auto_confirm_threshold == 0.9
        assert resolver.conflict_threshold == 0.1

    def test_auto_confirm_le_conflict_raises(self):
        """auto_confirm_threshold <= conflict_threshold 抛 ValueError。"""
        with pytest.raises(ValueError, match="must be greater than"):
            ConflictResolver(auto_confirm_threshold=0.3, conflict_threshold=0.5)

    def test_equal_thresholds_raises(self):
        """两阈值相等也抛 ValueError。"""
        with pytest.raises(ValueError, match="must be greater than"):
            ConflictResolver(
                auto_confirm_threshold=0.5, conflict_threshold=0.5
            )

    def test_default_similarity_func_used(self):
        """未注入 similarity_func 时使用 default_similarity。"""
        resolver = ConflictResolver()
        assert resolver.similarity_func is default_similarity

    def test_custom_similarity_func_stored(self):
        """注入的 similarity_func 被保留。"""
        custom = MagicMock(return_value=0.5)
        resolver = ConflictResolver(similarity_func=custom)
        assert resolver.similarity_func is custom


class TestConflictResolverResolve:
    """resolve 三区间决策。"""

    def test_high_similarity_confirmed(self):
        """相似度 >= 0.8 → confirmed（使用 mock 精确控制）。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        decision, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_CONFIRMED

    def test_low_similarity_needs_human_review(self):
        """相似度 <= 0.3 → needs_human_review。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.2)
        )
        decision, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_NEEDS_HUMAN_REVIEW

    def test_middle_similarity_revised(self):
        """中间区间 → revised。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.5)
        )
        decision, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_REVISED

    def test_boundary_exactly_auto_confirm(self):
        """边界值：正好 0.8 → confirmed（>= 阈值）。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.8)
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_CONFIRMED

    def test_boundary_exactly_conflict(self):
        """边界值：正好 0.3 → needs_human_review（<= 阈值）。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.3)
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_NEEDS_HUMAN_REVIEW

    def test_note_contains_similarity_value(self):
        """note 包含相似度数值。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        _, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert "0.85" in note

    def test_note_contains_decision_reason(self):
        """note 包含决策原因关键词。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        _, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert "auto_confirmed" in note
        assert "0.8" in note

    def test_note_contains_event_id_when_provided(self):
        """提供 event_id 时 note 包含它。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        _, note = resolver.resolve(
            simulated_answer="a",
            real_answer="b",
            event_id="evt-123",
        )
        assert "evt-123" in note

    def test_note_no_event_id_when_empty(self):
        """未提供 event_id 时 note 不含 event_id 字段。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        _, note = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert "event_id" not in note

    def test_real_text_confirmed(self):
        """真实文本完全相同 → confirmed（用 default_similarity）。"""
        resolver = ConflictResolver()
        text = "今天天气真好适合出去玩"
        decision, _ = resolver.resolve(
            simulated_answer=text, real_answer=text
        )
        assert decision == EVENT_CONFIRMED

    def test_real_text_disjoint_needs_review(self):
        """真实文本完全不相干字符 → needs_human_review。"""
        resolver = ConflictResolver()
        decision, _ = resolver.resolve(
            simulated_answer="abc", real_answer="xyz"
        )
        assert decision == EVENT_NEEDS_HUMAN_REVIEW


class TestConflictResolverResolveWithDetail:
    """resolve_with_detail 返回 ResolutionResult。"""

    def test_returns_resolution_result_instance(self):
        """返回 ResolutionResult 实例。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.5)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert isinstance(result, ResolutionResult)

    def test_result_has_decision(self):
        """结果含 decision 字段。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert result.decision == EVENT_CONFIRMED

    def test_result_has_similarity(self):
        """结果含 similarity 字段。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.42)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert result.similarity == pytest.approx(0.42)

    def test_result_has_note(self):
        """结果含 note 字段。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert result.note
        assert "0.85" in result.note

    def test_similarity_in_range_high(self):
        """similarity 在 [0.0, 1.0] 范围内（高端）。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=1.0)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert 0.0 <= result.similarity <= 1.0

    def test_similarity_in_range_low(self):
        """similarity 在 [0.0, 1.0] 范围内（低端）。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.0)
        )
        result = resolver.resolve_with_detail(
            simulated_answer="a", real_answer="b"
        )
        assert 0.0 <= result.similarity <= 1.0

    def test_default_note_is_empty_string(self):
        """ResolutionResult 默认 note 为空字符串。"""
        result = ResolutionResult(decision=EVENT_CONFIRMED, similarity=0.9)
        assert result.note == ""


class TestConflictResolverBatchResolve:
    """batch_resolve 批量解决。"""

    def test_returns_corresponding_count(self):
        """批量解决返回对应数量的结果。"""
        resolver = ConflictResolver()
        items = [
            {"simulated_answer": "abc", "real_answer": "abc"},
            {"simulated_answer": "abc", "real_answer": "xyz"},
            {"simulated_answer": "abcd", "real_answer": "abce"},
        ]
        results = resolver.batch_resolve(items)
        assert len(results) == 3
        assert all(isinstance(r, ResolutionResult) for r in results)

    def test_each_result_matches_single_resolve(self):
        """每个批量结果与单独 resolve_with_detail 一致。"""
        resolver = ConflictResolver()
        items = [
            {"simulated_answer": "hello", "real_answer": "hello"},
            {"simulated_answer": "abc", "real_answer": "xyz"},
            {"simulated_answer": "abcd", "real_answer": "abce"},
        ]
        batch_results = resolver.batch_resolve(items)
        single_results = [
            resolver.resolve_with_detail(
                simulated_answer=item["simulated_answer"],
                real_answer=item["real_answer"],
                event_id=item.get("event_id", ""),
            )
            for item in items
        ]
        for batch, single in zip(batch_results, single_results):
            assert batch.decision == single.decision
            assert batch.similarity == pytest.approx(single.similarity)
            assert batch.note == single.note

    def test_empty_list_returns_empty(self):
        """空列表返回空列表。"""
        resolver = ConflictResolver()
        assert resolver.batch_resolve([]) == []

    def test_with_event_ids(self):
        """带 event_id 的批量解决。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.85)
        )
        items = [
            {
                "simulated_answer": "a",
                "real_answer": "b",
                "event_id": "evt-1",
            },
            {
                "simulated_answer": "c",
                "real_answer": "d",
                "event_id": "evt-2",
            },
        ]
        results = resolver.batch_resolve(items)
        assert "evt-1" in results[0].note
        assert "evt-2" in results[1].note

    def test_missing_keys_treated_as_empty(self):
        """缺少的 key 被当作空字符串处理。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.5)
        )
        items = [{}]
        results = resolver.batch_resolve(items)
        assert len(results) == 1
        assert results[0].decision == EVENT_REVISED


class TestDefaultSimilarity:
    """default_similarity 字符级 Jaccard。"""

    def test_identical_returns_one(self):
        """完全相同返回 1.0。"""
        assert default_similarity("hello", "hello") == 1.0

    def test_disjoint_returns_zero(self):
        """完全不同返回 0.0。"""
        assert default_similarity("abc", "xyz") == 0.0

    def test_partial_returns_between_zero_and_one(self):
        """部分相同返回 (0, 1) 之间。"""
        sim = default_similarity("abcd", "abef")
        assert 0.0 < sim < 1.0

    def test_both_empty_returns_one(self):
        """两个空字符串返回 1.0。"""
        assert default_similarity("", "") == 1.0

    def test_one_empty_returns_zero(self):
        """一个空一个非空返回 0.0。"""
        assert default_similarity("", "abc") == 0.0
        assert default_similarity("abc", "") == 0.0

    def test_case_insensitive(self):
        """大小写不敏感。"""
        assert default_similarity("Hello", "hello") == 1.0
        assert default_similarity("ABC", "abc") == 1.0

    def test_chinese_text(self):
        """中文文本相似度计算。"""
        sim = default_similarity("今天天气真好", "今天天气不好")
        assert 0.0 < sim < 1.0
        # 完全相同的中文返回 1.0
        assert default_similarity("你好世界", "你好世界") == 1.0

    def test_chinese_disjoint(self):
        """完全不相同的中文返回 0.0。"""
        assert default_similarity("你好", "世界") == 0.0

    def test_jaccard_value_correctness(self):
        """Jaccard 值数学正确性：|A∩B| / |A∪B|。"""
        # "abc" → {a,b,c}, "bcd" → {b,c,d}
        # 交集 {b,c} = 2，并集 {a,b,c,d} = 4 → 0.5
        assert default_similarity("abc", "bcd") == 0.5

    def test_returns_float(self):
        """返回值为 float 类型。"""
        result = default_similarity("a", "b")
        assert isinstance(result, float)


class TestConflictResolverCustomSimilarity:
    """自定义 similarity_func 注入。"""

    def test_custom_func_called(self):
        """自定义 similarity_func 被调用。"""
        mock_func = MagicMock(return_value=0.5)
        resolver = ConflictResolver(similarity_func=mock_func)
        resolver.resolve(simulated_answer="a", real_answer="b")
        mock_func.assert_called_once_with("a", "b")

    def test_custom_func_called_with_correct_args(self):
        """自定义函数接收正确的参数顺序。"""
        mock_func = MagicMock(return_value=0.5)
        resolver = ConflictResolver(similarity_func=mock_func)
        resolver.resolve(
            simulated_answer="sim_text", real_answer="real_text"
        )
        mock_func.assert_called_once_with("sim_text", "real_text")

    def test_custom_func_high_value_confirmed(self):
        """自定义函数返回高值 → confirmed。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.95)
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_CONFIRMED

    def test_custom_func_low_value_needs_review(self):
        """自定义函数返回低值 → needs_human_review。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.05)
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_NEEDS_HUMAN_REVIEW

    def test_custom_func_middle_value_revised(self):
        """自定义函数返回中间值 → revised。"""
        resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.5)
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_REVISED

    def test_custom_func_affects_decision(self):
        """同一输入下，不同 similarity_func 返回值影响决策。"""
        text_a = "same_a"
        text_b = "same_b"
        high_resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.9)
        )
        low_resolver = ConflictResolver(
            similarity_func=MagicMock(return_value=0.1)
        )
        decision_high, _ = high_resolver.resolve(
            simulated_answer=text_a, real_answer=text_b
        )
        decision_low, _ = low_resolver.resolve(
            simulated_answer=text_a, real_answer=text_b
        )
        assert decision_high == EVENT_CONFIRMED
        assert decision_low == EVENT_NEEDS_HUMAN_REVIEW

    def test_custom_thresholds_with_mock(self):
        """自定义阈值与 mock 配合测试各区间。"""
        resolver = ConflictResolver(
            auto_confirm_threshold=0.95,
            conflict_threshold=0.05,
            similarity_func=MagicMock(return_value=0.5),
        )
        decision, _ = resolver.resolve(
            simulated_answer="a", real_answer="b"
        )
        assert decision == EVENT_REVISED


class TestConflictResolverDeterminism:
    """确定性（相同输入多次一致）。"""

    def test_resolve_deterministic(self):
        """resolve 相同输入多次调用结果一致。"""
        resolver = ConflictResolver()
        results = [
            resolver.resolve(
                simulated_answer="hello world", real_answer="hello there"
            )
            for _ in range(10)
        ]
        decisions = [r[0] for r in results]
        notes = [r[1] for r in results]
        assert len(set(decisions)) == 1
        assert len(set(notes)) == 1

    def test_resolve_with_detail_deterministic(self):
        """resolve_with_detail 相同输入多次调用结果一致。"""
        resolver = ConflictResolver()
        results = [
            resolver.resolve_with_detail(
                simulated_answer="你好世界", real_answer="你好地球"
            )
            for _ in range(10)
        ]
        decisions = [r.decision for r in results]
        similarities = [r.similarity for r in results]
        notes = [r.note for r in results]
        assert len(set(decisions)) == 1
        assert len(set(similarities)) == 1
        assert len(set(notes)) == 1

    def test_batch_resolve_deterministic(self):
        """batch_resolve 相同输入多次调用结果一致。"""
        resolver = ConflictResolver()
        items = [
            {"simulated_answer": "abc", "real_answer": "abc"},
            {"simulated_answer": "abc", "real_answer": "xyz"},
        ]
        first = resolver.batch_resolve(items)
        second = resolver.batch_resolve(items)
        assert len(first) == len(second)
        for a, b in zip(first, second):
            assert a.decision == b.decision
            assert a.similarity == b.similarity
            assert a.note == b.note

    def test_default_similarity_deterministic(self):
        """default_similarity 相同输入多次调用结果一致。"""
        results = [
            default_similarity("test input", "test other") for _ in range(10)
        ]
        assert len(set(results)) == 1

    def test_different_resolvers_same_input_same_output(self):
        """两个独立 resolver 实例对相同输入产生相同结果。"""
        r1 = ConflictResolver()
        r2 = ConflictResolver()
        result1 = r1.resolve_with_detail(
            simulated_answer="hello", real_answer="world"
        )
        result2 = r2.resolve_with_detail(
            simulated_answer="hello", real_answer="world"
        )
        assert result1.decision == result2.decision
        assert result1.similarity == result2.similarity
        assert result1.note == result2.note
