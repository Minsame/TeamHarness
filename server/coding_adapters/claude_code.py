"""Claude Code 会话 Adapter。

对应 Task 3：读取 ``~/.claude/projects/**/*.jsonl``，实现 SessionProvider Protocol。

会话路径：``~/.claude/projects/<project>/*.jsonl``
- 每个 ``*.jsonl`` 是一个会话文件
- 每行一条 JSON 消息，含 role / content / timestamp 等字段
- 多个 project 子目录下可能存在同名会话文件，session_id 采用
  相对 projects_root 的路径去掉 ``.jsonl``（含 project 段），例如
  ``proj_a/abc`` 对应 ``projects/proj_a/abc.jsonl``
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


class ClaudeCodeAdapter:
    """Claude Code 会话 Provider。

    实现 :class:`SessionProvider` Protocol。
    """

    PROVIDER_NAME = "claude_code"

    def __init__(self, projects_root: Path | None = None) -> None:
        """初始化。

        Args:
            projects_root: projects 根目录。None 时默认 ``~/.claude/projects``。
                显式传入用于测试或自定义安装路径。
        """
        if projects_root is None:
            projects_root = resolve_path("~/.claude/projects")
        self._root = projects_root

    def _resolve_root(self) -> Path | None:
        return self._root if self._root.is_dir() else None

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        root = self._resolve_root()
        if root is None:
            return []
        metas: list[SessionMeta] = []
        # rglob 前已由 _resolve_root 保证 root 是目录
        for p in root.rglob("*.jsonl"):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since:
                continue
            session_id = self._path_to_session_id(root, p)
            metas.append(
                SessionMeta(
                    session_id=session_id,
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
                f"Claude Code projects root 未找到，无法读取 {session_id}"
            )
        path = self._session_id_to_path(root, session_id)
        if path is None or not path.is_file():
            raise FileNotFoundError(f"会话文件不存在: {session_id}")
        return parse_jsonl_session(path, session_id)

    def is_completed(self, session_id: str) -> bool:
        root = self._resolve_root()
        if root is None:
            return False
        path = self._session_id_to_path(root, session_id)
        return path is not None and path.is_file()

    # ------------------------------------------------------------------
    # session_id <-> path 转换
    # ------------------------------------------------------------------

    @staticmethod
    def _path_to_session_id(root: Path, path: Path) -> str:
        """文件路径 → session_id（相对 root 去 .jsonl，正斜杠分隔）。"""
        rel = path.relative_to(root).with_suffix("")
        return str(rel).replace("\\", "/")

    @staticmethod
    def _session_id_to_path(root: Path, session_id: str) -> Path | None:
        """session_id → 文件路径（防路径穿越）。"""
        parts = session_id.split("/")
        if any(p == ".." or p == "" for p in parts):
            return None
        # 用 with_name 附加 .jsonl，避免末段已含点时被 with_suffix 截断
        base = root.joinpath(*parts)
        return base.with_name(base.name + ".jsonl")


__all__ = ["ClaudeCodeAdapter"]
