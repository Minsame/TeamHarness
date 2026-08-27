"""metrics 测试（SubTask 7.9 Light 阶段候选信号计数上报）。"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from server.distill_personal.light_stage import LightStageResult, Signal
from server.distill_personal.metrics import (
    BUDGET_ADJUST_RATIO_HIGH,
    BUDGET_ADJUST_RATIO_LOW,
    SIGNAL_COUNT_HIGH_THRESHOLD,
    SIGNAL_COUNT_LOW_THRESHOLD,
    SIGNAL_REPORT_EVENT_TYPE,
    SignalReport,
    SignalReporter,
    adjust_budget_by_signal_count,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------


def test_event_type_is_distill_signal() -> None:
    """事件类型应为 distill_signal（与 adoption 的 recall/view/adopt 区分）。"""
    assert SIGNAL_REPORT_EVENT_TYPE == "distill_signal"


def test_thresholds_reasonable() -> None:
    """阈值常量取值合理。"""
    assert SIGNAL_COUNT_HIGH_THRESHOLD > SIGNAL_COUNT_LOW_THRESHOLD
    assert BUDGET_ADJUST_RATIO_HIGH > 1.0
    assert BUDGET_ADJUST_RATIO_LOW < 1.0


# ---------------------------------------------------------------------------
# SignalReport
# ---------------------------------------------------------------------------


def test_signal_report_to_dict() -> None:
    """SignalReport.to_dict 字段完整。"""
    report = SignalReport(
        member_id="alice",
        signal_count=10,
        yield_ratio=0.25,
        type_counts={"rule": 5, "memory": 5},
        scanned_sessions=3,
        scanned_turns=40,
        skipped_turns=30,
    )
    d = report.to_dict()
    assert d["member_id"] == "alice"
    assert d["signal_count"] == 10
    assert d["yield_ratio"] == 0.25
    assert d["type_counts"] == {"rule": 5, "memory": 5}
    assert d["scanned_sessions"] == 3
    assert d["scanned_turns"] == 40
    assert d["skipped_turns"] == 30


def test_signal_report_from_light_result() -> None:
    """从 LightStageResult 构造 SignalReport。"""
    result = LightStageResult(
        signals=[Signal(signal_id="s1", session_id="x", turn_index=0, candidate_type="rule", content_excerpt="")],
        scanned_sessions=2,
        scanned_turns=10,
        skipped_turns=5,
        type_counts={"rule": 1, "memory": 0, "skill": 0, "tool": 0},
    )
    report = SignalReport.from_light_result(member_id="bob", result=result)
    assert report.member_id == "bob"
    assert report.signal_count == 1
    assert report.yield_ratio == 0.1  # 1 / 10
    assert report.scanned_sessions == 2
    assert report.type_counts == {"rule": 1, "memory": 0, "skill": 0, "tool": 0}


# ---------------------------------------------------------------------------
# SignalReporter.report
# ---------------------------------------------------------------------------


class _FakeAdoptionReporter:
    """模拟 AdoptionReporter（仅需 cache_dir 与 events_log_path 属性）。"""

    def __init__(self, cache_dir: Path) -> None:
        self.cache_dir = cache_dir
        self.events_log_path = cache_dir / "adoption-events.jsonl"


def test_report_without_adoption_reporter_returns_true() -> None:
    """无 adoption_reporter 时仅记日志，返回 True（best-effort）。"""
    reporter = SignalReporter(adoption_reporter=None, member_id="alice")
    report = SignalReport(member_id="alice", signal_count=10, yield_ratio=0.5)
    assert reporter.report(report) is True


def test_report_with_adoption_reporter_writes_event(tmp_path: Path) -> None:
    """有 adoption_reporter 时写入 adoption-events.jsonl。"""
    fake = _FakeAdoptionReporter(cache_dir=tmp_path)
    reporter = SignalReporter(adoption_reporter=fake, member_id="alice")

    report = SignalReport(
        member_id="alice",
        signal_count=15,
        yield_ratio=0.3,
        type_counts={"rule": 10, "memory": 5},
    )
    assert reporter.report(report) is True

    # 验证 events_log_path 有内容
    assert fake.events_log_path.is_file()
    lines = fake.events_log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 1
    payload = json.loads(lines[0])
    assert payload["event_type"] == SIGNAL_REPORT_EVENT_TYPE
    assert payload["member_id"] == "alice"
    assert payload["metadata"]["signal_count"] == 15
    assert payload["metadata"]["yield_ratio"] == 0.3
    assert payload["metadata"]["type_counts"] == {"rule": 10, "memory": 5}
    # 不含敏感字段（asset_id / agent_id 为空，不绑定具体资产）
    assert payload["asset_id"] == ""
    assert payload["agent_id"] == ""


def test_report_appends_multiple_events(tmp_path: Path) -> None:
    """多次 report 应追加到同一文件。"""
    fake = _FakeAdoptionReporter(cache_dir=tmp_path)
    reporter = SignalReporter(adoption_reporter=fake, member_id="alice")
    for i in range(3):
        reporter.report(SignalReport(member_id="alice", signal_count=i, yield_ratio=0.0))
    lines = fake.events_log_path.read_text(encoding="utf-8").strip().split("\n")
    assert len(lines) == 3


def test_report_uses_member_id_from_report_when_present(tmp_path: Path) -> None:
    """report.member_id 优先于 SignalReporter.member_id。"""
    fake = _FakeAdoptionReporter(cache_dir=tmp_path)
    reporter = SignalReporter(adoption_reporter=fake, member_id="default")
    report = SignalReport(member_id="override", signal_count=1, yield_ratio=0.0)
    reporter.report(report)
    payload = json.loads(
        fake.events_log_path.read_text(encoding="utf-8").strip()
    )
    assert payload["member_id"] == "override"


def test_report_falls_back_to_reporter_member_id(tmp_path: Path) -> None:
    """report.member_id 为空时回退到 SignalReporter.member_id。"""
    fake = _FakeAdoptionReporter(cache_dir=tmp_path)
    reporter = SignalReporter(adoption_reporter=fake, member_id="fallback")
    report = SignalReport(member_id="", signal_count=1, yield_ratio=0.0)
    reporter.report(report)
    payload = json.loads(
        fake.events_log_path.read_text(encoding="utf-8").strip()
    )
    assert payload["member_id"] == "fallback"


def test_report_failure_returns_false(tmp_path: Path) -> None:
    """adoption_reporter 写入异常时返回 False（不阻塞提炼流程）。"""
    class _BrokenReporter:
        cache_dir = tmp_path
        # events_log_path 设为已存在的目录（open 会失败）
        events_log_path = tmp_path  # 目录，open 会抛

    reporter = SignalReporter(adoption_reporter=_BrokenReporter(), member_id="alice")
    report = SignalReport(member_id="alice", signal_count=1, yield_ratio=0.0)
    # _BrokenReporter.events_log_path 是目录，open("a") 会抛 → report 返回 False
    assert reporter.report(report) is False


# ---------------------------------------------------------------------------
# adjust_budget_by_signal_count
# ---------------------------------------------------------------------------


def test_adjust_budget_high_signal_count_increases_budget() -> None:
    """signal_count >= high_threshold → budget × high_ratio。"""
    new = adjust_budget_by_signal_count(100_000, 60)
    assert new == int(100_000 * BUDGET_ADJUST_RATIO_HIGH)


def test_adjust_budget_at_high_threshold_increases() -> None:
    """signal_count == high_threshold 也提升（>=）。"""
    new = adjust_budget_by_signal_count(100_000, SIGNAL_COUNT_HIGH_THRESHOLD)
    assert new == int(100_000 * BUDGET_ADJUST_RATIO_HIGH)


def test_adjust_budget_low_signal_count_decreases_budget() -> None:
    """signal_count < low_threshold → budget × low_ratio。"""
    new = adjust_budget_by_signal_count(100_000, 2)
    assert new == int(100_000 * BUDGET_ADJUST_RATIO_LOW)


def test_adjust_budget_midrange_unchanged() -> None:
    """low <= signal_count < high → budget 不变。"""
    new = adjust_budget_by_signal_count(100_000, 20)
    assert new == 100_000


def test_adjust_budget_custom_thresholds() -> None:
    """支持自定义阈值。"""
    new = adjust_budget_by_signal_count(
        100_000,
        30,
        high_threshold=100,
        low_threshold=10,
    )
    # 30 在 [10, 100) 之间，不变
    assert new == 100_000


def test_adjust_budget_zero_signal_count_lowers_budget() -> None:
    """signal_count=0 视为低产，降低 budget。"""
    new = adjust_budget_by_signal_count(100_000, 0)
    assert new == int(100_000 * BUDGET_ADJUST_RATIO_LOW)
