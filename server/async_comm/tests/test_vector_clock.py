"""Task 11 测试：VectorClock 版本向量。

覆盖 increment / merge / compare（before/after/equal/concurrent）/ 序列化。
"""

from __future__ import annotations

from server.async_comm.types import VectorClock


class TestVectorClockIncrement:
    """increment 递增逻辑。"""

    def test_increment_new_peer(self):
        """递增一个新 peer_id，counter 从 0 变为 1。"""
        vc = VectorClock()
        vc.increment("alice")
        assert vc.counters == {"alice": 1}

    def test_increment_existing_peer(self):
        """递增已存在的 peer_id，counter 累加。"""
        vc = VectorClock(counters={"alice": 1})
        vc.increment("alice")
        assert vc.counters == {"alice": 2}

    def test_increment_multiple_peers(self):
        """递增多个 peer_id，互不影响。"""
        vc = VectorClock()
        vc.increment("alice")
        vc.increment("bob")
        vc.increment("alice")
        assert vc.counters == {"alice": 2, "bob": 1}

    def test_increment_returns_none(self):
        """increment 无返回值（原地修改）。"""
        vc = VectorClock()
        result = vc.increment("alice")
        assert result is None


class TestVectorClockMerge:
    """merge 合并逻辑。"""

    def test_merge_disjoint_peers(self):
        """合并两个不重叠的 peer 集合。"""
        a = VectorClock(counters={"alice": 2})
        b = VectorClock(counters={"bob": 3})
        merged = a.merge(b)
        assert merged.counters == {"alice": 2, "bob": 3}

    def test_merge_takes_max(self):
        """合并时取各 peer 的最大值。"""
        a = VectorClock(counters={"alice": 5, "bob": 1})
        b = VectorClock(counters={"alice": 2, "bob": 4})
        merged = a.merge(b)
        assert merged.counters == {"alice": 5, "bob": 4}

    def test_merge_does_not_mutate_self(self):
        """merge 不修改 self。"""
        a = VectorClock(counters={"alice": 1})
        b = VectorClock(counters={"bob": 2})
        a.merge(b)
        assert a.counters == {"alice": 1}

    def test_merge_does_not_mutate_other(self):
        """merge 不修改 other。"""
        a = VectorClock(counters={"alice": 1})
        b = VectorClock(counters={"bob": 2})
        a.merge(b)
        assert b.counters == {"bob": 2}

    def test_merge_empty(self):
        """与空向量合并，结果等于自身。"""
        a = VectorClock(counters={"alice": 3})
        empty = VectorClock()
        merged = a.merge(empty)
        assert merged.counters == {"alice": 3}

    def test_merge_both_empty(self):
        """两个空向量合并结果为空。"""
        merged = VectorClock().merge(VectorClock())
        assert merged.counters == {}

    def test_merge_returns_new_instance(self):
        """merge 返回新的 VectorClock 实例。"""
        a = VectorClock(counters={"alice": 1})
        b = VectorClock(counters={"bob": 2})
        merged = a.merge(b)
        assert merged is not a
        assert merged is not b
        assert isinstance(merged, VectorClock)


