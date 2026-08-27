"""Task 3 — 各 Adapter 的 SessionProvider 实现测试。

覆盖 ClaudeCodeAdapter / CodexAdapter / CursorAdapter / AiderAdapter /
WindsurfAdapter 的 list_sessions / read_session / is_completed，
包括正常解析、文件不存在、空文件、格式错误、路径穿越等边界。

工程规则遵守：
- 用真实 tmp_path 构造文件系统，不 mock 文件系统 API（避免扁平列表失真）
- Cursor SQLite 测试用临时 db 文件 + 只读 URI 模式
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from server.coding_adapters.aider import AiderAdapter
from server.coding_adapters.claude_code import ClaudeCodeAdapter
from server.coding_adapters.codex import CodexAdapter
from server.coding_adapters.cursor import CursorAdapter
from server.coding_adapters.windsurf import WindsurfAdapter
from server.distill_personal.session_provider import (
    Session,
    SessionMeta,
    SessionTurn,
)


# ---------------------------------------------------------------------------
# 辅助：构造 JSONL 会话文件
# ---------------------------------------------------------------------------


def _write_jsonl(path: Path, lines: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for obj in lines:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ===========================================================================
# ClaudeCodeAdapter
# ===========================================================================


class TestClaudeCodeAdapter:
    def _make_root(self, tmp_path: Path) -> Path:
        return tmp_path / "claude_projects"

    def test_list_sessions_multi_projects(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "proj_a" / "s1.jsonl",
            [{"role": "user", "content": "hi", "timestamp": "2026-01-01T00:00:00Z"}],
        )
        _write_jsonl(
            root / "proj_b" / "s2.jsonl",
            [{"role": "assistant", "content": "hello"}],
        )
        adapter = ClaudeCodeAdapter(projects_root=root)
        metas = adapter.list_sessions()
        assert len(metas) == 2
        ids = {m.session_id for m in metas}
        assert ids == {"proj_a/s1", "proj_b/s2"}
        for m in metas:
            assert isinstance(m, SessionMeta)
            assert m.completed is True

    def test_list_sessions_root_missing(self, tmp_path: Path):
        adapter = ClaudeCodeAdapter(projects_root=tmp_path / "no_such")
        assert adapter.list_sessions() == []

    def test_list_sessions_since_filter(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "proj" / "old.jsonl", [{"role": "user", "content": "old"}])
        import os
        old_path = root / "proj" / "old.jsonl"
        # 设一个较早的 mtime
        ts_old = 1000.0
        os.utime(old_path, (ts_old, ts_old))
        future = 2000.0
        metas = ClaudeCodeAdapter(projects_root=root).list_sessions(since=future)
        assert metas == []
        metas_all = ClaudeCodeAdapter(projects_root=root).list_sessions(since=0.0)
        assert len(metas_all) == 1

    def test_list_sessions_empty_file_ignored(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        # 空文件仍会出现在 list_sessions（落盘即会话），但 read_session 返回 0 turns
        (root / "proj").mkdir(parents=True)
        (root / "proj" / "empty.jsonl").write_text("", encoding="utf-8")
        metas = ClaudeCodeAdapter(projects_root=root).list_sessions()
        assert len(metas) == 1
        assert metas[0].size == 0

    def test_read_session_normal(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "proj_a" / "s1.jsonl",
            [
                {"role": "user", "content": "hello", "timestamp": "2026-01-01T00:00:00Z"},
                {"role": "assistant", "content": "hi there", "timestamp": "2026-01-01T00:00:01Z"},
            ],
        )
        adapter = ClaudeCodeAdapter(projects_root=root)
        session = adapter.read_session("proj_a/s1")
        assert isinstance(session, Session)
        assert session.session_id == "proj_a/s1"
        assert len(session.turns) == 2
        assert session.turns[0].role == "user"
        assert session.turns[0].content == "hello"
        assert session.turns[1].role == "assistant"
        assert session.started_at == "2026-01-01T00:00:00Z"
        assert session.ended_at == "2026-01-01T00:00:01Z"
        assert session.completed is True

    def test_read_session_not_found(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "proj" / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = ClaudeCodeAdapter(projects_root=root)
        with pytest.raises(FileNotFoundError):
            adapter.read_session("proj/missing")

    def test_read_session_root_missing(self, tmp_path: Path):
        adapter = ClaudeCodeAdapter(projects_root=tmp_path / "no_such")
        with pytest.raises(FileNotFoundError):
            adapter.read_session("any")

    def test_read_session_path_traversal_blocked(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "proj" / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = ClaudeCodeAdapter(projects_root=root)
        with pytest.raises(FileNotFoundError):
            adapter.read_session("../etc/passwd")

    def test_filename_with_dot_roundtrip(self, tmp_path: Path):
        """文件名 stem 含点（v1.2.jsonl）时 session_id 往返正确。"""
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "proj" / "v1.2.jsonl",
            [{"role": "user", "content": "dot"}],
        )
        adapter = ClaudeCodeAdapter(projects_root=root)
        metas = adapter.list_sessions()
        assert metas[0].session_id == "proj/v1.2"
        session = adapter.read_session("proj/v1.2")
        assert session.turns[0].content == "dot"

    def test_is_completed(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "proj" / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = ClaudeCodeAdapter(projects_root=root)
        assert adapter.is_completed("proj/s1") is True
        assert adapter.is_completed("proj/missing") is False

    def test_malformed_json_lines_skipped(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        path = root / "proj" / "s1.jsonl"
        path.parent.mkdir(parents=True)
        path.write_text(
            '{"role":"user","content":"ok"}\n'
            'NOT JSON\n'
            '{"role":"assistant","content":"still ok"}\n',
            encoding="utf-8",
        )
        adapter = ClaudeCodeAdapter(projects_root=root)
        session = adapter.read_session("proj/s1")
        # 非法行被跳过，仍解析出 2 条
        assert len(session.turns) == 2

    def test_provider_name(self):
        assert ClaudeCodeAdapter.PROVIDER_NAME == "claude_code"


# ===========================================================================
# CodexAdapter
# ===========================================================================


class TestCodexAdapter:
    def _make_root(self, tmp_path: Path) -> Path:
        return tmp_path / "codex_sessions"

    def test_list_sessions_jsonl_and_json(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "hi"}])
        (root / "s2.json").write_text('{"k":"v"}', encoding="utf-8")
        # 非 .jsonl/.json 文件被忽略
        (root / "readme.txt").write_text("ignore", encoding="utf-8")
        adapter = CodexAdapter(sessions_root=root)
        metas = adapter.list_sessions()
        ids = {m.session_id for m in metas}
        assert ids == {"s1", "s2"}

    def test_list_sessions_root_missing(self, tmp_path: Path):
        adapter = CodexAdapter(sessions_root=tmp_path / "no_such")
        assert adapter.list_sessions() == []

    def test_read_session_jsonl(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "s1.jsonl",
            [
                {"role": "user", "content": "q", "timestamp": "t1"},
                {"role": "assistant", "content": "a", "timestamp": "t2"},
            ],
        )
        adapter = CodexAdapter(sessions_root=root)
        session = adapter.read_session("s1")
        assert len(session.turns) == 2
        assert session.turns[1].content == "a"

    def test_read_session_json_returns_empty(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        root.mkdir(parents=True)
        (root / "s.json").write_text('{"k":"v"}', encoding="utf-8")
        adapter = CodexAdapter(sessions_root=root)
        session = adapter.read_session("s")
        assert isinstance(session, Session)
        assert session.turns == []
        assert session.completed is True

    def test_read_session_not_found(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = CodexAdapter(sessions_root=root)
        with pytest.raises(FileNotFoundError):
            adapter.read_session("missing")

    def test_read_session_root_missing(self, tmp_path: Path):
        adapter = CodexAdapter(sessions_root=tmp_path / "no_such")
        with pytest.raises(FileNotFoundError):
            adapter.read_session("any")

    def test_is_completed(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = CodexAdapter(sessions_root=root)
        assert adapter.is_completed("s1") is True
        assert adapter.is_completed("missing") is False

    def test_provider_name(self):
        assert CodexAdapter.PROVIDER_NAME == "codex"


# ===========================================================================
# CursorAdapter
# ===========================================================================


def _make_cursor_db(db_path: Path, items: list[tuple[str, object]]) -> None:
    """构造一个含 ItemTable 的 SQLite db，items 为 (key, value) 列表。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE ItemTable (key TEXT PRIMARY KEY, value BLOB)")
        for k, v in items:
            cur.execute(
                "INSERT INTO ItemTable (key, value) VALUES (?, ?)", (k, v)
            )
        conn.commit()
    finally:
        conn.close()


