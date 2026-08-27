"""mapping.yaml + module_path 推断测试（SubTask 6.3 + 6.5 + 6.11）。

覆盖：
- mapping.yaml 解析与序列化（默认 + 自定义）
- 逻辑→物理路径映射
- 物理→逻辑 module_path 反查（精确/前缀/模糊匹配）
- module_path 推断（cwd/explicit/env 优先级）
- 跨软件路径差异处理（Trae / Cursor / OpenClaw）
- 命名前缀
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from server.client.mapping import (
    DEFAULT_LAYOUT,
    DEFAULT_ROOT,
    DEFAULT_TARGET,
    MappingConfig,
    load_mapping,
    parse_mapping_yaml,
    save_mapping,
    serialize_mapping_yaml,
)
from server.client.module_path import (
    ModulePathInference,
    from_env,
    from_explicit,
    infer_from_cwd,
    infer_module_path,
    is_valid_module_path,
    normalize_module_path,
)
from server.common.models import AssetType


# ---------------------------------------------------------------------------
# mapping.yaml 解析
# ---------------------------------------------------------------------------


def test_parse_default_mapping():
    cfg = parse_mapping_yaml("")
    assert cfg.target == DEFAULT_TARGET
    assert cfg.root == DEFAULT_ROOT
    assert cfg.layout == dict(DEFAULT_LAYOUT)


def test_parse_full_mapping():
    text = """
target: trae
root: .trae-cn/memory
layout:
  rules: rules/
  memory: memory/
  skills: skills/
  tools: tools/
naming:
  convention: kebab-case
  prefix:
    rule: "rule-"
    memory: "mem-"
module_paths:
  "modules/backend": "modules/backend"
  "modules/backend/submodules/auth": "modules/backend/submodules/auth"
index: .teamharness/manifest.json
"""
    cfg = parse_mapping_yaml(text)
    assert cfg.target == "trae"
    assert cfg.root == ".trae-cn/memory"
    assert cfg.layout["rules"] == "rules/"
    assert cfg.naming["convention"] == "kebab-case"
    assert cfg.naming["prefix"]["rule"] == "rule-"
    assert cfg.module_paths["modules/backend"] == "modules/backend"
    assert cfg.index == ".teamharness/manifest.json"


def test_parse_module_paths_list_format():
    """module_paths 也支持列表式 [{physical, logical}]。"""
    text = """
module_paths:
  - physical: "modules/backend"
    logical: "modules/backend"
