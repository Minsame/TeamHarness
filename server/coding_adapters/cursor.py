"""Cursor 会话 Adapter（SQLite）。

对应 Task 3：读取 ``~/.cursor/state.vscdb``，实现 SessionProvider Protocol。

实现要点：
- 用标准库 :mod:`sqlite3` 以只读 URI 模式打开（``mode=ro&immutable=1``），
  避免锁定 Cursor 正在使用的数据库文件
- 从 ``ItemTable`` 表查询 ``key LIKE '%aiService%'`` 等键
- 若 sqlite3 无法打开、表不存在、查询失败 → 降级为空列表（不抛异常）
- session_id 采用 ``<rowid>`` 或 ``<key>`` 字符串
"""

from __future__ import annotations

import logging
import sqlite3
from pathlib import Path
from urllib.parse import quote

from server.coding_adapters.fingerprints import resolve_path
from server.distill_personal.session_provider import (
    Session,
    SessionMeta,
    SessionProvider,
    SessionTurn,
)

logger = logging.getLogger(__name__)

# Cursor 的 ItemTable 中与 AI 会话相关的键前缀（保守匹配，避免误伤）
_AI_KEY_PATTERNS = (
    "%aiService%",
    "%composer%",
    "%chatHistory%",
    "%aiChat%",
)


class CursorAdapter:
    """Cursor 会话 Provider。

    实现 :class:`SessionProvider` Protocol。从 Cursor ``state.vscdb`` SQLite
    文件中读取 AI 会话相关条目。

    所有读取操作均不会抛出异常：文件缺失 / 锁定 / 表缺失 / 查询失败
    一律降级为空列表或 ``False``，保证调用方安全。
    """

    PROVIDER_NAME = "cursor"

    def __init__(self, db_path: Path | None = None) -> None:
        """初始化。

        Args:
            db_path: state.vscdb 文件路径。None 时默认
                ``~/.cursor/state.vscdb``。
        """
        if db_path is None:
            db_path = resolve_path("~/.cursor/state.vscdb")
        self._db_path = db_path

    # ------------------------------------------------------------------
    # SessionProvider Protocol
    # ------------------------------------------------------------------

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        rows = self._query_ai_rows()
        if rows is None:
            return []
        metas: list[SessionMeta] = []
        for rowid, key, value in rows:
            try:
                stat = self._db_path.stat()
                mtime = stat.st_mtime
                size = stat.st_size
            except OSError:
                mtime = 0.0
                size = 0
            if since is not None and mtime < since:
                continue
            metas.append(
                SessionMeta(
                    session_id=str(rowid),
                    source_path=str(self._db_path),
                    mtime=mtime,
                    size=size,
                    completed=True,
                )
            )
        metas.sort(key=lambda m: m.mtime)
        return metas

    def read_session(self, session_id: str) -> Session:
        rows = self._query_ai_rows()
        if rows is None:
            raise FileNotFoundError(
                f"Cursor state.vscdb 无法读取，无法获取 {session_id}"
            )
        for rowid, key, value in rows:
            if str(rowid) == session_id:
                turns = self._value_to_turns(value)
                return Session(
                    session_id=session_id,
                    turns=turns,
                    source_path=str(self._db_path),
                    completed=True,
                    metadata={"key": key},
                )
        raise FileNotFoundError(f"会话不存在: {session_id}")

    def is_completed(self, session_id: str) -> bool:
        rows = self._query_ai_rows()
        if rows is None:
            return False
        return any(str(rowid) == session_id for rowid, _, _ in rows)

    # ------------------------------------------------------------------
    # 内部：SQLite 读取
    # ------------------------------------------------------------------

    def _query_ai_rows(self) -> list[tuple[int, str, object]] | None:
        """查询 ItemTable 中 AI 相关条目。

        Returns:
            ``(rowid, key, value)`` 列表；任何异常均返回 None（降级）。
        """
        if not self._db_path.is_file():
            return None
        uri = self._readonly_uri(self._db_path)
        try:
            # immutable=1 模式不获取锁，即使 Cursor 正在运行也可读取
            conn = sqlite3.connect(uri, uri=True, timeout=0.5)
        except sqlite3.Error as exc:
            logger.warning("Cursor state.vscdb 无法打开（%s），降级为空", exc)
            return None
        try:
            cur = conn.cursor()
            # 先确认 ItemTable 存在
            try:
                cur.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='ItemTable'"
                )
                if cur.fetchone() is None:
                    logger.info("Cursor state.vscdb 无 ItemTable，降级为空")
                    return []
            except sqlite3.Error as exc:
                logger.warning("查询 ItemTable 元数据失败（%s），降级为空", exc)
                return []
            # 查询 AI 相关键
            clause = " OR ".join(
                f"key LIKE ?" for _ in _AI_KEY_PATTERNS
            )
            sql = (
                f"SELECT rowid, key, value FROM ItemTable WHERE {clause}"
            )
            try:
                cur.execute(sql, list(_AI_KEY_PATTERNS))
                rows = cur.fetchall()
            except sqlite3.Error as exc:
                logger.warning("查询 AI 会话条目失败（%s），降级为空", exc)
                return []
            return [(r[0], r[1], r[2]) for r in rows]
        finally:
            conn.close()

    @staticmethod
    def _readonly_uri(db_path: Path) -> str:
        """构造只读 + immutable 的 SQLite URI。

        ``file:<url-encoded-path>?mode=ro&immutable=1``
        - 路径用 :func:`urllib.parse.quote` 编码以兼容 Windows 盘符与空格
        - immutable=1 跳过文件锁，避免与 Cursor 进程争用
        """
        abs_path = db_path.resolve()
        # 跨平台：file: URI 使用正斜杠，绝对路径以 / 开头
        # Windows D:\\a\\b → file:///D:/a/b
        quoted = quote(str(abs_path), safe=":/")
        if not quoted.startswith("/"):
            quoted = "/" + quoted
        return f"file:{quoted}?mode=ro&immutable=1"

    @staticmethod
    def _value_to_turns(value: object) -> list[SessionTurn]:
        """将 SQLite value 字段转为 SessionTurn 列表。

        Cursor 实际存储格式可能为 JSON 字符串或 blob。此处采用宽松策略：
        - 字符串：尝试 JSON 解析，若为 list 则逐项取 role/content；
          解析失败时整体作为单条 user 消息内容
        - bytes：尝试 utf-8 解码后递归
        - 其他类型：转为字符串放入单条消息
        """
        if value is None:
            return []
        if isinstance(value, (bytes, bytearray)):
            try:
                value = value.decode("utf-8", errors="replace")
            except Exception:
                return [SessionTurn(role="user", content="<binary blob>")]
        if isinstance(value, str):
            import json

            try:
                obj = json.loads(value)
            except json.JSONDecodeError:
                return [SessionTurn(role="user", content=value)]
            if isinstance(obj, list):
                turns: list[SessionTurn] = []
                for item in obj:
                    if not isinstance(item, dict):
                        continue
                    turns.append(
                        SessionTurn(
                            role=str(item.get("role", "user")).lower(),
                            content=str(item.get("content", "")),
                        )
                    )
                return turns
            if isinstance(obj, dict):
                role = str(obj.get("role", "user")).lower()
                content = str(obj.get("content", value))
                return [SessionTurn(role=role, content=content)]
            return [SessionTurn(role="user", content=str(obj))]
        return [SessionTurn(role="user", content=str(value))]


__all__ = ["CursorAdapter"]