def _make_cursor_db_no_itemtable(db_path: Path) -> None:
    """构造一个无 ItemTable 的 SQLite db。"""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        cur = conn.cursor()
        cur.execute("CREATE TABLE OtherTable (k TEXT)")
        conn.commit()
    finally:
        conn.close()


class TestCursorAdapter:
    def test_list_sessions_with_ai_keys(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                (
                    "aiService.composer.history",
                    json.dumps(
                        [
                            {"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"},
                        ]
                    ),
                ),
                ("workbench.panel.location", "some-value"),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        metas = adapter.list_sessions()
        # 只 aiService 键命中
        assert len(metas) == 1
        assert metas[0].completed is True

    def test_list_sessions_file_missing(self, tmp_path: Path):
        adapter = CursorAdapter(db_path=tmp_path / "no_such.vscdb")
        assert adapter.list_sessions() == []

    def test_list_sessions_no_itemtable(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db_no_itemtable(db)
        adapter = CursorAdapter(db_path=db)
        assert adapter.list_sessions() == []

    def test_list_sessions_non_sqlite_file(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        db.write_text("this is not a sqlite database", encoding="utf-8")
        adapter = CursorAdapter(db_path=db)
        assert adapter.list_sessions() == []

    def test_list_sessions_no_ai_keys(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                ("workbench.panel.location", "x"),
                ("editor.fontFamily", "mono"),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        assert adapter.list_sessions() == []

    def test_read_session_normal(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                (
                    "aiService.composer.history",
                    json.dumps(
                        [
                            {"role": "user", "content": "hi"},
                            {"role": "assistant", "content": "hello"},
                        ]
                    ),
                ),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        metas = adapter.list_sessions()
        sid = metas[0].session_id
        session = adapter.read_session(sid)
        assert isinstance(session, Session)
        assert len(session.turns) == 2
        assert session.turns[0].role == "user"
        assert session.turns[0].content == "hi"

    def test_read_session_not_found(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                (
                    "aiService.composer.history",
                    json.dumps([{"role": "user", "content": "x"}]),
                ),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        with pytest.raises(FileNotFoundError):
            adapter.read_session("99999")

    def test_read_session_file_missing(self, tmp_path: Path):
        adapter = CursorAdapter(db_path=tmp_path / "no_such.vscdb")
        with pytest.raises(FileNotFoundError):
            adapter.read_session("1")

    def test_is_completed_true(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                (
                    "aiService.chat",
                    json.dumps([{"role": "user", "content": "x"}]),
                ),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        metas = adapter.list_sessions()
        assert adapter.is_completed(metas[0].session_id) is True

    def test_is_completed_false_when_missing(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [("aiService.chat", json.dumps([{"role": "user", "content": "x"}]))],
        )
        adapter = CursorAdapter(db_path=db)
        assert adapter.is_completed("99999") is False

    def test_is_completed_false_when_db_missing(self, tmp_path: Path):
        adapter = CursorAdapter(db_path=tmp_path / "no_such.vscdb")
        assert adapter.is_completed("1") is False

    def test_value_to_turns_handles_bytes(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                # bytes 类型 value（UTF-8 编码的 JSON）
                (
                    "aiService.composer.history",
                    json.dumps([{"role": "user", "content": "from bytes"}]).encode(
                        "utf-8"
                    ),
                ),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        metas = adapter.list_sessions()
        session = adapter.read_session(metas[0].session_id)
        assert session.turns[0].content == "from bytes"

    def test_value_to_turns_invalid_json_fallback(self, tmp_path: Path):
        db = tmp_path / "state.vscdb"
        _make_cursor_db(
            db,
            [
                ("aiService.composer.history", "not a json string"),
            ],
        )
        adapter = CursorAdapter(db_path=db)
        metas = adapter.list_sessions()
        session = adapter.read_session(metas[0].session_id)
        # 非法 JSON 整体作为单条 user 消息
        assert len(session.turns) == 1
        assert session.turns[0].role == "user"
        assert session.turns[0].content == "not a json string"

    def test_provider_name(self):
        assert CursorAdapter.PROVIDER_NAME == "cursor"


# ===========================================================================
# AiderAdapter
# ===========================================================================


class TestAiderAdapter:
    def _make_history(self, tmp_path: Path, content: str) -> Path:
        path = tmp_path / ".aider.chat.history.md"
        path.write_text(content, encoding="utf-8")
        return path

    def test_list_sessions_multiple_segments(self, tmp_path: Path):
        path = self._make_history(
            tmp_path,
            "# Session 1\n> user input 1\nassistant reply 1\n"
            "## Session 2\n> user input 2\nassistant reply 2\n",
        )
        adapter = AiderAdapter(history_path=path)
        metas = adapter.list_sessions()
        assert len(metas) == 2
        assert metas[0].session_id == ".aider.chat.history.md#0"
        assert metas[1].session_id == ".aider.chat.history.md#1"

    def test_list_sessions_file_missing(self, tmp_path: Path):
        adapter = AiderAdapter(history_path=tmp_path / "no_such.md")
        assert adapter.list_sessions() == []

    def test_list_sessions_no_headings_single_segment(self, tmp_path: Path):
        path = self._make_history(
            tmp_path, "> hello\nworld reply\n"
        )
        adapter = AiderAdapter(history_path=path)
        metas = adapter.list_sessions()
        assert len(metas) == 1
        assert metas[0].session_id == ".aider.chat.history.md#0"

    def test_read_session_normal(self, tmp_path: Path):
        path = self._make_history(
            tmp_path,
            "# Session 1\n> user input 1\nassistant reply 1\n"
            "## Session 2\n> user input 2\nassistant reply 2\n",
        )
        adapter = AiderAdapter(history_path=path)
        session = adapter.read_session(".aider.chat.history.md#0")
        assert len(session.turns) == 2
        assert session.turns[0].role == "user"
        assert session.turns[0].content == "user input 1"
        assert session.turns[1].role == "assistant"
        assert session.turns[1].content == "assistant reply 1"

    def test_read_session_second_segment(self, tmp_path: Path):
        path = self._make_history(
            tmp_path,
            "# Session 1\n> user input 1\nassistant reply 1\n"
            "## Session 2\n> user input 2\nassistant reply 2\n",
        )
        adapter = AiderAdapter(history_path=path)
        session = adapter.read_session(".aider.chat.history.md#1")
        assert len(session.turns) == 2
        assert session.turns[0].content == "user input 2"

    def test_read_session_invalid_id(self, tmp_path: Path):
        path = self._make_history(tmp_path, "# H\n> x\n")
        adapter = AiderAdapter(history_path=path)
        with pytest.raises(FileNotFoundError):
            adapter.read_session(".aider.chat.history.md#99")
        with pytest.raises(FileNotFoundError):
            adapter.read_session("bad_id")

    def test_read_session_file_missing(self, tmp_path: Path):
        adapter = AiderAdapter(history_path=tmp_path / "no_such.md")
        with pytest.raises(FileNotFoundError):
            adapter.read_session(".aider.chat.history.md#0")

    def test_is_completed(self, tmp_path: Path):
        path = self._make_history(
            tmp_path, "# H\n> x\nreply\n"
        )
        adapter = AiderAdapter(history_path=path)
        assert adapter.is_completed(".aider.chat.history.md#0") is True
        assert adapter.is_completed(".aider.chat.history.md#99") is False

    def test_is_completed_file_missing(self, tmp_path: Path):
        adapter = AiderAdapter(history_path=tmp_path / "no_such.md")
        assert adapter.is_completed(".aider.chat.history.md#0") is False

    def test_provider_name(self):
        assert AiderAdapter.PROVIDER_NAME == "aider"


# ===========================================================================
# WindsurfAdapter
# ===========================================================================


class TestWindsurfAdapter:
    def _make_root(self, tmp_path: Path) -> Path:
        return tmp_path / "windsurf_sessions"

    def test_list_sessions_normal(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "s1.jsonl",
            [{"role": "user", "content": "hi", "timestamp": "t1"}],
        )
        _write_jsonl(
            root / "s2.jsonl",
            [{"role": "assistant", "content": "hello"}],
        )
        adapter = WindsurfAdapter(sessions_root=root)
        metas = adapter.list_sessions()
        ids = {m.session_id for m in metas}
        assert ids == {"s1", "s2"}

    def test_list_sessions_ignores_non_jsonl(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "hi"}])
        (root / "ignore.txt").write_text("x", encoding="utf-8")
        (root / "ignore.json").write_text("{}", encoding="utf-8")
        adapter = WindsurfAdapter(sessions_root=root)
        metas = adapter.list_sessions()
        assert len(metas) == 1
        assert metas[0].session_id == "s1"

    def test_list_sessions_root_missing(self, tmp_path: Path):
        adapter = WindsurfAdapter(sessions_root=tmp_path / "no_such")
        assert adapter.list_sessions() == []

    def test_list_sessions_empty_dir(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        root.mkdir(parents=True)
        adapter = WindsurfAdapter(sessions_root=root)
        assert adapter.list_sessions() == []

    def test_read_session_normal(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(
            root / "s1.jsonl",
            [
                {"role": "user", "content": "q", "timestamp": "t1"},
                {"role": "assistant", "content": "a", "timestamp": "t2"},
            ],
        )
        adapter = WindsurfAdapter(sessions_root=root)
        session = adapter.read_session("s1")
        assert len(session.turns) == 2
        assert session.turns[1].content == "a"

    def test_read_session_not_found(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = WindsurfAdapter(sessions_root=root)
        with pytest.raises(FileNotFoundError):
            adapter.read_session("missing")

    def test_read_session_root_missing(self, tmp_path: Path):
        adapter = WindsurfAdapter(sessions_root=tmp_path / "no_such")
        with pytest.raises(FileNotFoundError):
            adapter.read_session("any")

    def test_is_completed(self, tmp_path: Path):
        root = self._make_root(tmp_path)
        _write_jsonl(root / "s1.jsonl", [{"role": "user", "content": "x"}])
        adapter = WindsurfAdapter(sessions_root=root)
        assert adapter.is_completed("s1") is True
        assert adapter.is_completed("missing") is False

    def test_is_completed_root_missing(self, tmp_path: Path):
        adapter = WindsurfAdapter(sessions_root=tmp_path / "no_such")
        assert adapter.is_completed("s1") is False

    def test_provider_name(self):
        assert WindsurfAdapter.PROVIDER_NAME == "windsurf"
