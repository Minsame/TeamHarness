"""私有资产隔离测试（SubTask 6.8 + 6.11）。

覆盖：
- .gitignore 检查与自动修复
- 私有资产读写
- 私有资产列举（按 module_path / type）
- 私有资产删除
- promote_to_team（提升为团队资产）
- 路径穿越防护
- 镜像分层结构（modules/<m>/submodules/<s>/rules/...）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.client.private_isolation import (
    ALLOWED_SHARED_FILES,
    PrivateIsolation,
    REQUIRED_GITIGNORE_RULES,
)
from server.client.config import TEAMHARNESS_DIR
from server.common.models import Scope


@pytest.fixture
def pi(tmp_path: Path) -> PrivateIsolation:
    return PrivateIsolation(tmp_path)


# ---------------------------------------------------------------------------
# .gitignore 检查
# ---------------------------------------------------------------------------


def test_check_gitignore_no_file(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    status = pi.check_gitignore()
    assert status.exists is False
    assert set(status.missing_rules) == set(REQUIRED_GITIGNORE_RULES)
    assert status.ok is False


def test_check_gitignore_complete(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "\n".join(REQUIRED_GITIGNORE_RULES) + "\n", encoding="utf-8"
    )
    status = pi.check_gitignore()
    assert status.exists is True
    assert status.missing_rules == []
    assert status.ok is True


def test_check_gitignore_partial_missing(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    (tmp_path / ".gitignore").write_text(
        "\n".join(REQUIRED_GITIGNORE_RULES[:2]) + "\n", encoding="utf-8"
    )
    status = pi.check_gitignore()
    assert status.ok is False
    assert set(status.missing_rules) == set(REQUIRED_GITIGNORE_RULES[2:])


def test_ensure_gitignore_appends_missing(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    # 初始无 .gitignore
    status = pi.ensure_gitignore(append=True)
    assert status.ok is True
    assert status.fixed is True
    # 文件存在且含所有规则
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    for rule in REQUIRED_GITIGNORE_RULES:
        assert rule in text
    for allowed in ALLOWED_SHARED_FILES:
        assert allowed in text


def test_ensure_gitignore_preserves_existing_content(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    (tmp_path / ".gitignore").write_text("# my rules\nnode_modules/\n", encoding="utf-8")
    pi.ensure_gitignore(append=True)
    text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert "# my rules" in text
    assert "node_modules/" in text
    assert ".teamharness/private/" in text


def test_ensure_gitignore_no_append_returns_status(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    status = pi.ensure_gitignore(append=False)
    assert status.ok is False
    assert status.fixed is False
    # 文件未创建
    assert not (tmp_path / ".gitignore").is_file()


def test_ensure_gitignore_idempotent(tmp_path: Path):
    pi = PrivateIsolation(tmp_path)
    pi.ensure_gitignore(append=True)
    text1 = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    # 再次调用不应重复追加
    pi.ensure_gitignore(append=True)
    text2 = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert text1 == text2


# ---------------------------------------------------------------------------
# 私有资产读写
# ---------------------------------------------------------------------------


def test_write_private_asset_forces_private_scope(pi: PrivateIsolation, tmp_path: Path):
    path = pi.write_private_asset(
        "rules",
        "secret-lint",
        frontmatter={"id": "rule-secret", "type": "rule", "owner": "alice", "scope": "team"},
        body="# secret\n",
    )
    assert path.is_file()
    assert path == tmp_path / TEAMHARNESS_DIR / "private" / "rules" / "secret-lint.md"
    # 读取验证 scope 被强制为 private
    asset = pi.read_private_asset("rules/secret-lint.md")
    assert asset.scope == Scope.PRIVATE


def test_write_private_asset_with_module_path(pi: PrivateIsolation, tmp_path: Path):
    path = pi.write_private_asset(
        "rules",
        "backend-secret",
        frontmatter={"id": "rule-backend-secret", "type": "rule", "owner": "alice"},
        body="x",
        module_path="modules/backend",
    )
    assert path == tmp_path / TEAMHARNESS_DIR / "private" / "modules" / "backend" / "rules" / "backend-secret.md"


def test_write_private_asset_invalid_type_dir(pi: PrivateIsolation):
    with pytest.raises(ValueError, match="非法资产目录"):
        pi.write_private_asset(
            "invalid",
            "x",
            frontmatter={"id": "x", "type": "rule", "owner": "alice"},
            body="",
        )


def test_read_private_asset_not_found(pi: PrivateIsolation):
    with pytest.raises(FileNotFoundError):
        pi.read_private_asset("rules/nonexistent.md")


def test_read_private_asset_path_traversal(pi: PrivateIsolation):
    with pytest.raises(PermissionError, match="路径越界"):
        pi.read_private_asset("../../etc/passwd")


def test_write_private_asset_creates_nested_dirs(pi: PrivateIsolation, tmp_path: Path):
    path = pi.write_private_asset(
        "memory",
        "auth-tips",
        frontmatter={"id": "mem-auth-tips", "type": "memory", "owner": "alice"},
        body="",
        module_path="modules/backend/submodules/auth",
    )
    assert path.is_file()


# ---------------------------------------------------------------------------
# 列举
# ---------------------------------------------------------------------------


@pytest.fixture
def pi_with_assets(pi: PrivateIsolation):
    pi.write_private_asset(
        "rules", "global-secret",
        frontmatter={"id": "rule-gs", "type": "rule", "owner": "alice"}, body="",
    )
    pi.write_private_asset(
        "rules", "backend-secret",
        frontmatter={"id": "rule-bs", "type": "rule", "owner": "bob"}, body="",
        module_path="modules/backend",
    )
    pi.write_private_asset(
        "memory", "backend-tips",
        frontmatter={"id": "mem-bt", "type": "memory", "owner": "alice"}, body="",
        module_path="modules/backend",
    )
    return pi


def test_list_private_assets_all(pi_with_assets: PrivateIsolation):
    assets = pi_with_assets.list_private_assets()
    ids = {a.asset_id for a in assets}
    assert ids == {"rule-gs", "rule-bs", "mem-bt"}


def test_list_private_assets_by_type(pi_with_assets: PrivateIsolation):
    rules = pi_with_assets.list_private_assets(asset_type_dir="rules")
    memories = pi_with_assets.list_private_assets(asset_type_dir="memory")
    assert {a.asset_id for a in rules} == {"rule-gs", "rule-bs"}
    assert {a.asset_id for a in memories} == {"mem-bt"}


def test_list_private_assets_by_module(pi_with_assets: PrivateIsolation):
    backend = pi_with_assets.list_private_assets(module_path="modules/backend")
    ids = {a.asset_id for a in backend}
    assert ids == {"rule-bs", "mem-bt"}


def test_list_private_assets_by_module_and_type(pi_with_assets: PrivateIsolation):
    backend_rules = pi_with_assets.list_private_assets(
        module_path="modules/backend", asset_type_dir="rules"
    )
    assert {a.asset_id for a in backend_rules} == {"rule-bs"}


def test_list_private_assets_empty(pi: PrivateIsolation):
    assert pi.list_private_assets() == []


# ---------------------------------------------------------------------------
# 删除
# ---------------------------------------------------------------------------


def test_delete_private_asset(pi: PrivateIsolation):
    pi.write_private_asset(
        "rules", "x",
        frontmatter={"id": "x", "type": "rule", "owner": "alice"}, body="",
    )
    assert pi.delete_private_asset("rules/x.md") is True
    assert pi.delete_private_asset("rules/x.md") is False  # 再删返回 False


def test_delete_private_asset_path_traversal(pi: PrivateIsolation):
    with pytest.raises(PermissionError, match="路径越界"):
        pi.delete_private_asset("../../etc/passwd")


# ---------------------------------------------------------------------------
# promote_to_team
# ---------------------------------------------------------------------------


def test_promote_to_team_writes_to_central_repo(pi: PrivateIsolation, tmp_path: Path):
    pi.write_private_asset(
        "rules",
        "promoted",
        frontmatter={"id": "rule-promoted", "type": "rule", "owner": "alice"},
        body="# promoted rule\n",
        module_path="modules/backend",
    )
    target_path = pi.promote_to_team(
        "modules/backend/rules/promoted.md",
        target_repo_root=tmp_path,
        new_scope=Scope.TEAM,
    )
    assert target_path.is_file()
    # 验证目标路径在中央仓库对应位置
    assert target_path == tmp_path / "modules" / "backend" / "rules" / "promoted.md"
    # 验证 scope 已改
    text = target_path.read_text(encoding="utf-8")
    assert "team" in text
    assert "private" not in text


def test_promote_to_team_with_invalid_scope_raises(pi: PrivateIsolation, tmp_path: Path):
    pi.write_private_asset(
        "rules", "x",
        frontmatter={"id": "rule-x", "type": "rule", "owner": "alice"}, body="",
    )
    with pytest.raises(ValueError, match="不能为 private"):
        pi.promote_to_team("rules/x.md", target_repo_root=tmp_path, new_scope=Scope.PRIVATE)