"""
    cfg = parse_mapping_yaml(text)
    assert cfg.module_paths == {"modules/backend": "modules/backend"}


def test_serialize_round_trip():
    cfg = MappingConfig(
        target="cursor",
        root="~/.cursor/memories",
        layout={"rules": "rules/", "memory": "memory/"},
        naming={"convention": "kebab-case", "prefix": {"rule": "r-"}},
        module_paths={"modules/x": "modules/x"},
        index=".teamharness/manifest.json",
    )
    text = serialize_mapping_yaml(cfg)
    cfg2 = parse_mapping_yaml(text)
    assert cfg2.target == "cursor"
    assert cfg2.root == "~/.cursor/memories"
    assert cfg2.layout == cfg.layout
    assert cfg2.naming == cfg.naming
    assert cfg2.module_paths == cfg.module_paths


def test_load_mapping_not_exists_returns_default(tmp_path: Path):
    cfg = load_mapping(tmp_path)
    assert cfg.target == DEFAULT_TARGET
    assert cfg.source_path == tmp_path / ".teamharness" / "mapping.yaml"


def test_load_mapping_from_file(tmp_path: Path):
    th_dir = tmp_path / ".teamharness"
    th_dir.mkdir()
    (th_dir / "mapping.yaml").write_text(
        yaml.safe_dump({"target": "cursor", "root": "~/.cursor/memories"}),
        encoding="utf-8",
    )
    cfg = load_mapping(tmp_path)
    assert cfg.target == "cursor"
    assert cfg.root == "~/.cursor/memories"
    assert cfg.source_path is not None


def test_save_mapping_creates_parent(tmp_path: Path):
    cfg = MappingConfig(target="custom")
    path = save_mapping(cfg, path=tmp_path / ".teamharness" / "mapping.yaml")
    assert path.is_file()
    cfg2 = parse_mapping_yaml(path.read_text(encoding="utf-8"))
    assert cfg2.target == "custom"


# ---------------------------------------------------------------------------
# 逻辑 → 物理路径
# ---------------------------------------------------------------------------


def test_logical_to_physical_project_level(tmp_path: Path):
    cfg = MappingConfig()
    p = cfg.logical_to_physical(tmp_path, AssetType.RULE, "lint")
    assert p == tmp_path / ".trae-cn" / "memory" / "rules" / "lint.md"


def test_logical_to_physical_module_level(tmp_path: Path):
    cfg = MappingConfig()
    p = cfg.logical_to_physical(
        tmp_path, AssetType.RULE, "backend-lint", module_path="modules/backend"
    )
    assert p == tmp_path / ".trae-cn" / "memory" / "modules" / "backend" / "rules" / "backend-lint.md"


def test_logical_to_physical_with_module_paths_override(tmp_path: Path):
    """module_paths 表中映射可以重定向物理路径。"""
    cfg = MappingConfig(module_paths={"modules/backend": "custom/backend-dir"})
    p = cfg.logical_to_physical(
        tmp_path, AssetType.RULE, "lint", module_path="modules/backend"
    )
    # 使用映射后的物理段
    assert "custom" in p.parts
    assert "backend-dir" in p.parts


def test_logical_to_physical_absolute_root(tmp_path: Path):
    cfg = MappingConfig(root=str(tmp_path / "absolute" / "mem"))
    p = cfg.logical_to_physical(tmp_path, AssetType.RULE, "x")
    assert p == tmp_path / "absolute" / "mem" / "rules" / "x.md"


def test_logical_to_physical_custom_layout(tmp_path: Path):
    """layout 子目录可自定义（如全部放 flat）。"""
    cfg = MappingConfig(layout={"rules": "", "memory": ""})
    p = cfg.logical_to_physical(tmp_path, AssetType.RULE, "x")
    assert p == tmp_path / ".trae-cn" / "memory" / "x.md"


# ---------------------------------------------------------------------------
# 物理 → 逻辑 module_path 反查
# ---------------------------------------------------------------------------


def test_physical_to_logical_exact_match():
    cfg = MappingConfig(module_paths={"modules/backend": "modules/backend"})
    assert cfg.physical_to_logical_module("modules/backend") == "modules/backend"


def test_physical_to_logical_prefix_match():
    cfg = MappingConfig(
        module_paths={
            "modules/backend": "modules/backend",
            "modules/backend/submodules/auth": "modules/backend/submodules/auth",
        }
    )
    # cwd 在 modules/backend/rules/ 下（含 layout 子目录）
    assert (
        cfg.physical_to_logical_module("modules/backend/rules")
        == "modules/backend"
    )


def test_physical_to_logical_longest_prefix_wins():
    cfg = MappingConfig(
        module_paths={
            "modules/backend": "modules/backend",
            "modules/backend/submodules/auth": "modules/backend/submodules/auth",
        }
    )
    # cwd 在 modules/backend/submodules/auth/rules → 应匹配最长的 auth 段
    assert (
        cfg.physical_to_logical_module("modules/backend/submodules/auth/rules")
        == "modules/backend/submodules/auth"
    )


def test_physical_to_logical_auto_modules_pattern():
    """无 module_paths 表时，自动识别 modules/<seg> 模式。"""
    cfg = MappingConfig()
    assert cfg.physical_to_logical_module("modules/backend/rules") == "modules/backend"
    assert (
        cfg.physical_to_logical_module("modules/backend/submodules/auth/memory")
        == "modules/backend/submodules/auth"
    )


def test_physical_to_logical_empty_returns_none():
    cfg = MappingConfig()
    assert cfg.physical_to_logical_module("") is None


def test_physical_to_logical_no_match_returns_none():
    cfg = MappingConfig()
    # 完全无关的路径
    assert cfg.physical_to_logical_module("/etc/passwd") is None


def test_physical_to_logical_handles_windows_path():
    cfg = MappingConfig()
    assert cfg.physical_to_logical_module("modules\\backend\\rules") == "modules/backend"


# ---------------------------------------------------------------------------
# 命名前缀
# ---------------------------------------------------------------------------


def test_get_prefix_returns_per_type():
    cfg = MappingConfig(naming={"prefix": {"rule": "rule-", "memory": "mem-"}})
    assert cfg.get_prefix(AssetType.RULE) == "rule-"
    assert cfg.get_prefix(AssetType.MEMORY) == "mem-"
    assert cfg.get_prefix(AssetType.SKILL) == ""  # 未配置


def test_get_prefix_no_prefix_section():
    cfg = MappingConfig(naming={"convention": "kebab-case"})
    assert cfg.get_prefix(AssetType.RULE) == ""


def test_naming_convention():
    cfg = MappingConfig()
    assert cfg.naming_convention() == "kebab-case"
    cfg2 = MappingConfig(naming={"convention": "snake_case"})
    assert cfg2.naming_convention() == "snake_case"


# ---------------------------------------------------------------------------
# module_path 推断
# ---------------------------------------------------------------------------


def test_from_explicit_returns_explicit():
    r = from_explicit("modules/backend")
    assert r.source == "explicit"
    assert r.module_path == "modules/backend"
    assert r.confidence == 1.0


def test_from_explicit_empty_returns_none():
    r = from_explicit("")
    assert r.source == "none"
    assert r.confidence == 0.0


def test_from_explicit_none_returns_none():
    r = from_explicit(None)
    assert r.source == "none"


def test_from_explicit_normalizes_path():
    r = from_explicit("modules\\backend")
    assert r.module_path == "modules/backend"


def test_from_env_set(monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_MODULE_PATH", "modules/auth")
    r = from_env()
    assert r.source == "env"
    assert r.module_path == "modules/auth"
    assert r.confidence == 0.9


def test_from_env_unset_returns_none(monkeypatch):
    monkeypatch.delenv("TEAMHARNESS_MODULE_PATH", raising=False)
    r = from_env()
    assert r.source == "none"


def test_infer_from_cwd_in_physical_root(tmp_path: Path, monkeypatch):
    """cwd 在物理根下 → 无 module_path（根级）。"""
    physical_root = tmp_path / ".trae-cn" / "memory"
    physical_root.mkdir(parents=True)
    cfg = MappingConfig()
    r = infer_from_cwd(cwd=physical_root, mapping=cfg, repo_root=tmp_path)
    assert r.source == "none"  # 根级无 module_path


def test_infer_from_cwd_in_module_dir(tmp_path: Path):
    """cwd 在 modules/backend/rules 下 → 反查为 modules/backend。"""
    physical_root = tmp_path / ".trae-cn" / "memory"
    module_dir = physical_root / "modules" / "backend" / "rules"
    module_dir.mkdir(parents=True)
    cfg = MappingConfig()
    r = infer_from_cwd(cwd=module_dir, mapping=cfg, repo_root=tmp_path)
    assert r.source == "cwd"
    assert r.module_path == "modules/backend"


def test_infer_from_cwd_with_explicit_module_paths(tmp_path: Path):
    """module_paths 表精确匹配 confidence=0.8。"""
    physical_root = tmp_path / "mem"
    module_dir = physical_root / "modules" / "backend"
    module_dir.mkdir(parents=True)
    cfg = MappingConfig(
        root="mem",
        module_paths={"modules/backend": "modules/backend"},
    )
    r = infer_from_cwd(cwd=module_dir, mapping=cfg, repo_root=tmp_path)
    assert r.source == "cwd"
    assert r.module_path == "modules/backend"
    assert r.confidence == 0.8  # 精确匹配


def test_infer_from_cwd_outside_physical_root(tmp_path: Path):
    """cwd 不在物理根下 → 尝试自动识别 modules/<seg>。"""
    cfg = MappingConfig()
    cwd = tmp_path / "other" / "modules" / "frontend"
    cwd.mkdir(parents=True)
    r = infer_from_cwd(cwd=cwd, mapping=cfg, repo_root=tmp_path)
    # 路径中含 modules/frontend → 自动识别
    assert r.source == "cwd"
    assert r.module_path == "modules/frontend"


# ---------------------------------------------------------------------------
# infer_module_path 优先级
# ---------------------------------------------------------------------------


def test_infer_module_path_explicit_wins(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_MODULE_PATH", "modules/env")
    r = infer_module_path(
        explicit="modules/explicit",
        cwd=tmp_path,  # 仓库根，cwd 反查会返回 none
        repo_root=tmp_path,
    )
    assert r.source == "explicit"
    assert r.module_path == "modules/explicit"


def test_infer_module_path_cwd_beats_env(tmp_path: Path, monkeypatch):
    """cwd 命中 → 优先于 env。"""
    monkeypatch.setenv("TEAMHARNESS_MODULE_PATH", "modules/env")
    physical_root = tmp_path / "mem"
    cwd = physical_root / "modules" / "backend" / "rules"
    cwd.mkdir(parents=True)
    cfg = MappingConfig(root="mem")
    r = infer_module_path(cwd=cwd, mapping=cfg, repo_root=tmp_path)
    assert r.source == "cwd"
    assert r.module_path == "modules/backend"


def test_infer_module_path_env_when_no_explicit_no_cwd(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_MODULE_PATH", "modules/env")
    r = infer_module_path(repo_root=tmp_path)  # cwd 为当前工作目录
    # 当前工作目录不在物理根 → 自动识别可能命中也可能不命中，取决于执行环境
    # 至少 env 是兜底
    assert r.source in ("cwd", "env")


def test_infer_module_path_none_when_all_fail(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("TEAMHARNESS_MODULE_PATH", raising=False)
    # cwd 为 tmp_path（仓库根），不在物理根下
    r = infer_module_path(repo_root=tmp_path)
    # 仓库根反查 → none
    assert r.source == "none"
    assert r.confidence == 0.0


# ---------------------------------------------------------------------------
# 合法性校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mp,expected",
    [
        ("", True),  # 根级
        ("modules/backend", True),
        ("modules/backend/submodules/auth", True),
        ("Modules/Backend", False),  # 大写
        ("modules//backend", False),  # 空段
        ("modules/ backend", False),  # 含空格
    ],
)
def test_is_valid_module_path(mp, expected):
    assert is_valid_module_path(mp) is expected


def test_normalize_module_path():
    assert normalize_module_path("modules\\backend") == "modules/backend"
    assert normalize_module_path("/modules/backend/") == "modules/backend"
    assert normalize_module_path("") == ""
