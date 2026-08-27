"""Task 1 — coding_adapters 指纹表与注册表测试。

验证点：
- resolve_path 跨平台路径展开（~ 与环境变量）
- SOFTWARE_FINGERPRINTS 覆盖全部 6 个软件且结构完整
- discover_installed 在无软件安装时返回空列表
- discover_installed 通过路径 / CLI 命中已安装软件
- get_session_providers 返回 provider 类名列表
"""

from __future__ import annotations

import sys
from pathlib import Path

from server.coding_adapters import registry
from server.coding_adapters.fingerprints import (
    SOFTWARE_FINGERPRINTS,
    resolve_path,
)
from server.coding_adapters.registry import (
    CodingSoftwareRegistry,
    InstalledSoftware,
)

# ---------------------------------------------------------------------------
# resolve_path
# ---------------------------------------------------------------------------


def test_resolve_path_expands_tilde():
    """~ 展开为用户主目录。"""
    result = resolve_path("~/some/sub/path")
    assert not str(result).startswith("~")
    assert str(result).startswith(str(Path.home()))


def test_resolve_path_env_var(tmp_path: Path, monkeypatch):
    """环境变量按平台语法展开。"""
    monkeypatch.setenv("TEAMHARNESS_TEST_VAR", str(tmp_path))
    if sys.platform == "win32":
        template = "%TEAMHARNESS_TEST_VAR%/sub/dir"
    else:
        template = "$TEAMHARNESS_TEST_VAR/sub/dir"
    assert resolve_path(template) == tmp_path / "sub" / "dir"


def test_resolve_path_literal_passthrough(tmp_path: Path):
    """无变量/无 ~ 的绝对路径原样返回。"""
    assert resolve_path(str(tmp_path)) == tmp_path


# ---------------------------------------------------------------------------
# SOFTWARE_FINGERPRINTS
# ---------------------------------------------------------------------------


def test_fingerprints_contains_all_six():
    expected = {"trae", "claude_code", "codex", "cursor", "aider", "windsurf"}
    assert set(SOFTWARE_FINGERPRINTS.keys()) == expected


def test_fingerprints_entry_structure():
    required_keys = {"detect", "sessions", "rules", "memory", "provider"}
    for name, fp in SOFTWARE_FINGERPRINTS.items():
        assert required_keys <= set(fp.keys()), (
            f"{name} 缺少字段: {required_keys - set(fp.keys())}"
        )
        detect = fp["detect"]
        assert "cli" in detect
        assert isinstance(detect["paths"], list)
        assert isinstance(fp["provider"], str) and fp["provider"]


def test_fingerprints_provider_names_unique():
    providers = [fp["provider"] for fp in SOFTWARE_FINGERPRINTS.values()]
    assert len(providers) == len(set(providers)), "provider 类名应唯一"


# ---------------------------------------------------------------------------
# discover_installed
# ---------------------------------------------------------------------------


def _fake_fp_all_missing(tmp_path: Path) -> dict:
    """全部软件指向不存在路径、cli=None 的指纹表。"""
    return {
        "trae": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_trae")]},
            "provider": "TraeAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "claude_code": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_claude")]},
            "provider": "ClaudeCodeAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "codex": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_codex")]},
            "provider": "CodexAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "cursor": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_cursor")]},
            "provider": "CursorAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "aider": {
            "detect": {"cli": None, "paths": []},
            "provider": "AiderAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "windsurf": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_windsurf")]},
            "provider": "WindsurfAdapter", "sessions": "", "rules": [], "memory": "",
        },
    }


def test_discover_installed_empty_when_no_software(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _fake_fp_all_missing(tmp_path))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    reg = CodingSoftwareRegistry()
    assert reg.discover_installed() == []


