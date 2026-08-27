"""Windsurf 会话 Adapter。

对应 Task 3：读取 ``~/.codeium/windsurf/sessions/*``，实现 SessionProvider Protocol。

实现策略：
- 优先按 JSONL 解析（每行一条 JSON 消息），复用 :func:`parse_jsonl_session`
- 非 ``.jsonl`` 文件 / 解析失败 → 跳过该文件（不抛异常）
- 整体 sessions 目录缺失或不可读 → 返回空列表
"""

from __future__ import annotations

import logging
from pathlib import Path

from server.coding_adapters._jsonl import parse_jsonl_session
from server.coding_adapters.fingerprints import resolve_path
from server.distill_personal.session_provider import (
    Session,
    SessionMeta,
    SessionProvider,
)

logger = logging.getLogger(__name__)


class WindsurfAdapter:
    """Windsurf 会话 Provider。

    实现 :class:`SessionProvider` Protocol。读取
    ``~/.codeium/windsurf/sessions/*.jsonl`` 会话文件。

    遇到不可解析的文件（非 JSONL / 格式错误）会记录 warning 并跳过，
    不影响其他会话的读取。
    """

    PROVIDER_NAME = "windsurf"

    def __init__(self, sessions_root: Path | None = None) -> None:
        """初始化。

        Args:
            sessions_root: sessions 根目录。None 时默认
                ``~/.codeium/windsurf/sessions``。
        """
        if sessions_root is None:
            sessions_root = resolve_path("~/.codeium/windsurf/sessions")
        self._root = sessions_root

    def _resolve_root(self) -> Path | None:
        return self._root if self._root.is_dir() else None

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        root = self._resolve_root()
        if root is None:
            return []
        metas: list[SessionMeta] = []
        for p in root.iterdir():
            if not p.is_file():
                continue
            if p.suffix.lower() != ".jsonl":
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since:
                continue
            metas.append(
                SessionMeta(
                    session_id=p.stem,
                    source_path=str(p),
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    completed=True,
                )
            )
        metas.sort(key=lambda m: m.mtime)
        return metas

    def read_session(self, session_id: str) -> Session:
        root = self._resolve_root()
        if root is None:
            raise FileNotFoundError(
                f"Windsurf sessions root 未找到，无法读取 {session_id}"
            )
        path = root / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        try:
            return parse_jsonl_session(path, session_id)
        except OSError as exc:
            logger.warning("读取 Windsurf 会话 %s 失败: %s", session_id, exc)
            raise FileNotFoundError(f"会话读取失败: {session_id}") from exc

    def is_completed(self, session_id: str) -> bool:
        root = self._resolve_root()
        if root is None:
            return False
        if session_id == ".." or session_id == "" or "/" in session_id:
            return False
        return (root / f"{session_id}.jsonl").is_file()


__all__ = ["WindsurfAdapter"]
