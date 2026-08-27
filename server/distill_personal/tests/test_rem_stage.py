"""SubTask 7.4 — REM 阶段测试。

验证点：
- 意图归纳：从 signals 抽取 intent
- 区分一次性上下文 vs 可复用经验
- 一次性上下文（含"这次"/"刚才"等）被丢弃
- 跨会话重复模式识别
- intent 持久化
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_personal.light_stage import Signal
from server.distill_personal.rem_stage import (
    Intent,
    RemStage,
    load_intents,
    save_intents,
)


def _make_signal(
    *,
    signal_id: str,
    session_id: str,
    candidate_type: str,
    content: str,
    confidence: float = 0.6,
) -> Signal:
    return Signal(
        signal_id=signal_id,
        session_id=session_id,
        turn_index=0,
        candidate_type=candidate_type,
        content_excerpt=content,
        confidence=confidence,
    )


def test_rem_produces_intent():
    """signal 经 REM 阶段归纳为 intent。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="rule",
            content="提交前必须跑 lint，禁止跳过",
        )
    ]
    stage = RemStage()
    result = stage.run(signals)
    assert result.intent_count == 1
    assert result.intents[0].candidate_type == "rule"
    assert result.intents[0].reusable is True


def test_rem_discards_one_time_context():
    """一次性上下文（含"这次"）被丢弃。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="memory",
            content="这次 bug 是 typo 导致的",
        )
    ]
    stage = RemStage(discard_one_time=True)
    result = stage.run(signals)
    assert result.intent_count == 0
    assert result.discarded_count == 1


def test_rem_keeps_one_time_when_discard_disabled():
    """discard_one_time=False 时保留一次性上下文。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="memory",
            content="这次 bug 是 typo 导致的",
        )
    ]
    stage = RemStage(discard_one_time=False)
    result = stage.run(signals)
    assert result.intent_count == 1
    assert result.intents[0].reusable is False


def test_rem_cross_session_pattern():
    """跨会话重复模式识别。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="rule",
            content="必须跑 lint",
        ),
        _make_signal(
            signal_id="s2",
            session_id="sess-2",  # 不同会话
            candidate_type="rule",
            content="必须跑 lint 检查",
        ),
    ]
    stage = RemStage()
    result = stage.run(signals)
    # 应聚类为同一簇
    assert result.intent_count == 1
    intent = result.intents[0]
    assert intent.pattern_count == 2
    assert intent.metadata["cross_session_count"] == 2
    assert result.pattern_detected_count == 1


def test_rem_same_session_no_cross_pattern():
    """同会话内多个相似 signal 不算跨会话。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="rule",
            content="必须跑 lint",
        ),
        _make_signal(
            signal_id="s2",
            session_id="sess-1",  # 同会话
            candidate_type="rule",
            content="必须跑 lint",
        ),
    ]
    stage = RemStage()
    result = stage.run(signals)
    assert result.intent_count == 1
    assert result.intents[0].metadata["cross_session_count"] == 1


def test_rem_empty_signals():
    stage = RemStage()
    result = stage.run([])
    assert result.intent_count == 0


def test_rem_intent_persistence(tmp_path: Path):
    intents = [
        Intent(
            intent_id="i1",
            description="必须跑 lint",
            candidate_type="rule",
            reusable=True,
            pattern_count=2,
        )
    ]
    path = save_intents(intents, repo_root=tmp_path)
    assert path.is_file()
    loaded = load_intents(repo_root=tmp_path)
    assert len(loaded) == 1
    assert loaded[0].intent_id == "i1"


def test_rem_load_intents_missing(tmp_path: Path):
    assert load_intents(repo_root=tmp_path) == []


def test_rem_different_types_separate_clusters():
    """不同 candidate_type 不聚类为同簇。"""
    signals = [
        _make_signal(
            signal_id="s1",
            session_id="sess-1",
            candidate_type="rule",
            content="必须跑 lint",
        ),
        _make_signal(
            signal_id="s2",
            session_id="sess-1",
            candidate_type="memory",
            content="必须跑 lint 的决定",
        ),
    ]
    stage = RemStage()
    result = stage.run(signals)
    assert result.intent_count == 2  # 不同类型分两簇
