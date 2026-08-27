"""privacy 测试（SubTask 7.12 + 重点风险 🔴 隐私保护）。"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_personal.light_stage import Signal
from server.distill_personal.privacy import (
    DREAMS_GITIGNORE_RULES,
    INTENT_SENSITIVE_FIELDS,
    PrivacyAuditResult,
    PrivacyGuard,
    SESSION_SENSITIVE_FIELDS,
    SIGNAL_SENSITIVE_FIELDS,
    upload_safe_intent,
    upload_safe_session,
    upload_safe_signal,
)
from server.distill_personal.rem_stage import Intent
from server.distill_personal.session_provider import Session, SessionTurn


# ---------------------------------------------------------------------------
# 敏感字段常量
# ---------------------------------------------------------------------------


def test_sensitive_fields_constants() -> None:
    """敏感字段常量覆盖核心隐私字段。"""
    assert "source_path" in SESSION_SENSITIVE_FIELDS
    assert "turns" in SESSION_SENSITIVE_FIELDS
    assert "content_excerpt" in SIGNAL_SENSITIVE_FIELDS
    assert "description" in INTENT_SENSITIVE_FIELDS
    assert "source_signal_ids" in INTENT_SENSITIVE_FIELDS


def test_dreams_gitignore_rules_cover_dreams_and_distill() -> None:
    """gitignore 规则覆盖 .dreams/ 与 distill/ 目录。"""
    assert ".teamharness-local/dreams/" in DREAMS_GITIGNORE_RULES
    assert ".teamharness-local/distill/" in DREAMS_GITIGNORE_RULES


# ---------------------------------------------------------------------------
# PrivacyGuard.redact_*
# ---------------------------------------------------------------------------


def test_redact_session_removes_sensitive_fields() -> None:
    """redact_session 移除 source_path / turns。"""
    guard = PrivacyGuard()
    session_dict = {
        "session_id": "s1",
        "source_path": "/secret/path.jsonl",
        "turns": [{"role": "user", "content": "secret"}],
        "started_at": "2026-08-07T10:00:00Z",
    }
    safe = guard.redact_session(session_dict)
    assert "source_path" not in safe
    assert "turns" not in safe
    assert safe["session_id"] == "s1"
    assert safe["started_at"] == "2026-08-07T10:00:00Z"
    # 原 dict 不变
    assert "source_path" in session_dict
    assert "turns" in session_dict


def test_redact_signal_removes_content_excerpt() -> None:
    """redact_signal 移除 content_excerpt。"""
    guard = PrivacyGuard()
    signal_dict = {
        "signal_id": "sig1",
        "candidate_type": "rule",
        "content_excerpt": "用户说必须跑 lint",
        "confidence": 0.8,
    }
    safe = guard.redact_signal(signal_dict)
    assert "content_excerpt" not in safe
    assert safe["signal_id"] == "sig1"
    assert safe["confidence"] == 0.8


def test_redact_intent_removes_description_and_source_signal_ids() -> None:
    """redact_intent 移除 description / source_signal_ids。"""
    guard = PrivacyGuard()
    intent_dict = {
        "intent_id": "i1",
        "description": "用户反复强调 lint",
        "source_signal_ids": ["sig1", "sig2"],
        "candidate_type": "rule",
        "reusable": True,
    }
    safe = guard.redact_intent(intent_dict)
    assert "description" not in safe
    assert "source_signal_ids" not in safe
    assert safe["intent_id"] == "i1"
    assert safe["candidate_type"] == "rule"


# ---------------------------------------------------------------------------
# PrivacyGuard.audit_upload
# ---------------------------------------------------------------------------


def test_audit_upload_clean_payload_passes() -> None:
    """无敏感字段的载荷审计通过。"""
    guard = PrivacyGuard()
    result = guard.audit_upload(
        session={"session_id": "s1"},
        signals=[{"signal_id": "sig1"}],
        intents=[{"intent_id": "i1"}],
    )
    assert result.ok
    assert result.violations == []


def test_audit_upload_detects_session_violation() -> None:
    """审计 session 中的敏感字段。"""
    guard = PrivacyGuard()
    result = guard.audit_upload(
        session={"session_id": "s1", "turns": [{"role": "user"}]},
    )
    assert not result.ok
    assert "session.turns" in result.violations
    assert "session.turns" in result.redacted_fields


def test_audit_upload_detects_signal_violation() -> None:
    """审计 signals 中的敏感字段。"""
    guard = PrivacyGuard()
    result = guard.audit_upload(
        signals=[
            {"signal_id": "ok"},
            {"signal_id": "bad", "content_excerpt": "secret"},
        ],
    )
    assert not result.ok
    assert "signals[1].content_excerpt" in result.violations


def test_audit_upload_detects_intent_violation() -> None:
    """审计 intents 中的敏感字段。"""
    guard = PrivacyGuard()
    result = guard.audit_upload(
        intents=[
            {"intent_id": "i1", "description": "secret", "source_signal_ids": ["s1"]},
        ],
    )
    assert not result.ok
    assert "intents[0].description" in result.violations
    assert "intents[0].source_signal_ids" in result.violations


def test_audit_upload_all_none_passes() -> None:
    """全部 None 视为无载荷，审计通过。"""
    guard = PrivacyGuard()
    result = guard.audit_upload()
    assert result.ok


def test_privacy_audit_result_to_dict() -> None:
    """PrivacyAuditResult.to_dict。"""
    r = PrivacyAuditResult(
        ok=False,
        violations=["session.turns"],
        redacted_fields=["session.turns"],
    )
    d = r.to_dict()
    assert d["ok"] is False
    assert d["violations"] == ["session.turns"]
    assert d["redacted_fields"] == ["session.turns"]


# ---------------------------------------------------------------------------
# ensure_dreams_gitignored
# ---------------------------------------------------------------------------


def test_ensure_dreams_gitignored_creates_gitignore(tmp_path: Path) -> None:
    """无 .gitignore 时创建并追加规则。"""
    guard = PrivacyGuard()
    assert guard.ensure_dreams_gitignored(tmp_path) is True
    gitignore = tmp_path / ".gitignore"
    assert gitignore.is_file()
    content = gitignore.read_text(encoding="utf-8")
    for rule in DREAMS_GITIGNORE_RULES:
        assert rule in content


def test_ensure_dreams_gitignored_appends_missing_rules(tmp_path: Path) -> None:
    """.gitignore 已有部分规则时只追加缺失项。"""
    gitignore = tmp_path / ".gitignore"
    gitignore.write_text(".teamharness-local/dreams/\n", encoding="utf-8")
    guard = PrivacyGuard()
    assert guard.ensure_dreams_gitignored(tmp_path) is True
    content = gitignore.read_text(encoding="utf-8")
    assert ".teamharness-local/dreams/" in content
    assert ".teamharness-local/distill/" in content


def test_ensure_dreams_gitignored_idempotent(tmp_path: Path) -> None:
    """已有完整规则时不重复追加。"""
    guard = PrivacyGuard()
    guard.ensure_dreams_gitignored(tmp_path)
    content_before = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    guard.ensure_dreams_gitignored(tmp_path)
    content_after = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert content_before == content_after


# ---------------------------------------------------------------------------
# upload_safe_* 便捷函数
# ---------------------------------------------------------------------------


def test_upload_safe_session_only_returns_metadata() -> None:
    """upload_safe_session 只返回元数据字段，不含 turns / source_path。"""
    session = Session(
        session_id="s1",
        turns=[SessionTurn(role="user", content="secret")],
        started_at="2026-08-07T10:00:00Z",
        ended_at="2026-08-07T11:00:00Z",
        source_path="/secret/path.jsonl",
        completed=True,
    )
    safe = upload_safe_session(session)
    assert safe["session_id"] == "s1"
    assert safe["started_at"] == "2026-08-07T10:00:00Z"
    assert "turns" not in safe
    assert "source_path" not in safe
    assert safe["turn_count"] == 1


def test_upload_safe_signal_omits_content_excerpt() -> None:
    """upload_safe_signal 不含 content_excerpt。"""
    signal = Signal(
        signal_id="sig1",
        session_id="s1",
        turn_index=2,
        candidate_type="rule",
        content_excerpt="secret content",
        reason="rule match",
        confidence=0.8,
    )
    safe = upload_safe_signal(signal)
    assert safe["signal_id"] == "sig1"
    assert safe["candidate_type"] == "rule"
    assert "content_excerpt" not in safe


def test_upload_safe_intent_omits_description_and_signal_ids() -> None:
    """upload_safe_intent 不含 description / source_signal_ids。"""
    intent = Intent(
        intent_id="i1",
        description="secret description",
        candidate_type="rule",
        reusable=True,
        reuse_hint="hint",
        source_signal_ids=["sig1", "sig2"],
        pattern_count=2,
    )
    safe = upload_safe_intent(intent)
    assert safe["intent_id"] == "i1"
    assert safe["pattern_count"] == 2
    assert "description" not in safe
    assert "source_signal_ids" not in safe


def test_upload_safe_session_does_not_leak_via_audit() -> None:
    """upload_safe_session 结果应通过 audit_upload 审计。"""
    session = Session(
        session_id="s1",
        turns=[SessionTurn(role="user", content="x")],
        source_path="/secret",
    )
    safe = upload_safe_session(session)
    guard = PrivacyGuard()
    result = guard.audit_upload(session=safe)
    assert result.ok
