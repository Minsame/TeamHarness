"""manifest.json 本地缓存索引测试（SubTask 6.9 + 6.11）。

覆盖：
- ManifestBuilder.build：从 INDEX.md + 资产派生 manifest
- 私有资产索引
- save / load round-trip
- diff：新增/修改/删除
- counts 统计
- 缺失资产文件仍记录基本信息（防孤岛违规场景）
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.client.manifest import Manifest, ManifestBuilder, ManifestAssetEntry
from server.client.private_isolation import PrivateIsolation


# ---------------------------------------------------------------------------
# 复用 tests/conftest.py 的 sample_repo fixture
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_with_assets(sample_repo: Path) -> Path:
    """复用 conftest 的 sample_repo（含 INDEX.md + 1 个资产）。"""
    return sample_repo


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------


def test_build_basic(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build(head_commit="abc123")
    assert manifest.version == 1
    assert manifest.head_commit == "abc123"
    assert manifest.repo_root == str(repo_with_assets)
    # 至少含项目级 + backend 模块级 + auth 子模块级 三个 module 条目
    assert len(manifest.modules) >= 3


def test_build_includes_assets_from_index(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build()
    # 项目级有 1 个资产 rule-global-lint
    project_module = next(m for m in manifest.modules if m.level == "project")
    asset_ids = {a.id for a in project_module.assets}
    assert "rule-global-lint" in asset_ids


def test_build_module_level_assets(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build()
    backend = next(m for m in manifest.modules if m.module_path == "backend")
    asset_ids = {a.id for a in backend.assets}
    assert "rule-backend-lint" in asset_ids


def test_build_includes_private_assets(repo_with_assets: Path):
    # 在私有目录下加一个资产
    pi = PrivateIsolation(repo_with_assets)
    pi.write_private_asset(
        "rules",
        "private-rule",
        frontmatter={"id": "rule-private", "type": "rule", "owner": "alice"},
        body="# private\n",
        module_path="modules/backend",
    )
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build()
    private_ids = {a.id for a in manifest.private_assets}
    assert "rule-private" in private_ids


def test_build_exclude_private_when_disabled(repo_with_assets: Path):
    pi = PrivateIsolation(repo_with_assets)
    pi.write_private_asset(
        "rules", "private-rule",
        frontmatter={"id": "rule-private", "type": "rule", "owner": "alice"}, body="",
    )
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build(include_private=False)
    assert manifest.private_assets == []


def test_build_counts(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build()
    assert manifest.counts["assets"] >= 2  # 至少 2 个公开资产
    assert manifest.counts["modules"] >= 2  # backend + auth
    assert manifest.counts["private_assets"] == 0


def test_build_handles_missing_asset_file(sample_repo: Path):
    """INDEX.md 登记了资产但文件不存在 → 仍记录基本信息。"""
    # 删除已登记的资产文件
    (sample_repo / "rules" / "global-lint.md").unlink()
    builder = ManifestBuilder(sample_repo)
    manifest = builder.build()
    project_module = next(m for m in manifest.modules if m.level == "project")
    # 资产仍被记录
    asset_ids = {a.id for a in project_module.assets}
    assert "rule-global-lint" in asset_ids
    # 但 content_hash 为空
    asset = next(a for a in project_module.assets if a.id == "rule-global-lint")
    assert asset.content_hash == ""


def test_build_content_hash_stable(repo_with_assets: Path):
    """同一资产多次构建 content_hash 一致。"""
    builder = ManifestBuilder(repo_with_assets)
    m1 = builder.build()
    m2 = builder.build()
    a1 = next(a for m in m1.modules if m.level == "project" for a in m.assets)
    a2 = next(a for m in m2.modules if m.level == "project" for a in m.assets)
    assert a1.content_hash == a2.content_hash
    assert a1.content_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# save / load
# ---------------------------------------------------------------------------


def test_save_creates_manifest_file(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    manifest = builder.build(head_commit="abc")
    path = builder.save(manifest)
    assert path.is_file()
    assert path == repo_with_assets / ".teamharness" / "manifest.json"


def test_load_round_trip(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    original = builder.build(head_commit="xyz")
    builder.save(original)
    loaded = builder.load()
    assert loaded is not None
    assert loaded.head_commit == "xyz"
    assert loaded.version == original.version
    assert len(loaded.modules) == len(original.modules)


def test_load_not_exists_returns_none(tmp_path: Path):
    builder = ManifestBuilder(tmp_path)
    assert builder.load() is None


def test_load_invalid_json_returns_none(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    builder.manifest_path.parent.mkdir(parents=True, exist_ok=True)
    builder.manifest_path.write_text("not json", encoding="utf-8")
    assert builder.load() is None


# ---------------------------------------------------------------------------
# diff
# ---------------------------------------------------------------------------


def test_diff_added(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    old = builder.build()
    # 新增一个资产文件
    (repo_with_assets / "rules" / "new.md").write_text(
        "---\nid: rule-new\ntype: rule\n---\n# new\n", encoding="utf-8"
    )
    # 更新 INDEX.md 登记新资产
    index_path = repo_with_assets / "INDEX.md"
    text = index_path.read_text(encoding="utf-8")
    text = text.replace(
        "counts:\n  assets: 1\n  submodules: 1",
        "counts:\n  assets: 2\n  submodules: 1",
    )
    text = text.replace(
        "  - id: rule-global-lint\n    path: rules/global-lint.md\n    type: rule\n    purpose: 全局 lint 规范",
        "  - id: rule-global-lint\n    path: rules/global-lint.md\n    type: rule\n    purpose: 全局 lint 规范\n  - id: rule-new\n    path: rules/new.md\n    type: rule\n    purpose: 新规则",
    )
    index_path.write_text(text, encoding="utf-8")
    new = builder.build()
    diff = builder.diff(old, new)
    added_paths = [a["path"] for a in diff["added"]]
    assert "rules/new.md" in added_paths


def test_diff_modified(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    old = builder.build()
    # 修改资产内容
    asset_path = repo_with_assets / "rules" / "global-lint.md"
    asset_path.write_text(
        "---\nid: rule-global-lint\ntype: rule\n---\n# modified content\n", encoding="utf-8"
    )
    new = builder.build()
    diff = builder.diff(old, new)
    modified_paths = [m["path"] for m in diff["modified"]]
    assert "rules/global-lint.md" in modified_paths


def test_diff_deleted(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    old = builder.build()
    # 删除资产文件 + 同步移除 INDEX.md 登记（manifest 派生自信 INDEX.md + 文件，
    # 若仅删文件不更新 INDEX.md，build 会按"防孤岛"原则仍记录该资产，diff 视为 modified）
    (repo_with_assets / "rules" / "global-lint.md").unlink()
    index_path = repo_with_assets / "INDEX.md"
    text = index_path.read_text(encoding="utf-8")
    text = text.replace(
        "  - id: rule-global-lint\n    path: rules/global-lint.md\n    type: rule\n    purpose: 全局 lint 规范\n",
        "",
    )
    text = text.replace(
        "counts:\n  assets: 1\n  submodules: 1",
        "counts:\n  assets: 0\n  submodules: 1",
    )
    index_path.write_text(text, encoding="utf-8")
    new = builder.build()
    diff = builder.diff(old, new)
    deleted_paths = [d["path"] for d in diff["deleted"]]
    assert "rules/global-lint.md" in deleted_paths


def test_diff_no_changes(repo_with_assets: Path):
    builder = ManifestBuilder(repo_with_assets)
    m = builder.build()
    diff = builder.diff(m, m)
    assert diff == {"added": [], "modified": [], "deleted": []}


# ---------------------------------------------------------------------------
# Manifest 序列化
# ---------------------------------------------------------------------------


def test_manifest_to_dict_and_from_dict_round_trip():
    manifest = Manifest(
        version=1,
        generated_at="2026-08-07T10:00:00Z",
        repo_root="/tmp",
        head_commit="abc",
        modules=[],
        private_assets=[
            ManifestAssetEntry(
                id="rule-x",
                type="rule",
                path=".teamharness/private/rules/x.md",
                owner="alice",
                scope="private",
                tags=["t"],
                category="rule-backend",
                version="0.0.1",
                content_hash="sha256:abc",
                module_path="modules/backend",
            )
        ],
        counts={"assets": 0, "modules": 0, "private_assets": 1},
    )
    data = manifest.to_dict()
    restored = Manifest.from_dict(data)
    assert restored.version == 1
    assert restored.head_commit == "abc"
    assert len(restored.private_assets) == 1
    assert restored.private_assets[0].id == "rule-x"
    assert restored.counts["private_assets"] == 1


def test_manifest_from_dict_tolerates_missing_fields():
    data = {"version": 1, "modules": []}
    m = Manifest.from_dict(data)
    assert m.version == 1
    assert m.modules == []
    assert m.private_assets == []
    assert m.counts == {}
