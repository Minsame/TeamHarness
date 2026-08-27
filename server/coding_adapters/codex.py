"""Codex 会话 Adapter。

对应 Task 3：读取 ``~/.codex/sessions/*``，实现 SessionProvider Protocol。

会话路径：``~/.codex/sessions/*``（``*.jsonl`` 或 ``*.json``）
- 优先按 JSONL 解析（每行一条 JSON 消息）
- 单文件 ``*.json`` 暂不展开（落空返回空 Session），保留接口便于后续扩展
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


class CodexAdapter:
    """Codex 会话 Provider。

    实现 :class:`SessionProvider` Protocol。
    """

    PROVIDER_NAME = "codex"

    def __init__(self, sessions_root: Path | None = None) -> None:
        """初始化。

        Args:
            sessions_root: sessions 根目录。None 时默认 ``~/.codex/sessions``。
        """
        if sessions_root is None:
            sessions_root = resolve_path("~/.codex/sessions")
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
            if p.suffix.lower() not in (".jsonl", ".json"):
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
                f"Codex sessions root 未找到，无法读取 {session_id}"
            )
        path = self._find_session_file(root, session_id)
        if path is None:
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        if path.suffix.lower() == ".json":
            # 单文件 JSON 格式暂未实现完整解析，返回空 Session 占位
            logger.warning(
                "Codex 会话 %s 为 .json 单文件格式，当前仅支持 .jsonl，返回空会话",
                path.name,
            )
            return Session(
                session_id=session_id,
                source_path=str(path),
                completed=True,
            )
        return parse_jsonl_session(path, session_id)

    def is_completed(self, session_id: str) -> bool:
        root = self._resolve_root()
        if root is None:
            return False
        return self._find_session_file(root, session_id) is not None

    @staticmethod
    def _find_session_file(root: Path, session_id: str) -> Path | None:
        """在 root 下查找 stem 等于 session_id 的会话文件（.jsonl 优先于 .json）。"""
        if session_id == ".." or session_id == "" or "/" in session_id:
            return None
        for ext in (".jsonl", ".json"):
            candidate = root / f"{session_id}{ext}"
            if candidate.is_file():
                return candidate
        return None


__all__ = ["CodexAdapter"]
