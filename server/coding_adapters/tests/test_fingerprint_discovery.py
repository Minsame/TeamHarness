"""Task 2 — discover_by_fingerprint 指纹模糊匹配测试。

验证点：
- scan_root 不存在 / 是文件 → 空列表
- 含 sessions/*.jsonl 的目录被识别
- 含 .rules 文件/目录的目录被识别
- 含 memory/ 子目录的目录被识别
- 无特征的普通目录不被识别
- sessions/ 目录存在但无 *.jsonl 不被识别
- 已被 SOFTWARE_FINGERPRINTS 中 paths 命中的目录被跳过
- 默认 scan_root=None 时使用 Path.home()
- _sanitize_unknown_name / _looks_like_ai_software 辅助方法
"""

from __future__ import annotations

from pathlib import Path

from server.coding_adapters import registry
from server.coding_adapters.registry import (
    CodingSoftwareRegistry,
    InstalledSoftware,
)


# ---------------------------------------------------------------------------
# 辅助构造
# ---------------------------------------------------------------------------


def _empty_fp() -> dict:
    """空指纹表：无任何已知软件，保证 discover_by_fingerprint 不跳过目录。"""
    return {}


def _make_dir_with_sessions(parent: Path, name: str) -> Path:
    d = parent / name
    (d / "sessions").mkdir(parents=True)
    (d / "sessions" / "s1.jsonl").write_text("{}", encoding="utf-8")
    return d


def _make_dir_with_rules(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    (d / ".rules").write_text("rule", encoding="utf-8")
    return d


def _make_dir_with_memory(parent: Path, name: str) -> Path:
    d = parent / name
    (d / "memory").mkdir(parents=True)
    return d


def _make_plain_dir(parent: Path, name: str) -> Path:
    d = parent / name
    d.mkdir(parents=True)
    return d


# ---------------------------------------------------------------------------
# scan_root 边界
# ---------------------------------------------------------------------------


def test_discover_by_fingerprint_empty_when_root_missing(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(tmp_path / "no_such") == []


def test_discover_by_fingerprint_empty_when_root_is_file(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    f = tmp_path / "file.txt"
    f.write_text("x", encoding="utf-8")
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(f) == []


def test_discover_by_fingerprint_empty_when_root_is_empty_dir(
    tmp_path: Path, monkeypatch
):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(tmp_path) == []


# ---------------------------------------------------------------------------
# 特征识别
# ---------------------------------------------------------------------------


def test_detects_sessions_with_jsonl(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    _make_dir_with_sessions(tmp_path, "toolA")
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint(tmp_path)
    assert len(result) == 1
    sw = result[0]
    assert isinstance(sw, InstalledSoftware)
    assert sw.name == "unknown_toola"
    assert sw.provider_name == "GenericJsonlSessionProvider"
    assert sw.cli_path is None
    assert sw.install_path == tmp_path / "toolA"


def test_detects_rules_file(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    _make_dir_with_rules(tmp_path, "toolB")
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint(tmp_path)
    assert len(result) == 1
    assert result[0].name == "unknown_toolb"


def test_detects_rules_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    d = tmp_path / "toolB2"
    (d / ".rules").mkdir(parents=True)
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint(tmp_path)
    assert len(result) == 1
    assert result[0].name == "unknown_toolb2"


def test_detects_memory_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    _make_dir_with_memory(tmp_path, "toolC")
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint(tmp_path)
    assert len(result) == 1
    assert result[0].name == "unknown_toolc"


def test_ignores_plain_directory(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    _make_plain_dir(tmp_path, "plain")
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(tmp_path) == []


def test_sessions_dir_without_jsonl_not_matched(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    d = tmp_path / "empty_sessions"
    (d / "sessions").mkdir(parents=True)  # 无 .jsonl 文件
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(tmp_path) == []


def test_multiple_dirs_detected(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    _make_dir_with_sessions(tmp_path, "toolA")
    _make_dir_with_rules(tmp_path, "toolB")
    _make_dir_with_memory(tmp_path, "toolC")
    _make_plain_dir(tmp_path, "plain")
    reg = CodingSoftwareRegistry()
    names = {s.name for s in reg.discover_by_fingerprint(tmp_path)}
    assert names == {"unknown_toola", "unknown_toolb", "unknown_toolc"}


def test_ignores_non_directory_entries(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    # 在 scan_root 下放一个文件（非目录），不应触发异常
    (tmp_path / "stray.jsonl").write_text("{}", encoding="utf-8")
    reg = CodingSoftwareRegistry()
    assert reg.discover_by_fingerprint(tmp_path) == []


# ---------------------------------------------------------------------------
# 已知软件路径去重
# ---------------------------------------------------------------------------


def test_skips_known_software_paths(tmp_path: Path, monkeypatch):
    known = _make_dir_with_sessions(tmp_path, "known_trae")
    fake_fp = {
        "trae": {
            "detect": {"cli": None, "paths": [str(known)]},
            "provider": "TraeAdapter",
            "sessions": "", "rules": [], "memory": "",
        },
    }
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", fake_fp)
    _make_dir_with_sessions(tmp_path, "unknown_tool")
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint(tmp_path)
    names = {s.name for s in result}
    assert "unknown_known_trae" not in names
    assert "unknown_unknown_tool" in names


# ---------------------------------------------------------------------------
# 默认 scan_root
# ---------------------------------------------------------------------------


def test_default_scan_root_is_home(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _empty_fp())
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    _make_dir_with_sessions(tmp_path, "hometool")
    reg = CodingSoftwareRegistry()
    result = reg.discover_by_fingerprint()
    assert len(result) == 1
    assert result[0].name == "unknown_hometool"


# ---------------------------------------------------------------------------
# 辅助方法
# ---------------------------------------------------------------------------


def test_sanitize_unknown_name_handles_special_chars():
    assert (
        CodingSoftwareRegistry._sanitize_unknown_name("My Tool!")
        == "unknown_my_tool"
    )
    assert (
        CodingSoftwareRegistry._sanitize_unknown_name("...")
        == "unknown_unknown"
    )
    assert CodingSoftwareRegistry._sanitize_unknown_name("ABC") == "unknown_abc"
    assert CodingSoftwareRegistry._sanitize_unknown_name("a-b_c.d") == "unknown_a_b_c_d"


def test_looks_like_ai_software_branches(tmp_path: Path):
    # sessions + jsonl
    d1 = tmp_path / "d1"
    (d1 / "sessions").mkdir(parents=True)
    (d1 / "sessions" / "a.jsonl").write_text("{}", encoding="utf-8")
    assert CodingSoftwareRegistry._looks_like_ai_software(d1) is True
    # .rules 文件
    d2 = tmp_path / "d2"
    d2.mkdir()
    (d2 / ".rules").write_text("r", encoding="utf-8")
    assert CodingSoftwareRegistry._looks_like_ai_software(d2) is True
    # memory 目录
    d3 = tmp_path / "d3"
    (d3 / "memory").mkdir(parents=True)
    assert CodingSoftwareRegistry._looks_like_ai_software(d3) is True
    # 无特征
    d4 = tmp_path / "d4"
    d4.mkdir()
    assert CodingSoftwareRegistry._looks_like_ai_software(d4) is False
    # sessions 目录但无 jsonl
    d5 = tmp_path / "d5"
    (d5 / "sessions").mkdir(parents=True)
    assert CodingSoftwareRegistry._looks_like_ai_software(d5) is False
