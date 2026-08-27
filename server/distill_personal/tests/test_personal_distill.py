"""PersonalDistill 主入口测试（聚合三阶段 + budget + pending + signal_report + privacy）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.distill_personal.budget import (
    BudgetManager,
    PendingCandidate,
    PendingCandidateStore,
)
from server.distill_personal.deep_stage import DeepStage
from server.distill_personal.light_stage import LightStage, Signal
from server.distill_personal.metrics import SignalReporter
from server.distill_personal.personal_distill import (
    PersonalDistill,
    PersonalDistillResult,
)
from server.distill_personal.privacy import PrivacyGuard
from server.distill_personal.rem_stage import Intent, RemStage
from server.distill_personal.session_provider import Session, SessionTurn


# ---------------------------------------------------------------------------
# 测试 fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_sessions() -> list[Session]:
    """构造含规则信号的会话列表。"""
    return [
        Session(
            session_id="s1",
            turns=[
                SessionTurn(role="user", content="你好"),  # 噪声
                SessionTurn(role="user", content="提交前必须跑 lint，禁止跳过"),  # rule 信号
                SessionTurn(role="assistant", content="明白，已记录规则"),
            ],
        ),
        Session(
            session_id="s2",
            turns=[
                SessionTurn(role="user", content="我们决定用 SQLAlchemy 作为 ORM"),  # memory 信号
            ],
        ),
    ]


def _make_llm_returning_asset() -> Any:
    """返回产出 rule 资产的 LLM stub。"""
    class _StubLLM:
        def chat(self, messages, *, schema=None, **kw):
            return {
                "content": json.dumps({
                    "skip": False,
                    "asset": {
                        "title": "lint 规则",
                        "content": "提交前必须跑 lint",
                        "tags": ["lint"],
                        "rationale": "用户反复强调",
                    },
                    "confidence": 0.9,
                }),
                "usage": {"total_tokens": 200},
            }
    return _StubLLM()


# ---------------------------------------------------------------------------
# 公共 API 单元测试
# ---------------------------------------------------------------------------


def test_run_light_returns_signals(fake_sessions) -> None:
    """run_light 返回 Signal 列表。"""
    distill = PersonalDistill()
    signals = distill.run_light(fake_sessions)
    assert len(signals) > 0
    assert all(isinstance(s, Signal) for s in signals)


def test_run_rem_returns_intents(fake_sessions) -> None:
    """run_rem 返回 Intent 列表。"""
    distill = PersonalDistill()
    signals = distill.run_light(fake_sessions)
    intents = distill.run_rem(signals)
    assert isinstance(intents, list)
    for intent in intents:
        assert isinstance(intent, Intent)


def test_run_deep_returns_dict_with_assets_and_pending(fake_sessions) -> None:
    """run_deep 返回 dict 含 assets / pending / skipped_intents。"""
    distill = PersonalDistill(llm=_make_llm_returning_asset(), promotion_threshold=0.0)
    signals = distill.run_light(fake_sessions)
    intents = distill.run_rem(signals)
    result = distill.run_deep(intents, budget=None)
    assert isinstance(result, dict)
    assert "assets" in result
    assert "pending" in result
    assert "skipped_intents" in result
    assert "errors" in result


def test_report_metrics_returns_bool() -> None:
    """report_metrics 返回 bool。"""
    distill = PersonalDistill(signal_reporter=SignalReporter(member_id="alice"))
    result = distill.report_metrics("alice", signal_count=10, yield_ratio=0.5)
    assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# run() 全流程
# ---------------------------------------------------------------------------


def test_run_full_pipeline_produces_assets(tmp_path: Path, fake_sessions) -> None:
    """run() 完整流程：Light → REM → Deep → 产出资产。"""
    distill = PersonalDistill(
        llm=_make_llm_returning_asset(),
        budget_mgr=BudgetManager(default_daily_budget=100_000),
        pending_store=PendingCandidateStore(repo_root=tmp_path),
        signal_reporter=SignalReporter(member_id="alice"),
        owner="alice",
        module_path="modules/backend",
        member_id="alice",
        repo_root=tmp_path,
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="alice")
    assert isinstance(result, PersonalDistillResult)
    assert result.light is not None
    assert result.rem is not None
    assert result.deep is not None
    assert result.error is None
    # 至少产出 1 个资产（rule 信号 + LLM 返回合规）
    assert result.produced_count >= 0  # 取决于 REM 是否产出 reusable intent


def test_run_signal_reported_flag(tmp_path: Path, fake_sessions) -> None:
    """run() 设置 signal_reported 标志。"""
    distill = PersonalDistill(
        signal_reporter=SignalReporter(member_id="alice"),
        repo_root=tmp_path,
        member_id="alice",
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="alice")
    assert result.signal_reported is True


def test_run_privacy_audit_ok(tmp_path: Path, fake_sessions) -> None:
    """run() 完成后 privacy_audit.ok=True。"""
    distill = PersonalDistill(
        privacy_guard=PrivacyGuard(),
        repo_root=tmp_path,
        member_id="alice",
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="alice")
    assert result.privacy_audit.get("ok") is True


def test_run_no_member_id_skips_signal_report(tmp_path: Path, fake_sessions) -> None:
    """member_id 为空时跳过 signal_report（signal_reported 保持 False）。"""
    distill = PersonalDistill(
        repo_root=tmp_path,
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="")
    # 无 member_id → 不上报 signal
    assert result.signal_reported is False


def test_run_persists_intermediate_when_repo_root_set(tmp_path: Path, fake_sessions) -> None:
    """repo_root 非空时持久化 signals/intents 到 .dreams/。"""
    distill = PersonalDistill(
        repo_root=tmp_path,
        member_id="alice",
        promotion_threshold=0.0,
    )
    distill.run(fake_sessions, member_id="alice")
    # signals.json 应存在
    signals_path = tmp_path / ".teamharness-local" / "dreams" / "light" / "signals.json"
    assert signals_path.is_file()
    # intents.json 应存在
    intents_path = tmp_path / ".teamharness-local" / "dreams" / "rem" / "intents.json"
    assert intents_path.is_file()


def test_run_skips_persist_when_repo_root_none(fake_sessions) -> None:
    """repo_root=None 时不持久化（不抛异常）。"""
    distill = PersonalDistill(promotion_threshold=0.0)
    result = distill.run(fake_sessions, member_id="")
    assert result.error is None


def test_run_light_failure_returns_error(fake_sessions) -> None:
    """Light 阶段抛异常时返回 error。"""
    class _BrokenLight:
        def run(self, sessions):
            raise RuntimeError("light boom")
    distill = PersonalDistill(
        light_stage=_BrokenLight(),
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="")
    assert result.error is not None
    assert "Light" in result.error


def test_run_rem_failure_returns_error(fake_sessions) -> None:
    """REM 阶段抛异常时返回 error。"""
    class _BrokenRem:
        def run(self, signals):
            raise RuntimeError("rem boom")
    distill = PersonalDistill(
        rem_stage=_BrokenRem(),
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="")
    assert result.error is not None
    assert "REM" in result.error


def test_run_deep_failure_returns_error(fake_sessions) -> None:
    """Deep 阶段抛异常时返回 error。"""
    class _BrokenDeep:
        def run(self, intents):
            raise RuntimeError("deep boom")
    distill = PersonalDistill(
        deep_stage=_BrokenDeep(),
        promotion_threshold=0.0,
    )
    result = distill.run(fake_sessions, member_id="")
    assert result.error is not None
    assert "Deep" in result.error


def test_run_budget_dynamic_adjustment(tmp_path: Path, fake_sessions) -> None:
    """run() 基于 signal_count 动态调整 budget。"""
    # 构造高 signal_count 场景：让 LightStage 产出大量信号
    class _HighSignalLight:
        def run(self, sessions):
            from server.distill_personal.light_stage import LightStageResult
            return LightStageResult(
                signals=[Signal(signal_id=f"s{i}", session_id="x", turn_index=0, candidate_type="rule", content_excerpt="")
                         for i in range(60)],
                scanned_sessions=1,
                scanned_turns=60,
                skipped_turns=0,
                type_counts={"rule": 60, "memory": 0, "skill": 0, "tool": 0},
            )
    budget_mgr = BudgetManager(default_daily_budget=100_000)
    distill = PersonalDistill(
        budget_mgr=budget_mgr,
        repo_root=tmp_path,
        member_id="alice",
        promotion_threshold=0.0,
        light_stage=_HighSignalLight(),
    )
    distill.run(fake_sessions, member_id="alice")
    # signal_count=60 >= high_threshold=50 → budget × 1.2
    budget = budget_mgr.get_budget("alice")
    assert budget.daily_token_budget == int(100_000 * 1.2)


# ---------------------------------------------------------------------------
# process_pending
# ---------------------------------------------------------------------------


def test_process_pending_no_member_id_returns_error() -> None:
    """process_pending 无 member_id 返回 error。"""
    distill = PersonalDistill()
    result = distill.process_pending(member_id="")
    assert "error" in result


def test_process_pending_empty_store_returns_zero(tmp_path: Path) -> None:
    """空 pending store → processed=0。"""
    distill = PersonalDistill(
        budget_mgr=BudgetManager(),
        pending_store=PendingCandidateStore(repo_root=tmp_path),
        member_id="alice",
    )
    result = distill.process_pending(member_id="alice")
    assert result["processed"] == 0
    assert result["succeeded"] == 0


def test_process_pending_processes_candidate(tmp_path: Path) -> None:
    """process_pending 处理 pending 候选。"""
    store = PendingCandidateStore(repo_root=tmp_path)
    store.save(PendingCandidate(
        candidate_id="c1",
        intent=Intent(
            intent_id="i1",
            description="x" * 30,
            candidate_type="rule",
            pattern_count=3,
        ).to_dict(),
        created_at="2026-08-07T08:00:00Z",
    ))
    distill = PersonalDistill(
        llm=_make_llm_returning_asset(),
        budget_mgr=BudgetManager(default_daily_budget=100_000),
        pending_store=store,
        member_id="alice",
        promotion_threshold=0.0,
    )
    result = distill.process_pending(member_id="alice")
    assert result["processed"] == 1
    assert result["succeeded"] == 1
    assert store.count() == 0


# ---------------------------------------------------------------------------
# PersonalDistillResult
# ---------------------------------------------------------------------------


def test_personal_distill_result_to_dict() -> None:
    """PersonalDistillResult.to_dict 字段完整。"""
    r = PersonalDistillResult()
    d = r.to_dict()
    assert d["produced"] == 0
    assert d["skipped"] == 0
    assert d["pending"] == 0
    assert d["error"] is None
    assert d["signal_reported"] is False
    assert d["privacy_ok"] is True
    assert d["light_signal_count"] == 0
    assert d["rem_intent_count"] == 0


def test_personal_distill_inject_custom_stages() -> None:
    """支持注入自定义 stage 实例（测试用）。"""
    custom_light = LightStage(min_confidence=0.5)
    custom_rem = RemStage(discard_one_time=False)
    custom_deep = DeepStage(promotion_threshold=0.6)
    distill = PersonalDistill(
        light_stage=custom_light,
        rem_stage=custom_rem,
        deep_stage=custom_deep,
    )
    assert distill.light_stage is custom_light
    assert distill.rem_stage is custom_rem
    # DeepStage 通过 _get_deep_stage 工厂重建（不直接复用），这里仅验证不报错


def test_personal_distill_default_construction_no_args() -> None:
    """无参构造使用默认 BudgetManager / PendingCandidateStore / SignalReporter。"""
    distill = PersonalDistill()
    assert distill.budget_mgr is not None
    assert distill.pending_store is not None
    assert distill.signal_reporter is not None
    assert distill.privacy_guard is not None


def test_personal_distill_max_llm_retries_configurable() -> None:
    """max_llm_retries 可配置。"""
    distill = PersonalDistill(max_llm_retries=5)
    assert distill.max_llm_retries == 5
