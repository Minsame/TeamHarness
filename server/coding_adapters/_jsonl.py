"""JSONL 会话文件解析公共函数。

供 ClaudeCodeAdapter / CodexAdapter / WindsurfAdapter 等 JSONL 系 Adapter 复用，
解析逻辑与 ``distill_personal.session_provider.TraeSessionProvider._parse_jsonl_file``
保持一致（避免跨包修改既有代码，本模块提供独立实现）。

宽松解析策略：
- 空行 / 非法 JSON 跳过（记 warning）
- role 缺失默认 "user"，content 缺失默认 ""
- timestamp 字段优先取 "timestamp" / "ts" / "time" / "@timestamp"
- tool_calls 字段优先取 "tool_calls" / "toolCalls"
- 其余字段透传到 metadata
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from server.distill_personal.session_provider import Session, SessionTurn

logger = logging.getLogger(__name__)


_KNOWN_KEYS = frozenset(
    {
        "role",
        "content",
        "timestamp",
        "ts",
        "time",
        "@timestamp",
        "tool_calls",
        "toolCalls",
    }
)

_TS_CANDIDATES = ("timestamp", "ts", "time", "@timestamp")
_TOOL_CALLS_CANDIDATES = ("tool_calls", "toolCalls")


def parse_jsonl_session(path: Path, session_id: str) -> Session:
    """解析 ``*.jsonl`` 会话文件，每行一条 JSON 消息。

    Args:
        path: 会话文件路径。
        session_id: 用于填充 ``Session.session_id``。

    Returns:
        解析后的 :class:`Session` 对象（``completed=True``）。
    """
    turns: list[SessionTurn] = []
    started_at = ""
    ended_at = ""
    first_ts_found = False
    with path.open("r", encoding="utf-8") as f:
        for line_no, raw in enumerate(f, start=1):
            line = raw.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError as exc:
                logger.warning(
                    "JSONL 会话 %s 第 %d 行非合法 JSON，跳过: %s",
                    path.name,
                    line_no,
                    exc,
                )
                continue
            if not isinstance(obj, dict):
                continue
            role = str(obj.get("role", "user")).lower()
            content = str(obj.get("content", ""))
            ts = ""
            for key in _TS_CANDIDATES:
                if key in obj:
                    ts = str(obj[key])
                    break
            if ts and not first_ts_found:
                started_at = ts
                first_ts_found = True
            if ts:
                ended_at = ts
            tool_calls: list[dict[str, Any]] = []
            for key in _TOOL_CALLS_CANDIDATES:
                val = obj.get(key)
                if isinstance(val, list):
                    tool_calls = [v for v in val if isinstance(v, dict)]
                    break
            metadata = {k: v for k, v in obj.items() if k not in _KNOWN_KEYS}
            turns.append(
                SessionTurn(
                    role=role,
                    content=content,
                    timestamp=ts,
                    tool_calls=tool_calls,
                    metadata=metadata,
                )
            )
    return Session(
        session_id=session_id,
        turns=turns,
        started_at=started_at,
        ended_at=ended_at,
        source_path=str(path),
        completed=True,
    )


__all__ = ["parse_jsonl_session"]
