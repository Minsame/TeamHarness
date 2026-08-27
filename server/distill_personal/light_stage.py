"""Light 阶段 — 信号筛选 + L0→L1 原子事实抽取。

对应 SubTask 7.3 + 技术方案 3.3.2 ① Light 浅睡：
- 扫描会话，过滤纯闲聊/问候/无信息轮次
- 抽取含决策、约束、踩坑、经验、工具使用的轮次
- 标注候选类型：rule / memory / skill / tool
- 写入本地 .dreams/light/（不入 git，纯临时）
- 借鉴 L0→L1 原子事实抽取：把对话轮次转为结构化的"原子事实"

设计要点：
- Light 阶段不产出资产，只产出"信号"（signals）
- 信号 = 标注了候选类型 + 候选理由的对话片段
- 先用规则过滤（关键词/轮次长度）减少进入 LLM 的量（成本控制）
- 规则过滤后可选地调 LLM 做精筛（提高质量）
- 隐私：信号只在本机处理，不上传原始对话（见 privacy.py）

信号结构（Signal）：
    {
      "signal_id": str,
      "session_id": str,
      "turn_index": int,
      "candidate_type": "rule" | "memory" | "skill" | "tool",
      "content_excerpt": str,  # 对话片段（仅本机使用）
      "reason": str,           # 标注理由
      "confidence": float,     # 0.0-1.0
      "metadata": {...}        # 额外信息（如关键词命中）
    }
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.distill_personal.session_provider import Session, SessionTurn

logger = logging.getLogger(__name__)

# Light 暂存目录默认路径（相对 repo_root）
DEFAULT_LIGHT_DIR = Path(".teamharness-local") / "dreams" / "light"


# ---------------------------------------------------------------------------
# 信号数据结构
# ---------------------------------------------------------------------------


@dataclass
class Signal:
    """Light 阶段产出的信号。"""

    signal_id: str
    session_id: str
    turn_index: int
    candidate_type: str  # rule / memory / skill / tool
    content_excerpt: str
    reason: str = ""
    confidence: float = 0.5
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "signal_id": self.signal_id,
            "session_id": self.session_id,
            "turn_index": self.turn_index,
            "candidate_type": self.candidate_type,
            "content_excerpt": self.content_excerpt,
            "reason": self.reason,
            "confidence": self.confidence,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Signal":
        return cls(
            signal_id=str(data.get("signal_id", "")),
            session_id=str(data.get("session_id", "")),
            turn_index=int(data.get("turn_index", 0)),
            candidate_type=str(data.get("candidate_type", "memory")),
            content_excerpt=str(data.get("content_excerpt", "")),
            reason=str(data.get("reason", "")),
            confidence=float(data.get("confidence", 0.5)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class LightStageResult:
    """Light 阶段结果。"""

    signals: list[Signal] = field(default_factory=list)
    scanned_sessions: int = 0
    scanned_turns: int = 0
    skipped_turns: int = 0  # 被规则过滤掉的轮次
    # 各类型信号计数（用于 SubTask 7.9 上报）
    type_counts: dict[str, int] = field(default_factory=dict)

    @property
    def signal_count(self) -> int:
        return len(self.signals)

    @property
    def yield_ratio(self) -> float:
        """信号产出率（signals / scanned_turns）。"""
        if self.scanned_turns == 0:
            return 0.0
        return self.signal_count / self.scanned_turns


# ---------------------------------------------------------------------------
# 规则过滤（关键词 + 轮次长度）
# ---------------------------------------------------------------------------


# 各候选类型关键词（命中则标注为候选）
_CANDIDATE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rule": (
        "必须", "禁止", "应当", "应该", "规则", "规范", "约定", "lint",
        "不要", "不能", "务必", "禁止", "must", "should", "rule",
    ),
    "memory": (
        "决定", "决策", "选择", "原因是", "踩坑", "教训", "经验",
        "项目用", "模块是", "协议是", "memory", "fact", "decided",
    ),
    "skill": (
        "步骤", "流程", "操作", "执行", "运行", "skill", "workflow",
        "第一步", "第二步", "先", "然后", "最后",
    ),
    "tool": (
        "工具", "脚本", "命令", "用 ruff", "用 pytest", "用 eslint",
        "tool", "script", "command", "CLI",
    ),
}

# 纯闲聊过滤关键词（命中则跳过）
_NOISE_KEYWORDS: tuple[str, ...] = (
    "你好", "hello", "hi", "谢谢", "thanks", "好的", "ok", "嗯",
    "哈哈", "lol",
)

# 最小轮次长度（短于此长度直接跳过）
MIN_TURN_CONTENT_LENGTH = 10


def _classify_candidate_type(content: str) -> tuple[str, int]:
    """根据关键词命中标注候选类型，返回 (type, score)。

    score = 命中关键词数，0 表示无命中。
    多类型命中时取 score 最高的。
    """
    text_lower = content.lower()
    best_type = ""
    best_score = 0
    for cand_type, keywords in _CANDIDATE_KEYWORDS.items():
        score = 0
        for kw in keywords:
            if kw.lower() in text_lower:
                score += 1
        if score > best_score:
            best_score = score
            best_type = cand_type
    return best_type, best_score


def _is_noise(content: str) -> bool:
    """判断是否为纯闲聊（应跳过）。"""
    text_lower = content.lower().strip()
    if len(text_lower) < MIN_TURN_CONTENT_LENGTH:
        return True
    # 全部由噪声关键词组成
    for kw in _NOISE_KEYWORDS:
        if text_lower == kw.lower():
            return True
    return False


# ---------------------------------------------------------------------------
# Light 阶段主流程
# ---------------------------------------------------------------------------


class LightStage:
    """Light 阶段：信号筛选 + L0→L1 原子事实抽取。

    使用：
        stage = LightStage()
        result = stage.run(sessions)
        # result.signals → REM 阶段输入
    """

    def __init__(
        self,
        *,
        min_confidence: float = 0.3,
        max_excerpt_chars: int = 1000,
    ) -> None:
        self.min_confidence = min_confidence
        self.max_excerpt_chars = max_excerpt_chars

    def run(self, sessions: list[Session]) -> LightStageResult:
        """对会话列表执行 Light 阶段。

        返回 LightStageResult，含 signals 列表。
        """
        signals: list[Signal] = []
        type_counts: dict[str, int] = {"rule": 0, "memory": 0, "skill": 0, "tool": 0}
        scanned_sessions = 0
        scanned_turns = 0
        skipped_turns = 0

        for session in sessions:
            scanned_sessions += 1
            for idx, turn in enumerate(session.turns):
                # 只处理 user 与 assistant 轮次（跳过 system / tool）
                if turn.role not in ("user", "assistant"):
                    continue
                scanned_turns += 1
                content = turn.content or ""
                # Task 28：协作信号标记（needs_human_review / tag_routing）
                # 来自 ConversationLogSessionProvider 转换的 SessionTurn.metadata
                meta = turn.metadata or {}
                event_type = str(meta.get("event_type", ""))
                has_needs_review = (
                    event_type == "needs_human_review"
                    or meta.get("needs_human_review") is True
                )
                has_tag_routing = bool(meta.get("tag_routing"))
                is_collab_signal = has_needs_review or has_tag_routing
                # 规则过滤：纯闲聊 / 过短（协作信号跳过噪声过滤，保留重要协作事件）
                if not is_collab_signal and _is_noise(content):
                    skipped_turns += 1
                    continue
                # 关键词分类
                cand_type, score = _classify_candidate_type(content)
                if not cand_type or score == 0:
                    if not is_collab_signal:
                        skipped_turns += 1
                        continue
                    # 协作信号无关键词命中时默认标注：
                    # needs_human_review → rule（可复用经验）；tag_routing → memory
                    cand_type = "rule" if has_needs_review else "memory"
                # confidence 简单映射：score=1→0.45, score=2→0.6, score>=3→0.75
                # 协作信号无关键词命中时基础 0.4（仍需加权后过 min_confidence）
                if score > 0:
                    confidence = min(0.9, 0.3 + score * 0.15)
                else:
                    confidence = 0.4
                # Task 28：协作信号加权
                # needs_human_review +0.2（重要经验，优先提炼）
                # tag_routing +0.1（跨职能协作信号）
                if has_needs_review:
                    confidence = min(1.0, confidence + 0.2)
                if has_tag_routing:
                    confidence = min(1.0, confidence + 0.1)
                # needs_human_review 优先标注为 rule（可复用经验）
                if has_needs_review:
                    cand_type = "rule"
                if confidence < self.min_confidence:
                    skipped_turns += 1
                    continue
                excerpt = content[: self.max_excerpt_chars]
                # 信号 metadata
                signal_meta: dict[str, Any] = {
                    "keyword_score": score,
                    "role": turn.role,
                    "timestamp": turn.timestamp,
                }
                if has_needs_review:
                    signal_meta["needs_human_review"] = True
                if has_tag_routing:
                    signal_meta["tag_routing"] = meta.get("tag_routing")
                reason_parts = [f"关键词命中 {score} 个（{cand_type}）"]
                if is_collab_signal:
                    collab_tags = []
                    if has_needs_review:
                        collab_tags.append("needs_human_review")
                    if has_tag_routing:
                        collab_tags.append("tag_routing")
                    reason_parts.append("协作信号加权（" + "+".join(collab_tags) + "）")
                signal = Signal(
                    signal_id=str(uuid.uuid4()),
                    session_id=session.session_id,
                    turn_index=idx,
                    candidate_type=cand_type,
                    content_excerpt=excerpt,
                    reason="；".join(reason_parts),
                    confidence=confidence,
                    metadata=signal_meta,
                )
                signals.append(signal)
                type_counts[cand_type] = type_counts.get(cand_type, 0) + 1

        return LightStageResult(
            signals=signals,
            scanned_sessions=scanned_sessions,
            scanned_turns=scanned_turns,
            skipped_turns=skipped_turns,
            type_counts=type_counts,
        )


# ---------------------------------------------------------------------------
# 信号持久化（.dreams/light/，可选）
# ---------------------------------------------------------------------------


def save_signals(
    signals: list[Signal],
    *,
    repo_root: Path | None = None,
    light_dir: Path | None = None,
) -> Path:
    """把信号写入 .dreams/light/signals.json（不入 git，纯临时）。

    返回写入的文件路径。
    """
    if light_dir is not None:
        target_dir = light_dir
    elif repo_root is not None:
        target_dir = repo_root / DEFAULT_LIGHT_DIR
    else:
        target_dir = Path.cwd() / DEFAULT_LIGHT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "signals.json"
    payload = {
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(signals),
        "signals": [s.to_dict() for s in signals],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_signals(
    *,
    repo_root: Path | None = None,
    light_dir: Path | None = None,
) -> list[Signal]:
    """从 .dreams/light/signals.json 加载信号。"""
    if light_dir is not None:
        path = light_dir / "signals.json"
    elif repo_root is not None:
        path = repo_root / DEFAULT_LIGHT_DIR / "signals.json"
    else:
        path = Path.cwd() / DEFAULT_LIGHT_DIR / "signals.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("加载 signals 失败: %s", exc)
        return []
    raw_signals = data.get("signals") or []
    return [Signal.from_dict(s) for s in raw_signals if isinstance(s, dict)]


__all__ = [
    "DEFAULT_LIGHT_DIR",
    "LightStage",
    "LightStageResult",
    "Signal",
    "load_signals",
    "save_signals",
]