def test_discover_installed_finds_software_by_path(monkeypatch, tmp_path: Path):
    fake_root = tmp_path / "fake_trae"
    fake_root.mkdir()
    fake_fp = {
        "trae": {
            "detect": {"cli": None, "paths": [str(fake_root)]},
            "provider": "TraeAdapter",
            "sessions": "", "rules": [], "memory": "",
        },
    }
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", fake_fp)
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    reg = CodingSoftwareRegistry()
    installed = reg.discover_installed()
    assert len(installed) == 1
    sw = installed[0]
    assert isinstance(sw, InstalledSoftware)
    assert sw.name == "trae"
    assert sw.install_path == fake_root
    assert sw.cli_path is None
    assert sw.provider_name == "TraeAdapter"


def test_discover_installed_finds_software_by_cli(monkeypatch, tmp_path: Path):
    fake_cli = tmp_path / "fake_claude_bin"
    fake_cli.write_text("")
    fake_fp = {
        "claude_code": {
            "detect": {"cli": "claude", "paths": [str(tmp_path / "no_claude")]},
            "provider": "ClaudeCodeAdapter",
            "sessions": "", "rules": [], "memory": "",
        },
    }
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", fake_fp)
    monkeypatch.setattr(
        "shutil.which",
        lambda cmd, *a, **k: str(fake_cli) if cmd == "claude" else None,
    )
    reg = CodingSoftwareRegistry()
    installed = reg.discover_installed()
    assert len(installed) == 1
    sw = installed[0]
    assert sw.name == "claude_code"
    assert sw.cli_path == Path(str(fake_cli))
    assert sw.install_path == Path(str(fake_cli))
    assert sw.provider_name == "ClaudeCodeAdapter"


def test_discover_installed_path_preferred_over_cli(monkeypatch, tmp_path: Path):
    """路径命中优先于 CLI。"""
    fake_root = tmp_path / "fake_codex"
    fake_root.mkdir()
    fake_cli = tmp_path / "fake_codex_bin"
    fake_cli.write_text("")
    fake_fp = {
        "codex": {
            "detect": {"cli": "codex", "paths": [str(fake_root)]},
            "provider": "CodexAdapter",
            "sessions": "", "rules": [], "memory": "",
        },
    }
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", fake_fp)
    monkeypatch.setattr("shutil.which", lambda *a, **k: str(fake_cli))
    reg = CodingSoftwareRegistry()
    installed = reg.discover_installed()
    assert len(installed) == 1
    # 路径命中优先，cli_path 应为 None
    assert installed[0].install_path == fake_root
    assert installed[0].cli_path is None


# ---------------------------------------------------------------------------
# get_session_providers
# ---------------------------------------------------------------------------


def test_get_session_providers(monkeypatch, tmp_path: Path):
    fake_trae = tmp_path / "trae_root"
    fake_trae.mkdir()
    fake_windsurf = tmp_path / "windsurf_root"
    fake_windsurf.mkdir()
    fake_fp = {
        "trae": {
            "detect": {"cli": None, "paths": [str(fake_trae)]},
            "provider": "TraeAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "windsurf": {
            "detect": {"cli": None, "paths": [str(fake_windsurf)]},
            "provider": "WindsurfAdapter", "sessions": "", "rules": [], "memory": "",
        },
        "aider": {
            "detect": {"cli": None, "paths": [str(tmp_path / "no_aider")]},
            "provider": "AiderAdapter", "sessions": "", "rules": [], "memory": "",
        },
    }
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", fake_fp)
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    reg = CodingSoftwareRegistry()
    assert set(reg.get_session_providers()) == {"TraeAdapter", "WindsurfAdapter"}


def test_get_session_providers_empty_when_none_installed(
    monkeypatch, tmp_path: Path
):
    monkeypatch.setattr(registry, "SOFTWARE_FINGERPRINTS", _fake_fp_all_missing(tmp_path))
    monkeypatch.setattr("shutil.which", lambda *a, **k: None)
    reg = CodingSoftwareRegistry()
    assert reg.get_session_providers() == []
