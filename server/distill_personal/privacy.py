"""隐私保护 — 对话不离开本机，只上传结构化资产。

对应 SubTask 7.12 + 重点风险 🔴：
- 一级提炼的对话记录只在本机读取与处理
- 上传到服务端的只有：
  1. 提炼后的结构化资产（写入 AssetIndex）
  2. Light 阶段候选信号计数（用于预算动态调整）
  3. 资产 frontmatter 元数据（score / confidence / tags）
- 不上传：
  1. 原始对话内容（SessionTurn.content）
  2. Session 文件路径（source_path）
  3. 完整 signal 的 content_excerpt（仅本机 .dreams/light/）
  4. intent 的 description 原文（仅本机 .dreams/rem/）

设计要点：
- PrivacyGuard 在上传前过滤敏感字段
- upload_safe_dict() 把 Session/Signal/Intent 转为可安全上传的版本
- 违反隐私保护须立即在汇报中标注（协调卡片重点风险 🔴）
- .dreams/ 目录加入 .gitignore（私有，不入 git）
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.distill_personal.light_stage import Signal
from server.distill_personal.rem_stage import Intent
from server.distill_personal.session_provider import Session

logger = logging.getLogger(__name__)


# 不上传到服务端的字段（敏感字段）
SESSION_SENSITIVE_FIELDS: tuple[str, ...] = ("source_path", "turns")
SIGNAL_SENSITIVE_FIELDS: tuple[str, ...] = ("content_excerpt",)
INTENT_SENSITIVE_FIELDS: tuple[str, ...] = ("description", "source_signal_ids")

# .dreams/ 目录需加入 .gitignore（不入 git）
DREAMS_GITIGNORE_RULES: tuple[str, ...] = (
    ".teamharness-local/dreams/",
    ".teamharness-local/distill/",
)


@dataclass
class PrivacyAuditResult:
    """隐私审计结果。"""

    ok: bool
    violations: list[str]
    redacted_fields: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "violations": list(self.violations),
            "redacted_fields": list(self.redacted_fields),
        }


class PrivacyGuard:
    """隐私保护守卫。

    使用：
        guard = PrivacyGuard()
        safe_session = guard.redact_session(session.to_dict())
        # safe_session 不含 source_path / turns
    """

    def redact_session(self, session_dict: dict[str, Any]) -> dict[str, Any]:
        """脱敏 Session dict（移除 source_path / turns）。"""
        return _omit_keys(session_dict, SESSION_SENSITIVE_FIELDS)

    def redact_signal(self, signal_dict: dict[str, Any]) -> dict[str, Any]:
        """脱敏 Signal dict（移除 content_excerpt）。"""
        return _omit_keys(signal_dict, SIGNAL_SENSITIVE_FIELDS)

    def redact_intent(self, intent_dict: dict[str, Any]) -> dict[str, Any]:
        """脱敏 Intent dict（移除 description / source_signal_ids）。"""
        return _omit_keys(intent_dict, INTENT_SENSITIVE_FIELDS)

    def audit_upload(
        self,
        *,
        session: dict[str, Any] | None = None,
        signals: list[dict[str, Any]] | None = None,
        intents: list[dict[str, Any]] | None = None,
    ) -> PrivacyAuditResult:
        """审计上传载荷是否含敏感字段。

        返回 PrivacyAuditResult，violations 列出违规字段路径。
        ok=False 时调用方应阻断上传并立即标注（对齐协调卡片重点风险 🔴）。
        """
        violations: list[str] = []
        redacted: list[str] = []
        if session is not None:
            for key in SESSION_SENSITIVE_FIELDS:
                if key in session:
                    violations.append(f"session.{key}")
                    redacted.append(f"session.{key}")
        if signals is not None:
            for i, sig in enumerate(signals):
                for key in SIGNAL_SENSITIVE_FIELDS:
                    if key in sig:
                        violations.append(f"signals[{i}].{key}")
                        redacted.append(f"signals[{i}].{key}")
        if intents is not None:
            for i, intent in enumerate(intents):
                for key in INTENT_SENSITIVE_FIELDS:
                    if key in intent:
                        violations.append(f"intents[{i}].{key}")
                        redacted.append(f"intents[{i}].{key}")
        return PrivacyAuditResult(
            ok=(len(violations) == 0),
            violations=violations,
            redacted_fields=redacted,
        )

    def ensure_dreams_gitignored(self, repo_root: Path) -> bool:
        """确保 .dreams/ 与 distill/ 目录加入 .gitignore。

        返回 True 表示规则已齐全（已存在或刚追加），False 表示追加失败。
        """
        gitignore_path = repo_root / ".gitignore"
        existing_lines: list[str] = []
        if gitignore_path.is_file():
            existing_lines = gitignore_path.read_text(encoding="utf-8").splitlines()
        missing = [
            rule for rule in DREAMS_GITIGNORE_RULES
            if rule not in existing_lines
        ]
        if not missing:
            return True
        # 追加缺失规则
        new_content = "\n".join(existing_lines + missing)
        if not existing_lines:
            new_content = "\n".join(missing)
        gitignore_path.write_text(new_content + "\n", encoding="utf-8")
        logger.info("已追加 .gitignore 规则: %s", missing)
        return True


def _omit_keys(d: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    """返回 dict 副本，移除指定 keys。"""
    return {k: v for k, v in d.items() if k not in keys}


def upload_safe_session(session: Session) -> dict[str, Any]:
    """便捷函数：把 Session 转为可安全上传的 dict。"""
    guard = PrivacyGuard()
    safe = guard.redact_session(session.to_dict())
    # 额外移除 turns（已通过 SESSION_SENSITIVE_FIELDS 移除，这里冗余保险）
    safe.pop("turns", None)
    # 只保留元数据级别字段
    return {
        "session_id": safe.get("session_id", ""),
        "started_at": safe.get("started_at", ""),
        "ended_at": safe.get("ended_at", ""),
        "completed": safe.get("completed", True),
        "turn_count": safe.get("turn_count", 0),
    }


def upload_safe_signal(signal: Signal) -> dict[str, Any]:
    """便捷函数：把 Signal 转为可安全上传的 dict。"""
    return {
        "signal_id": signal.signal_id,
        "session_id": signal.session_id,
        "turn_index": signal.turn_index,
        "candidate_type": signal.candidate_type,
        "reason": signal.reason,
        "confidence": signal.confidence,
        # 不含 content_excerpt
    }


def upload_safe_intent(intent: Intent) -> dict[str, Any]:
    """便捷函数：把 Intent 转为可安全上传的 dict。"""
    return {
        "intent_id": intent.intent_id,
        "candidate_type": intent.candidate_type,
        "reusable": intent.reusable,
        "reuse_hint": intent.reuse_hint,
        "pattern_count": intent.pattern_count,
        # 不含 description / source_signal_ids
    }


__all__ = [
    "DREAMS_GITIGNORE_RULES",
    "INTENT_SENSITIVE_FIELDS",
    "PrivacyAuditResult",
    "PrivacyGuard",
    "SESSION_SENSITIVE_FIELDS",
    "SIGNAL_SENSITIVE_FIELDS",
    "upload_safe_intent",
    "upload_safe_session",
    "upload_safe_signal",
]
