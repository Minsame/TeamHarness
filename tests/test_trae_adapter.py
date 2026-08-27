"""Trae 深度适配测试。

对应 SubTask 1.5：
- frontmatter 双区设计（coding 字段与 teamharness 字段分离，互不干扰）
- 单区/无 frontmatter 兼容
- 会话路径自动探测 discover_sessions_root（按 OS 查找 + env 覆盖）
- list_trae_sessions 增量采集
"""

from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from server.infra_git.trae_adapter import (
    CODING_MARKER_KEY,
    TEAMHARNESS_BLOCK_KEY,
    TraeFrontmatter,
    discover_sessions_root,
    list_trae_sessions,
    parse_frontmatter_dual,
    serialize_frontmatter_dual,
)


# ---------------------------------------------------------------------------
# 双区 frontmatter 解析
# ---------------------------------------------------------------------------


def test_parse_dual_frontmatter_coding_and_teamharness():
    content = (
        "---\n"
        "coding: trae\n"
        "enabled: true\n"
        "---\n\n"
        "---\n"
        "teamharness:\n"
        "  id: rule-001\n"
        "  type: rule\n"
        "  owner: alice\n"
        "---\n"
        "正文内容\n"
    )
    fm = parse_frontmatter_dual(content)
    assert fm.coding_software == "trae"
    assert fm.coding_fields["enabled"] is True
    assert fm.teamharness_fields["id"] == "rule-001"
    assert fm.teamharness_fields["type"] == "rule"
    assert fm.teamharness_fields["owner"] == "alice"
    assert "正文内容" in fm.body


def test_parse_dual_frontmatter_team_flat_fields():
    """teamharness 区字段平铺（无 teamharness 命名空间嵌套）也应被识别。"""
    content = (
        "---\n"
        "coding: trae\n"
        "---\n\n"
        "---\n"
        "id: rule-002\n"
        "type: rule\n"
        "---\n"
        "body\n"
    )
    fm = parse_frontmatter_dual(content)
    assert fm.coding_software == "trae"
    assert fm.teamharness_fields["id"] == "rule-002"


def test_parse_single_coding_block():
    """只有 coding 区。"""
    content = "---\ncoding: trae\nfoo: bar\n---\nbody\n"
    fm = parse_frontmatter_dual(content)
    assert fm.coding_software == "trae"
    assert fm.coding_fields["foo"] == "bar"
    assert fm.teamharness_fields == {}
    assert "body" in fm.body


def test_parse_single_teamharness_block():
    """只有 teamharness 区。"""
    content = "---\nteamharness:\n  id: x\n---\nbody\n"
    fm = parse_frontmatter_dual(content)
    assert fm.coding_fields == {}
    assert fm.teamharness_fields["id"] == "x"


def test_parse_no_frontmatter():
    content = "plain text\n"
    fm = parse_frontmatter_dual(content)
    assert fm.coding_fields == {}
    assert fm.teamharness_fields == {}
    assert fm.body == content


def test_parse_teamharness_invalid_yaml_returns_empty_team():
    """teamharness 区 YAML 不是 dict（如纯字符串）时回退为空。"""
    content = "---\ncoding: trae\n---\n\n---\njust string\n---\nbody\n"
    fm = parse_frontmatter_dual(content)
    # team_data 不是 dict → 回退空
    assert fm.teamharness_fields == {}


# ---------------------------------------------------------------------------
# 双区 frontmatter 序列化
# ---------------------------------------------------------------------------


def test_serialize_dual_frontmatter_round_trip():
    fm = TraeFrontmatter(
        coding_fields={"coding": "trae", "enabled": True},
        teamharness_fields={"id": "rule-001", "type": "rule"},
        body="正文",
    )
    text = serialize_frontmatter_dual(fm)
    fm2 = parse_frontmatter_dual(text)
    assert fm2.coding_software == "trae"
    assert fm2.coding_fields["enabled"] is True
    assert fm2.teamharness_fields["id"] == "rule-001"
    assert "正文" in fm2.body


def test_serialize_only_coding_block():
    fm = TraeFrontmatter(coding_fields={"coding": "trae"}, body="b")
    text = serialize_frontmatter_dual(fm)
    # 仅 coding 区，无 teamharness 区
    assert text.startswith("---\n")
    assert text.count("---") == 2  # 仅一对 ---


def test_coding_software_property_empty():
    fm = TraeFrontmatter()
    assert fm.coding_software == ""


# ---------------------------------------------------------------------------
# 会话路径自动探测
# ---------------------------------------------------------------------------


def test_discover_sessions_root_env_override(tmp_path: Path, monkeypatch):
    """TRAE_SESSIONS_ROOT 环境变量优先于 OS 默认。"""
    fake_sessions = tmp_path / "trae-sessions"
    fake_sessions.mkdir()
    (fake_sessions / "s1.jsonl").write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_sessions))
    assert discover_sessions_root() == fake_sessions


def test_discover_sessions_root_nonexistent_returns_none(tmp_path: Path, monkeypatch):
    """目录不存在 → None。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(tmp_path / "missing"))
    assert discover_sessions_root() is None


def test_list_trae_sessions_returns_jsonl_files(tmp_path: Path, monkeypatch):
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    f1 = sessions_dir / "a.jsonl"
    f2 = sessions_dir / "b.jsonl"
    f3 = sessions_dir / "c.txt"  # 非会话文件
    f1.write_text("{}", encoding="utf-8")
    time.sleep(0.01)
    f2.write_text("{}", encoding="utf-8")
    f3.write_text("not a session", encoding="utf-8")
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(sessions_dir))
    sessions = list_trae_sessions()
    names = {p.name for p in sessions}
    assert names == {"a.jsonl", "b.jsonl"}
    # 升序：a 在 b 前
    assert sessions[0].name == "a.jsonl"


def test_list_trae_sessions_since_filter(tmp_path: Path, monkeypatch):
    """since 时间戳增量采集。"""
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()
    old = sessions_dir / "old.jsonl"
    old.write_text("{}", encoding="utf-8")
    time.sleep(0.05)
    cutoff = time.time()
    time.sleep(0.05)
    new = sessions_dir / "new.jsonl"
    new.write_text("{}", encoding="utf-8")
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(sessions_dir))
    sessions = list_trae_sessions(since=cutoff)
    assert {p.name for p in sessions} == {"new.jsonl"}


def test_list_trae_sessions_no_root_returns_empty(monkeypatch):
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", "/nonexistent/path/xyz")
    assert list_trae_sessions() == []
