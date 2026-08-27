"""WorkingCopy 测试（SubTask 6.1 + 6.11）。

覆盖：
- 资产路径解析（项目级 / 模块级 / 子模块级）
- 命名校验（kebab-case + 前缀）
- 创建/读/改/删资产（双区 frontmatter）
- 列举资产（按类型 / module_path / scope 过滤）
- 路径穿越防护
- AssetFile 字段访问
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.client.working_copy import (
    ASSET_TYPE_TO_DIR,
    AssetFile,
    WorkingCopy,
    asset_logical_dir,
    ensure_prefix,
    resolve_asset_path,
    validate_asset_name,
)
from server.common.models import AssetType, Scope


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def test_asset_logical_dir_mapping():
    assert asset_logical_dir(AssetType.RULE) == "rules"
    assert asset_logical_dir("memory") == "memory"
    assert asset_logical_dir(AssetType.SKILL) == "skills"


def test_asset_logical_dir_invalid_type():
    with pytest.raises(ValueError, match="未知资产类型"):
        asset_logical_dir("nonexistent")


def test_resolve_asset_path_project_level(tmp_path: Path):
    p = resolve_asset_path(tmp_path, AssetType.RULE, "lint-rule")
    assert p == tmp_path / "rules" / "lint-rule.md"


def test_resolve_asset_path_module_level(tmp_path: Path):
    p = resolve_asset_path(
        tmp_path, AssetType.RULE, "backend-lint", module_path="modules/backend"
    )
    assert p == tmp_path / "modules" / "backend" / "rules" / "backend-lint.md"


def test_resolve_asset_path_submodule_level(tmp_path: Path):
    p = resolve_asset_path(
        tmp_path,
        AssetType.MEMORY,
        "auth-tips",
        module_path="modules/backend/submodules/auth",
    )
    assert p == tmp_path / "modules" / "backend" / "submodules" / "auth" / "memory" / "auth-tips.md"


def test_resolve_asset_path_with_md_extension(tmp_path: Path):
    p = resolve_asset_path(tmp_path, AssetType.RULE, "already.md")
    assert p == tmp_path / "rules" / "already.md"


# ---------------------------------------------------------------------------
# 命名校验
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name,expected",
    [
        ("lint-rule", True),
        ("backend-lint", True),
        ("rule1", True),
        ("rule-1-a", True),
        ("Rule", False),  # 大写
        ("rule_", False),  # 下划线
        ("rule-x!", False),  # 特殊字符
        ("", False),
        ("-rule", False),  # 前导连字符
        ("rule-", False),  # 尾随连字符
        ("lint-rule.md", True),  # 含扩展名也通过
    ],
)
def test_validate_asset_name(name, expected):
    assert validate_asset_name(name) is expected


def test_ensure_prefix_adds_when_missing():
    prefixes = {"rule": "rule-", "memory": "mem-"}
    assert ensure_prefix("rule", "lint", prefixes) == "rule-lint"


def test_ensure_prefix_no_duplicate_when_present():
    prefixes = {"rule": "rule-"}
    assert ensure_prefix("rule", "rule-lint", prefixes) == "rule-lint"


def test_ensure_prefix_no_prefix_defined():
    assert ensure_prefix("tool", "runner", {}) == "runner"


# ---------------------------------------------------------------------------
# WorkingCopy 创建/读/改/删
# ---------------------------------------------------------------------------


@pytest.fixture
def wc(tmp_path: Path) -> WorkingCopy:
    return WorkingCopy(tmp_path)


def test_create_asset_project_level(wc: WorkingCopy, tmp_path: Path):
    path = wc.create_asset(
        AssetType.RULE,
        "global-lint",
        owner="alice",
        body="# 全局 lint 规则\n",
    )
    assert path.is_file()
    assert path == tmp_path / "rules" / "global-lint.md"
    # 验证 id 自动生成
    asset = wc.read_asset("rules/global-lint.md")
    assert asset.asset_id == "rule-global-lint"
    assert asset.asset_type == AssetType.RULE
    assert asset.owner == "alice"
    assert asset.scope == Scope.PRIVATE  # 默认 private
    assert asset.body.startswith("# 全局 lint 规则")


def test_create_asset_module_level(wc: WorkingCopy, tmp_path: Path):
    path = wc.create_asset(
        AssetType.RULE,
        "backend-lint",
        owner="alice",
        body="# backend lint\n",
        module_path="modules/backend",
    )
    assert path == tmp_path / "modules" / "backend" / "rules" / "backend-lint.md"
    asset = wc.read_asset("modules/backend/rules/backend-lint.md")
    assert asset.asset_id == "rule-backend-lint"
    assert asset.module_path == "modules/backend"


def test_create_asset_with_invalid_name_raises(wc: WorkingCopy):
    with pytest.raises(ValueError, match="kebab-case"):
        wc.create_asset(AssetType.RULE, "InvalidName", owner="alice", body="")


def test_create_asset_with_prefix(wc: WorkingCopy, tmp_path: Path):
    prefixes = {"rule": "rule-", "memory": "mem-"}
    path = wc.create_asset(
        AssetType.RULE,
        "lint",
        owner="alice",
        body="",
        prefixes=prefixes,
    )
    # 前缀补全 → 文件名 rule-lint.md
    assert path.name == "rule-lint.md"
    # id 仍为 rule-rule-lint（前缀已纳入 id 计算）
    asset = wc.read_asset("rules/rule-lint.md")
    assert asset.asset_id == "rule-rule-lint"


def test_create_asset_with_category(wc: WorkingCopy):
    path = wc.create_asset(
        AssetType.RULE,
        "lint",
        owner="alice",
        body="",
        category="rule-backend",
    )
    asset = wc.read_asset("rules/lint.md")
    assert asset.frontmatter["category"] == "rule-backend"


def test_write_asset_requires_id(wc: WorkingCopy):
    with pytest.raises(ValueError, match="缺少必填字段"):
        wc.write_asset("rules/x.md", {"type": "rule", "owner": "alice"}, "body")


def test_write_asset_requires_type(wc: WorkingCopy):
    with pytest.raises(ValueError, match="缺少必填字段"):
        wc.write_asset("rules/x.md", {"id": "rule-x", "owner": "alice"}, "body")


def test_write_asset_requires_owner(wc: WorkingCopy):
    with pytest.raises(ValueError, match="缺少必填字段"):
        wc.write_asset("rules/x.md", {"id": "rule-x", "type": "rule"}, "body")


def test_write_asset_creates_parent_dir(wc: WorkingCopy, tmp_path: Path):
    path = wc.write_asset(
        "modules/backend/rules/x.md",
        {"id": "rule-x", "type": "rule", "owner": "alice"},
        "body",
    )
    assert path == tmp_path / "modules" / "backend" / "rules" / "x.md"
    assert path.is_file()


def test_read_asset_not_found_raises(wc: WorkingCopy):
    with pytest.raises(FileNotFoundError):
        wc.read_asset("rules/nonexistent.md")


def test_update_body_preserves_frontmatter(wc: WorkingCopy):
    wc.create_asset(AssetType.RULE, "x", owner="alice", body="old body")
    wc.update_body("rules/x.md", "new body")
    asset = wc.read_asset("rules/x.md")
    assert asset.body == "new body"
    assert asset.owner == "alice"
    assert asset.asset_id == "rule-x"


def test_update_frontmatter_merges(wc: WorkingCopy):
    wc.create_asset(AssetType.RULE, "x", owner="alice", body="b", tags=["t1"])
    wc.update_frontmatter("rules/x.md", {"scope": "team", "tags": ["t1", "t2"]})
    asset = wc.read_asset("rules/x.md")
    assert asset.scope == Scope.TEAM
    assert asset.tags == ["t1", "t2"]
    # 未涉及字段保留
    assert asset.owner == "alice"
    assert asset.body == "b"


def test_delete_asset(wc: WorkingCopy):
    wc.create_asset(AssetType.RULE, "x", owner="alice", body="b")
    assert wc.delete_asset("rules/x.md") is True
    # 再删返回 False
    assert wc.delete_asset("rules/x.md") is False


def test_exists(wc: WorkingCopy):
    assert wc.exists("rules/x.md") is False
    wc.create_asset(AssetType.RULE, "x", owner="alice", body="b")
    assert wc.exists("rules/x.md") is True


# ---------------------------------------------------------------------------
# 路径穿越防护
# ---------------------------------------------------------------------------


def test_read_asset_path_traversal_blocked(wc: WorkingCopy):
    with pytest.raises(PermissionError, match="路径越界"):
        wc.read_asset("../../etc/passwd")


def test_write_asset_path_traversal_blocked(wc: WorkingCopy):
    with pytest.raises(PermissionError, match="路径越界"):
        wc.write_asset(
            "../../etc/evil.md",
            {"id": "x", "type": "rule", "owner": "alice"},
            "body",
        )


def test_delete_asset_path_traversal_blocked(wc: WorkingCopy):
    with pytest.raises(PermissionError, match="路径越界"):
        wc.delete_asset("../../etc/evil.md")


# ---------------------------------------------------------------------------
# 列举资产
# ---------------------------------------------------------------------------


@pytest.fixture
def wc_with_assets(wc: WorkingCopy):
    """构造含项目级 + 模块级 + 子模块级资产的仓库。"""
    wc.create_asset(AssetType.RULE, "global-lint", owner="alice", body="global")
    wc.create_asset(
        AssetType.MEMORY,
        "global-mem",
        owner="alice",
        body="global mem",
        scope=Scope.TEAM,
    )
    wc.create_asset(
        AssetType.RULE,
        "backend-lint",
        owner="bob",
        body="backend",
        module_path="modules/backend",
        scope=Scope.TEAM,
    )
    wc.create_asset(
        AssetType.RULE,
        "auth-tip",
        owner="alice",
        body="auth",
        module_path="modules/backend/submodules/auth",
    )
    return wc


def test_list_all_assets(wc_with_assets: WorkingCopy):
    assets = wc_with_assets.list_assets()
    ids = {a.asset_id for a in assets}
    assert ids == {
        "rule-global-lint",
        "memory-global-mem",
        "rule-backend-lint",
        "rule-auth-tip",
    }


def test_list_assets_by_type(wc_with_assets: WorkingCopy):
    rules = wc_with_assets.list_assets(asset_type=AssetType.RULE)
    memories = wc_with_assets.list_assets(asset_type=AssetType.MEMORY)
    assert {a.asset_id for a in rules} == {
        "rule-global-lint",
        "rule-backend-lint",
        "rule-auth-tip",
    }
    assert {a.asset_id for a in memories} == {"memory-global-mem"}


def test_list_assets_by_module_path(wc_with_assets: WorkingCopy):
    backend_assets = wc_with_assets.list_assets(module_path="modules/backend")
    # 仅 backend 模块级资产，不含子模块
    assert {a.asset_id for a in backend_assets} == {"rule-backend-lint"}


def test_list_assets_exclude_private(wc_with_assets: WorkingCopy):
    # global-lint 默认 private；backend-lint 与 global-mem 为 team
    public_only = wc_with_assets.list_assets(include_private=False)
    ids = {a.asset_id for a in public_only}
    assert "rule-global-lint" not in ids  # private
    assert "rule-auth-tip" not in ids  # private
    assert "rule-backend-lint" in ids  # team
    assert "memory-global-mem" in ids  # team


def test_list_assets_empty_repo(wc: WorkingCopy):
    assert wc.list_assets() == []


# ---------------------------------------------------------------------------
# AssetFile 字段访问
# ---------------------------------------------------------------------------


def test_asset_file_field_access(wc: WorkingCopy):
    wc.create_asset(
        AssetType.RULE,
        "x",
        owner="alice",
        body="b",
        tags=["t1", "t2"],
        scope=Scope.TEAM,
        category="rule-backend",
        version="1.2.3",
        module_path="modules/backend",
    )
    # 资产写入 modules/backend/rules/x.md（module_path 决定路径）
    asset = wc.read_asset("modules/backend/rules/x.md")
    assert asset.asset_type == AssetType.RULE
    assert asset.scope == Scope.TEAM
    assert asset.tags == ["t1", "t2"]
    assert asset.frontmatter["category"] == "rule-backend"
    assert asset.frontmatter["version"] == "1.2.3"
    assert asset.module_path == "modules/backend"


def test_asset_file_invalid_scope_falls_back_to_private(wc: WorkingCopy):
    wc.write_asset(
        "rules/x.md",
        {"id": "rule-x", "type": "rule", "owner": "alice", "scope": "invalid-scope"},
        "b",
    )
    asset = wc.read_asset("rules/x.md")
    assert asset.scope == Scope.PRIVATE


def test_asset_file_invalid_type_returns_none(wc: WorkingCopy):
    wc.write_asset(
        "rules/x.md",
        {"id": "rule-x", "type": "unknown-type", "owner": "alice"},
        "b",
    )
    asset = wc.read_asset("rules/x.md")
    assert asset.asset_type is None


# ---------------------------------------------------------------------------
# 双区 frontmatter 兼容性
# ---------------------------------------------------------------------------


def test_write_asset_preserves_coding_fields(wc: WorkingCopy):
    wc.write_asset(
        "rules/x.md",
        {"id": "rule-x", "type": "rule", "owner": "alice"},
        "body",
        coding_fields={"coding": "trae", "enabled": True},
    )
    asset = wc.read_asset("rules/x.md")
    assert asset.coding_fields.get("coding") == "trae"
    assert asset.coding_fields.get("enabled") is True
