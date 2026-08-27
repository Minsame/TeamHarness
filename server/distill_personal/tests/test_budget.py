"""budget 测试（SubTask 7.8 每成员 daily_token_budget + 超限降级 + pending 处理）。"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from server.distill_personal.budget import (
    BudgetManager,
    PendingCandidate,
    PendingCandidateStore,
    PendingProcessor,
    PendingProcessResult,
)
from server.distill_personal.llm_provider import LLMBudget


# ---------------------------------------------------------------------------
# BudgetManager
# ---------------------------------------------------------------------------


def test_budget_manager_ensure_member_creates_budget() -> None:
    """ensure_member 创建默认 budget。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    budget = mgr.ensure_member("alice")
    assert budget.member_id == "alice"
    assert budget.daily_token_budget == 100_000
    assert budget.used == 0
    assert budget.degraded is False


def test_budget_manager_ensure_member_idempotent() -> None:
    """重复 ensure_member 同一成员返回同一 budget 实例。"""
    mgr = BudgetManager()
    b1 = mgr.ensure_member("alice")
    b2 = mgr.ensure_member("alice")
    assert b1 is b2


def test_budget_manager_consume_reduces_remaining() -> None:
    """consume 扣减 remaining。"""
    mgr = BudgetManager(default_daily_budget=1000)
    actual = mgr.consume("alice", 300)
    assert actual == 300
    budget = mgr.get_budget("alice")
    assert budget.used == 300
    assert budget.remaining == 700
    assert not budget.exhausted


def test_budget_manager_consume_more_than_remaining_returns_actual() -> None:
    """consume 超过 remaining 时返回实际消费量，并标记 degraded。"""
    mgr = BudgetManager(default_daily_budget=500)
    actual = mgr.consume("alice", 800)
    assert actual == 500
    budget = mgr.get_budget("alice")
    assert budget.used == 500
    assert budget.remaining == 0
    assert budget.exhausted
    assert budget.degraded


def test_budget_manager_is_degraded_after_exhaustion() -> None:
    """budget 耗尽后 is_degraded=True。"""
    mgr = BudgetManager(default_daily_budget=100)
    assert not mgr.is_degraded("alice")
    mgr.consume("alice", 100)
    assert mgr.is_degraded("alice")


def test_budget_manager_set_daily_budget_updates() -> None:
    """set_daily_budget 热更新成员预算。"""
    mgr = BudgetManager(default_daily_budget=1000)
    mgr.set_daily_budget("alice", 2000)
    budget = mgr.get_budget("alice")
    assert budget.daily_token_budget == 2000


def test_budget_manager_reset_member_clears_state() -> None:
    """reset_member 清零 used + degraded。"""
    mgr = BudgetManager(default_daily_budget=500)
    mgr.consume("alice", 500)
    assert mgr.is_degraded("alice")
    mgr.reset_member("alice")
    budget = mgr.get_budget("alice")
    assert budget.used == 0
    assert not budget.degraded


def test_budget_manager_reset_all_clears_every_member() -> None:
    """reset_all 清零全部成员。"""
    mgr = BudgetManager(default_daily_budget=500)
    mgr.consume("alice", 200)
    mgr.consume("bob", 300)
    mgr.reset_all()
    assert mgr.get_budget("alice").used == 0
    assert mgr.get_budget("bob").used == 0


def test_budget_manager_cross_day_auto_reset() -> None:
    """budget.reset_at 已过 → get_budget 自动 reset。"""
    mgr = BudgetManager(default_daily_budget=1000)
    budget = mgr.ensure_member("alice")
    # 把 reset_at 设为已过去的时间
    past = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    budget.reset_at = past
    budget.used = 800
    budget.degraded = True
    # get_budget 触发自动 reset
    refreshed = mgr.get_budget("alice")
    assert refreshed.used == 0
    assert not refreshed.degraded
    # reset_at 更新为次日 00:00 UTC
    assert refreshed.reset_at  # 非空


