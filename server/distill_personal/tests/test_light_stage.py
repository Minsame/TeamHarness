"""SubTask 7.3 — Light 阶段测试。

验证点：
- 信号筛选：过滤纯闲聊/问候/无信息轮次
- L0→L1 原子事实抽取：含决策/约束/踩坑/经验/工具使用的轮次被抽取
- 标注候选类型：rule / memory / skill / tool
- type_counts 统计正确
- yield_ratio 计算
- 信号持久化到 .dreams/light/signals.json
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from server.distill_personal.light_stage import (
    LightStage,
    Signal,
    load_signals,
    save_signals,
)
from server.distill_personal.session_provider import Session, SessionTurn


def _make_session(session_id: str, turns: list[tuple[str, str]]) -> Session:
    """便捷构造 Session。"""
    return Session(
        session_id=session_id,
        turns=[SessionTurn(role=r, content=c) for r, c in turns],
    )


def test_light_filters_noise():
    """纯闲聊/问候被过滤。"""
    session = _make_session(
        "s1",
        [
            ("user", "你好"),
            ("assistant", "hi"),
            ("user", "ok"),
        ],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.signal_count == 0
    assert result.skipped_turns == 3


def test_light_extracts_rule_signal():
    """含规则关键词的轮次被抽取为 rule 信号。"""
    session = _make_session(
        "s1",
        [
            ("user", "提交前必须跑 lint，禁止跳过"),
            ("assistant", "明白"),
        ],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.signal_count == 1
    assert result.signals[0].candidate_type == "rule"
    assert result.type_counts["rule"] == 1


def test_light_extracts_memory_signal():
    session = _make_session(
        "s1",
        [("user", "我们决定用 SQLAlchemy 作为 ORM")],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.signal_count == 1
    assert result.signals[0].candidate_type == "memory"


def test_light_extracts_skill_signal():
    session = _make_session(
        "s1",
        [("user", "第一步先创建数据库，第二步运行迁移脚本")],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.signal_count == 1
    assert result.signals[0].candidate_type == "skill"


def test_light_extracts_tool_signal():
    session = _make_session(
        "s1",
        [("user", "用 ruff 检查代码风格，用 pytest 跑测试")],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.signal_count == 1
    assert result.signals[0].candidate_type == "tool"


def test_light_skips_system_tool_roles():
    """system/tool 角色轮次被跳过。"""
    session = _make_session(
        "s1",
        [
            ("system", "你必须遵守规则"),
            ("tool", "tool result"),
        ],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.scanned_turns == 0  # system/tool 不计入 scanned
    assert result.signal_count == 0


def test_light_yield_ratio():
    """yield_ratio = signals / scanned_turns。"""
    session = _make_session(
        "s1",
        [
            ("user", "你好"),
            ("user", "提交前必须跑 lint"),
            ("user", "ok"),
        ],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.scanned_turns == 3
    assert result.signal_count == 1
    assert result.yield_ratio == pytest.approx(1 / 3, abs=0.01)


def test_light_min_confidence_filter():
    """min_confidence 过滤低置信度信号。"""
    session = _make_session(
        "s1",
        [("user", "应该")],  # 仅命中 1 个关键词，confidence=0.45
    )
    stage = LightStage(min_confidence=0.5)
    result = stage.run([session])
    assert result.signal_count == 0


def test_light_max_excerpt_chars():
    """content_excerpt 被截断到 max_excerpt_chars。"""
    long_content = "必须" + "x" * 2000
    session = _make_session("s1", [("user", long_content)])
    stage = LightStage(max_excerpt_chars=100)
    result = stage.run([session])
    assert len(result.signals[0].content_excerpt) <= 100


def test_light_type_counts():
    """type_counts 正确统计各类型。"""
    session = _make_session(
        "s1",
        [
            ("user", "提交前必须跑 lint 检查"),
            ("user", "我们决定用 SQLAlchemy 作为 ORM"),
            ("user", "第一步先创建数据库表结构"),
            ("user", "用 ruff 检查代码风格问题"),
        ],
    )
    stage = LightStage()
    result = stage.run([session])
    assert result.type_counts["rule"] == 1
    assert result.type_counts["memory"] == 1
    assert result.type_counts["skill"] == 1
    assert result.type_counts["tool"] == 1


def test_light_signal_persistence(tmp_path: Path):
    """信号持久化到 .dreams/light/signals.json 并可加载。"""
    signals = [
        Signal(
            signal_id="sig-1",
            session_id="s1",
            turn_index=0,
            candidate_type="rule",
            content_excerpt="必须 lint",
            reason="关键词命中",
            confidence=0.6,
        )
    ]
    path = save_signals(signals, repo_root=tmp_path)
    assert path.is_file()
    loaded = load_signals(repo_root=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].signal_id == "sig-1"
    assert loaded[0].candidate_type == "rule"


def test_light_load_signals_missing_file(tmp_path: Path):
    """文件不存在返回空列表。"""
    assert load_signals(repo_root=tmp_path) == []


def test_light_with_real_sessions(sample_sessions):
    """用真实 fixture 会话测试 Light 阶段。"""
    stage = LightStage()
    result = stage.run(sample_sessions)
    # session-001 有规则信号，session-002 有 memory 信号，session-003 是纯闲聊
    assert result.signal_count >= 2
    assert result.scanned_sessions == 3
    candidate_types = {s.candidate_type for s in result.signals}
    assert "rule" in candidate_types
    assert "memory" in candidate_types