class TestVectorClockCompare:
    """compare 因果关系比较。"""

    def test_equal_same_counters(self):
        """相同向量 → equal。"""
        a = VectorClock(counters={"alice": 2, "bob": 3})
        b = VectorClock(counters={"alice": 2, "bob": 3})
        assert a.compare(b) == "equal"

    def test_equal_both_empty(self):
        """两个空向量 → equal。"""
        assert VectorClock().compare(VectorClock()) == "equal"

    def test_equal_with_implicit_zero(self):
        """隐式零值相等 → equal（一方有 peer，另一方没有但视为 0）。"""
        a = VectorClock(counters={"alice": 0})
        b = VectorClock()
        assert a.compare(b) == "equal"

    def test_before_strictly_less(self):
        """self 所有 counter <= other 且至少一个 < → before。"""
        a = VectorClock(counters={"alice": 1, "bob": 2})
        b = VectorClock(counters={"alice": 2, "bob": 2})
        assert a.compare(b) == "before"

    def test_before_subset(self):
        """self 是 other 的子集（缺少的 peer 视为 0）→ before。"""
        a = VectorClock(counters={"alice": 1})
        b = VectorClock(counters={"alice": 1, "bob": 2})
        assert a.compare(b) == "before"

    def test_after_strictly_greater(self):
        """self 所有 counter >= other 且至少一个 > → after。"""
        a = VectorClock(counters={"alice": 3, "bob": 2})
        b = VectorClock(counters={"alice": 2, "bob": 2})
        assert a.compare(b) == "after"

    def test_after_superset(self):
        """self 是 other 的超集（多的 peer counter > 0）→ after。"""
        a = VectorClock(counters={"alice": 1, "bob": 2})
        b = VectorClock(counters={"alice": 1})
        assert a.compare(b) == "after"

    def test_concurrent_divergent(self):
        """self 与 other 在不同 peer 上各有优势 → concurrent。"""
        a = VectorClock(counters={"alice": 3, "bob": 1})
        b = VectorClock(counters={"alice": 1, "bob": 3})
        assert a.compare(b) == "concurrent"

    def test_concurrent_symmetric(self):
        """concurrent 关系对称：a.compare(b) == b.compare(a) == concurrent。"""
        a = VectorClock(counters={"alice": 2, "bob": 1})
        b = VectorClock(counters={"alice": 1, "bob": 2})
        assert a.compare(b) == "concurrent"
        assert b.compare(a) == "concurrent"

    def test_before_after_symmetric(self):
        """before/after 关系对称：a before b ⟺ b after a。"""
        a = VectorClock(counters={"alice": 1})
        b = VectorClock(counters={"alice": 2})
        assert a.compare(b) == "before"
        assert b.compare(a) == "after"

    def test_compare_is_deterministic(self):
        """相同输入多次比较结果一致（确定性）。"""
        a = VectorClock(counters={"alice": 3, "bob": 1, "carol": 2})
        b = VectorClock(counters={"alice": 1, "bob": 3, "carol": 2})
        results = [a.compare(b) for _ in range(10)]
        assert all(r == "concurrent" for r in results)


class TestVectorClockSerialization:
    """to_dict / from_dict 序列化。"""

    def test_to_dict_returns_copy(self):
        """to_dict 返回 counters 的副本。"""
        vc = VectorClock(counters={"alice": 1, "bob": 2})
        d = vc.to_dict()
        assert d == {"alice": 1, "bob": 2}
        d["alice"] = 99
        assert vc.counters["alice"] == 1

    def test_to_dict_empty(self):
        """空向量序列化为空 dict。"""
        assert VectorClock().to_dict() == {}

    def test_from_dict_creates_independent_copy(self):
        """from_dict 创建独立副本，不共享引用。"""
        data = {"alice": 1, "bob": 2}
        vc = VectorClock.from_dict(data)
        assert vc.counters == {"alice": 1, "bob": 2}
        data["alice"] = 99
        assert vc.counters["alice"] == 1

    def test_from_dict_empty(self):
        """空 dict 反序列化为空向量。"""
        vc = VectorClock.from_dict({})
        assert vc.counters == {}

    def test_roundtrip(self):
        """序列化 → 反序列化往返一致。"""
        original = VectorClock(counters={"alice": 5, "bob": 3, "carol": 7})
        data = original.to_dict()
        restored = VectorClock.from_dict(data)
        assert restored.counters == original.counters
        assert restored.compare(original) == "equal"

    def test_roundtrip_empty(self):
        """空向量往返一致。"""
        original = VectorClock()
        restored = VectorClock.from_dict(original.to_dict())
        assert restored.compare(original) == "equal"