def test_budget_manager_cross_day_no_reset_when_reset_at_future() -> None:
    """reset_at 在未来 → 不触发 reset。"""
    mgr = BudgetManager(default_daily_budget=1000)
    budget = mgr.ensure_member("alice")
    future = (datetime.now(timezone.utc) + timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
    budget.reset_at = future
    budget.used = 500
    refreshed = mgr.get_budget("alice")
    assert refreshed.used == 500


def test_budget_manager_invalid_reset_at_skipped() -> None:
    """reset_at 非法字符串时跳过 reset（不抛异常）。"""
    mgr = BudgetManager(default_daily_budget=1000)
    budget = mgr.ensure_member("alice")
    budget.reset_at = "not-a-date"
    budget.used = 500
    refreshed = mgr.get_budget("alice")
    assert refreshed.used == 500  # 未 reset


# ---------------------------------------------------------------------------
# PendingCandidate
# ---------------------------------------------------------------------------


def test_pending_candidate_to_dict_roundtrip() -> None:
    """PendingCandidate to_dict / from_dict 往返。"""
    cand = PendingCandidate(
        candidate_id="c1",
        intent={"intent_id": "i1", "description": "test"},
        created_at="2026-08-07T10:00:00Z",
        reason="budget_exhausted",
        source_session_ids=["s1", "s2"],
    )
    d = cand.to_dict()
    assert d["candidate_id"] == "c1"
    assert d["intent"] == {"intent_id": "i1", "description": "test"}
    assert d["reason"] == "budget_exhausted"
    assert d["source_session_ids"] == ["s1", "s2"]

    restored = PendingCandidate.from_dict(d)
    assert restored.candidate_id == "c1"
    assert restored.intent == {"intent_id": "i1", "description": "test"}
    assert restored.source_session_ids == ["s1", "s2"]


def test_pending_candidate_from_dict_defaults() -> None:
    """from_dict 缺字段时用默认值。"""
    cand = PendingCandidate.from_dict({"candidate_id": "c1"})
    assert cand.candidate_id == "c1"
    assert cand.intent == {}
    assert cand.reason == "budget_exhausted"
    assert cand.source_session_ids == []


# ---------------------------------------------------------------------------
# PendingCandidateStore
# ---------------------------------------------------------------------------


def test_pending_store_save_and_load(tmp_path: Path) -> None:
    """save + load 往返。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    cand = PendingCandidate(
        candidate_id="c1",
        intent={"intent_id": "i1"},
        created_at="2026-08-07T10:00:00Z",
    )
    cid = store.save(cand)
    assert cid == "c1"

    loaded = store.load("c1")
    assert loaded is not None
    assert loaded.candidate_id == "c1"
    assert loaded.intent == {"intent_id": "i1"}


def test_pending_store_load_nonexistent_returns_none(tmp_path: Path) -> None:
    """load 不存在的 id 返回 None。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    assert store.load("nope") is None


def test_pending_store_list_ids_sorted_by_created_at(tmp_path: Path) -> None:
    """list_ids 按 created_at 升序（先入先处理）。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    # 后写入的 created_at 更早 → 应排在前
    store.save(PendingCandidate(
        candidate_id="c2",
        intent={},
        created_at="2026-08-07T09:00:00Z",
    ))
    store.save(PendingCandidate(
        candidate_id="c1",
        intent={},
        created_at="2026-08-07T08:00:00Z",
    ))
    ids = store.list_ids()
    assert ids == ["c1", "c2"]


def test_pending_store_list_all_loads_candidates(tmp_path: Path) -> None:
    """list_all 加载全部 PendingCandidate。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    store.save(PendingCandidate(candidate_id="c1", intent={}, created_at="2026-08-07T08:00:00Z"))
    store.save(PendingCandidate(candidate_id="c2", intent={}, created_at="2026-08-07T09:00:00Z"))
    all_cands = store.list_all()
    assert len(all_cands) == 2
    assert all_cands[0].candidate_id == "c1"


def test_pending_store_delete(tmp_path: Path) -> None:
    """delete 删除候选。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    store.save(PendingCandidate(candidate_id="c1", intent={}, created_at="2026-08-07T08:00:00Z"))
    assert store.delete("c1") is True
    assert store.load("c1") is None
    assert store.delete("c1") is False  # 已删除


def test_pending_store_count(tmp_path: Path) -> None:
    """count 返回候选总数。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    assert store.count() == 0
    store.save(PendingCandidate(candidate_id="c1", intent={}, created_at="2026-08-07T08:00:00Z"))
    store.save(PendingCandidate(candidate_id="c2", intent={}, created_at="2026-08-07T09:00:00Z"))
    assert store.count() == 2


def test_pending_store_list_ids_missing_dir_returns_empty(tmp_path: Path) -> None:
    """目录不存在时 list_ids 返回空列表。"""
    store = PendingCandidateStore(pending_dir=tmp_path / "nonexistent")
    assert store.list_ids() == []
    assert store.count() == 0


def test_pending_store_load_corrupt_file_returns_none(tmp_path: Path) -> None:
    """加载损坏 JSON 文件返回 None。"""
    pending_dir = tmp_path / "pending"
    pending_dir.mkdir()
    (pending_dir / "bad.json").write_text("not json", encoding="utf-8")
    store = PendingCandidateStore(pending_dir=pending_dir)
    assert store.load("bad") is None


def test_pending_store_repo_root_resolution(tmp_path: Path) -> None:
    """通过 repo_root 构造时落到 .teamharness-local/dreams/pending/。"""
    store = PendingCandidateStore(repo_root=tmp_path)
    expected = tmp_path / ".teamharness-local" / "dreams" / "pending"
    assert store.pending_dir == expected


# ---------------------------------------------------------------------------
# PendingProcessor
# ---------------------------------------------------------------------------


def test_pending_processor_processes_all_when_budget_available(tmp_path: Path) -> None:
    """budget 充足时处理全部 pending 候选。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    store.save(PendingCandidate(
        candidate_id="c1",
        intent={"intent_id": "i1"},
        created_at="2026-08-07T08:00:00Z",
    ))
    store.save(PendingCandidate(
        candidate_id="c2",
        intent={"intent_id": "i2"},
        created_at="2026-08-07T09:00:00Z",
    ))
    processor = PendingProcessor(mgr, store)

    def callback(cand: PendingCandidate) -> dict[str, Any]:
        return {"success": True, "asset_id": "asset-" + cand.candidate_id, "error": None, "usage": {"total_tokens": 50}}

    result = processor.process_pending(member_id="alice", process_callback=callback)
    assert result.processed == 2
    assert result.succeeded == 2
    assert result.failed == 0
    assert store.count() == 0  # 全部删除


def test_pending_processor_stops_when_budget_exhausted(tmp_path: Path) -> None:
    """budget 再次耗尽时停止，剩余候选保留。"""
    mgr = BudgetManager(default_daily_budget=80)  # 只够处理 1 个
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    for i in range(3):
        store.save(PendingCandidate(
            candidate_id=f"c{i}",
            intent={"intent_id": f"i{i}"},
            created_at=f"2026-08-07T0{i}:00:00Z",
        ))
    processor = PendingProcessor(mgr, store)

    def callback(cand: PendingCandidate) -> dict[str, Any]:
        return {"success": True, "asset_id": "a", "error": None, "usage": {"total_tokens": 80}}

    result = processor.process_pending(member_id="alice", process_callback=callback)
    # 第 1 个处理消费 80，剩余 budget=0，第 2 个检测到 exhausted 停止
    assert result.processed == 1
    assert result.succeeded == 1
    assert store.count() == 2  # 剩余 2 个保留


def test_pending_processor_failed_candidate_retained(tmp_path: Path) -> None:
    """处理失败的候选保留在 pending 中。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    store.save(PendingCandidate(
        candidate_id="c1",
        intent={"intent_id": "i1"},
        created_at="2026-08-07T08:00:00Z",
    ))
    processor = PendingProcessor(mgr, store)

    def callback(cand: PendingCandidate) -> dict[str, Any]:
        return {"success": False, "asset_id": None, "error": "LLM failed"}

    result = processor.process_pending(member_id="alice", process_callback=callback)
    assert result.processed == 1
    assert result.succeeded == 0
    assert result.failed == 1
    assert store.count() == 1  # 失败候选保留


def test_pending_processor_callback_exception_counted_as_failure(tmp_path: Path) -> None:
    """回调抛异常时计为失败，不中断后续处理。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    store.save(PendingCandidate(candidate_id="c1", intent={}, created_at="2026-08-07T08:00:00Z"))
    store.save(PendingCandidate(candidate_id="c2", intent={}, created_at="2026-08-07T09:00:00Z"))
    processor = PendingProcessor(mgr, store)

    def callback(cand: PendingCandidate) -> dict[str, Any]:
        if cand.candidate_id == "c1":
            raise RuntimeError("boom")
        return {"success": True, "asset_id": "a", "error": None, "usage": {}}

    result = processor.process_pending(member_id="alice", process_callback=callback)
    assert result.processed == 2
    assert result.succeeded == 1
    assert result.failed == 1
    assert any("c1" in e for e in result.errors)


def test_pending_processor_max_process_limits_count(tmp_path: Path) -> None:
    """max_process 限制单次处理数量。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    for i in range(5):
        store.save(PendingCandidate(
            candidate_id=f"c{i}",
            intent={},
            created_at=f"2026-08-07T0{i}:00:00Z",
        ))
    processor = PendingProcessor(mgr, store)

    def callback(cand: PendingCandidate) -> dict[str, Any]:
        return {"success": True, "asset_id": "a", "error": None, "usage": {}}

    result = processor.process_pending(
        member_id="alice", process_callback=callback, max_process=2
    )
    assert result.processed == 2
    assert store.count() == 3  # 剩余 3 个


def test_pending_processor_empty_store_returns_zero_result(tmp_path: Path) -> None:
    """空 store 处理结果全为 0。"""
    mgr = BudgetManager()
    store = PendingCandidateStore(pending_dir=tmp_path / "pending")
    processor = PendingProcessor(mgr, store)
    result = processor.process_pending(
        member_id="alice", process_callback=lambda c: {"success": True}
    )
    assert result.processed == 0
    assert result.succeeded == 0


def test_pending_process_result_defaults() -> None:
    """PendingProcessResult 默认值。"""
    r = PendingProcessResult()
    assert r.processed == 0
    assert r.succeeded == 0
    assert r.failed == 0
    assert r.retained == 0
    assert r.errors == []
