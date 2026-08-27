"""Aider 会话 Adapter（Markdown）。

对应 Task 3：读取 ``.aider.chat.history.md``，实现 SessionProvider Protocol。

会话路径：``.aider.chat.history.md``（单文件，Markdown 格式）
- 按二级 / 一级标题分割为多轮"会话"（每个标题段视为一个 session）
- 角色识别：行首以 ``>`` 开头视为 user 输入，其余视为 assistant 回复（简化处理）
- 单文件可能含多个会话段，session_id 采用 ``<filename>#<seq>`` 形式
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

from server.coding_adapters.fingerprints import resolve_path
from server.distill_personal.session_provider import (
    Session,
    SessionMeta,
    SessionProvider,
    SessionTurn,
)

logger = logging.getLogger(__name__)

# 匹配 Markdown 标题行（## 或 # 开头），用于切分会话段
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")


class AiderAdapter:
    """Aider 会话 Provider。

    实现 :class:`SessionProvider` Protocol。解析 ``.aider.chat.history.md``
    Markdown 对话记录，按标题切分为多个会话。

    session_id 形如 ``aider.chat.history#0``、``aider.chat.history#1``，
    其中数字段为该文件中按标题切分的段序号（从 0 开始）。
    """

    PROVIDER_NAME = "aider"

    def __init__(self, history_path: Path | None = None) -> None:
        """初始化。

        Args:
            history_path: ``.aider.chat.history.md`` 路径。None 时默认
                当前工作目录下 ``./.aider.chat.history.md``（与指纹表一致）。
        """
        if history_path is None:
            history_path = resolve_path(".aider.chat.history.md")
        self._path = history_path

    # ------------------------------------------------------------------
    # SessionProvider Protocol
    # ------------------------------------------------------------------

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        if not self._path.is_file():
            return []
        try:
            stat = self._path.stat()
        except OSError:
            return []
        if since is not None and stat.st_mtime < since:
            return []
        segments = self._split_segments()
        metas: list[SessionMeta] = []
        for idx in range(len(segments)):
            metas.append(
                SessionMeta(
                    session_id=self._segment_id(idx),
                    source_path=str(self._path),
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    completed=True,
                )
            )
        return metas

    def read_session(self, session_id: str) -> Session:
        if not self._path.is_file():
            raise FileNotFoundError(f"Aider 历史文件不存在: {self._path}")
        idx = self._parse_segment_id(session_id)
        if idx is None:
            raise FileNotFoundError(f"会话不存在: {session_id}")
        segments = self._split_segments()
        if idx < 0 or idx >= len(segments):
            raise FileNotFoundError(f"会话不存在: {session_id}")
        turns = self._segment_to_turns(segments[idx])
        return Session(
            session_id=session_id,
            turns=turns,
            source_path=str(self._path),
            completed=True,
        )

    def is_completed(self, session_id: str) -> bool:
        if not self._path.is_file():
            return False
        idx = self._parse_segment_id(session_id)
        if idx is None:
            return False
        segments = self._split_segments()
        return 0 <= idx < len(segments)

    # ------------------------------------------------------------------
    # 内部：Markdown 解析
    # ------------------------------------------------------------------

    def _segment_id(self, idx: int) -> str:
        return f"{self._path.name}#{idx}"

    def _parse_segment_id(self, session_id: str) -> int | None:
        """从 ``<filename>#<idx>`` 提取 idx。"""
        if "#" not in session_id:
            return None
        _, _, suffix = session_id.rpartition("#")
        if not suffix.isdigit():
            return None
        return int(suffix)

    def _split_segments(self) -> list[list[str]]:
        """按标题行将文件切分为多段（每段为行列表，不含标题行）。

        若文件无标题，整体作为单段。
        """
        try:
            text = self._path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("读取 Aider 历史文件失败（%s），返回空段", exc)
            return []
        segments: list[list[str]] = []
        current: list[str] = []
        for line in text.splitlines():
            if _HEADING_RE.match(line):
                if current or segments:
                    segments.append(current)
                current = []
            else:
                current.append(line)
        segments.append(current)
        # 过滤全空段
        return [seg for seg in segments if any(s.strip() for s in seg)]

    @staticmethod
    def _segment_to_turns(lines: list[str]) -> list[SessionTurn]:
        """将一段 Markdown 行转为 SessionTurn 列表。

        角色识别简化规则：
        - 行以 ``>`` 开头（含引用符号 ``> ``）视为 user
        - 其余非空行视为 assistant
        - 连续同角色行合并为一条消息
        """
        turns: list[SessionTurn] = []
        cur_role: str | None = None
        buf: list[str] = []
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            role = "user" if stripped.startswith(">") else "assistant"
            content = stripped.lstrip("> ").rstrip()
            if cur_role is None:
                cur_role = role
                buf = [content]
            elif role == cur_role:
                buf.append(content)
            else:
                turns.append(
                    SessionTurn(role=cur_role, content="\n".join(buf))
                )
                cur_role = role
                buf = [content]
        if cur_role is not None and buf:
            turns.append(SessionTurn(role=cur_role, content="\n".join(buf)))
        return turns


__all__ = ["AiderAdapter"]
