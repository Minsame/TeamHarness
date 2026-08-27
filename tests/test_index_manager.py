"""分层仓库 INDEX.md 解析与防孤岛 CI 校验测试。

对应 SubTask 1.2 + 1.3：
- INDEX.md 规范完整（level/parent/module/assets/submodules/counts）
- 分层递归发现（项目级/模块级/子模块级）
- 防孤岛校验：资产文件存在但 INDEX.md 未登记 → 阻断
- counts 一致性校验（不阻断，仅 warning）
- CI 入口 passed/blockers 判定
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.common.models import INDEXLevel
from server.infra_git.index_manager import (
    ASSET_DIRS,
    INDEX_FILENAME,
    MODULES_DIR,
    SUBMODULES_DIR,
    AssetEntry,
    CIReport,
    CountsMismatch,
    IndexDoc,
    OrphanViolation,
    SubmoduleEntry,
    ci_check,
    check_counts_consistency,
    discover_levels,
    load_index,
    parse_frontmatter,
    parse_index_md,
    serialize_index_md,
    validate_no_orphan,
)


# ---------------------------------------------------------------------------
# 解析与序列化
# ---------------------------------------------------------------------------


def test_parse_index_md_full(sample_repo: Path):
    doc = load_index(sample_repo / INDEX_FILENAME)
    assert doc.level == INDEXLevel.PROJECT
    assert doc.module == "teamharness-shared"
    assert doc.parent is None
    assert len(doc.assets) == 1
    assert doc.assets[0].id == "rule-global-lint"
    assert doc.assets[0].path == "rules/global-lint.md"
    assert doc.assets[0].type == "rule"
    assert len(doc.submodules) == 1
    assert doc.submodules[0].name == "backend"
    assert doc.counts == {"assets": 1, "submodules": 1}


def test_parse_index_md_empty_frontmatter():
    doc = parse_index_md("just text\nno frontmatter")
    assert doc.level == INDEXLevel.PROJECT  # 默认 project
    assert doc.assets == []
    assert doc.submodules == []
    assert doc.counts == {}


def test_parse_index_md_invalid_level_falls_back_to_project():
    doc = parse_index_md("---\nlevel: unknown_xyz\nmodule: x\n---\nbody")
    assert doc.level == INDEXLevel.PROJECT


def test_serialize_round_trip(sample_repo: Path):
    doc = load_index(sample_repo / INDEX_FILENAME)
    text = serialize_index_md(doc)
    doc2 = parse_index_md(text)
    assert doc2.level == doc.level
    assert doc2.module == doc.module
    assert doc2.assets == doc.assets
    assert doc2.submodules == doc.submodules
    assert doc2.counts == doc.counts


def test_parse_frontmatter_returns_dict_and_body():
    fm, body = parse_frontmatter("---\nfoo: bar\n---\nhello body")
    assert fm == {"foo": "bar"}
    assert "hello body" in body


def test_parse_frontmatter_no_frontmatter():
    fm, body = parse_frontmatter("plain text only")
    assert fm == {}
    assert body == "plain text only"


def test_index_doc_asset_paths_normalized(sample_repo: Path):
    """asset_paths 须统一为 POSIX 风格相对路径。"""
    doc = IndexDoc(
        level=INDEXLevel.PROJECT,
        module="x",
        assets=[AssetEntry(id="a", path="./rules/a.md", type="rule")],
    )
    assert doc.asset_paths() == {"rules/a.md"}


def test_index_doc_submodule_paths_normalized():
    doc = IndexDoc(
        level=INDEXLevel.PROJECT,
        module="x",
        submodules=[SubmoduleEntry(name="m", path="./modules/m/")],
    )
    assert doc.submodule_paths() == {"modules/m"}


# ---------------------------------------------------------------------------
# 分层递归发现
# ---------------------------------------------------------------------------


def test_discover_levels_recursive(sample_repo: Path):
    """递归发现项目级 + 模块级 + 子模块级 INDEX.md。"""
    docs = discover_levels(sample_repo)
    levels = [doc.level.value for doc in docs]
    modules = {doc.module for doc in docs}
    assert "project" in levels
    assert "module" in levels
    assert "submodule" in levels
    assert "teamharness-shared" in modules
    assert "backend" in modules
    assert "auth" in modules
    assert len(docs) == 3  # 项目级 + backend + auth


def test_discover_levels_empty_repo(empty_repo: Path):
    assert discover_levels(empty_repo) == []


def test_discover_levels_no_modules_dir(tmp_path: Path):
    """无 modules/ 目录时仅返回项目级 INDEX.md。"""
    repo = tmp_path / "r"
    repo.mkdir()
    (repo / "INDEX.md").write_text(
        "---\nlevel: project\nmodule: x\n---\n", encoding="utf-8"
    )
    docs = discover_levels(repo)
    assert len(docs) == 1
    assert docs[0].level == INDEXLevel.PROJECT


# ---------------------------------------------------------------------------
# 防孤岛校验
# ---------------------------------------------------------------------------


def test_validate_no_orphan_detects_unregistered_asset(repo_with_orphan: Path):
    """memory/orphan.md 未在项目级 INDEX.md 登记 → 违规。"""
    violations = validate_no_orphan(repo_with_orphan)
    asset_paths = [v.asset_path for v in violations]
    assert "memory/orphan.md" in asset_paths
    # 所有违规都应给出原因
    assert all(v.reason for v in violations)


def test_validate_no_orphan_clean_repo(sample_repo: Path):
    """干净仓库（无未登记资产）→ 无任何违规。"""
    violations = validate_no_orphan(sample_repo)
    assert violations == []


def test_validate_no_orphan_unregistered_module(tmp_path: Path):
    """modules/ 下有未登记模块 → 违规。"""
    repo = tmp_path / "r"
    (repo / "modules" / "ghost").mkdir(parents=True)
    (repo / "modules" / "ghost" / "INDEX.md").write_text(
        "---\nlevel: module\nmodule: ghost\n---\n", encoding="utf-8"
    )
    (repo / "INDEX.md").write_text(
        "---\nlevel: project\nmodule: x\nsubmodules: []\n---\n",
        encoding="utf-8",
    )
    violations = validate_no_orphan(repo)
    # ghost 模块未被父级 INDEX.md 的 submodules 登记
    assert any("ghost" in v.asset_path for v in violations)


def test_validate_no_orphan_unregistered_submodule(sample_repo: Path):
    """子模块目录存在但未在父模块 INDEX.md submodules 登记 → 违规。"""
    ghost = sample_repo / "modules" / "backend" / "submodules" / "ghost"
    ghost.mkdir(parents=True)
    (ghost / "INDEX.md").write_text(
        "---\nlevel: submodule\nmodule: ghost\n---\n", encoding="utf-8"
    )
    violations = validate_no_orphan(sample_repo)
    assert any("ghost" in v.asset_path for v in violations)


def test_ci_check_blocks_on_orphan(repo_with_orphan: Path):
    """有未登记资产 → CI 阻断。"""
    report = ci_check(repo_with_orphan)
    assert not report.passed
    assert report.blockers


def test_ci_check_passes_when_clean(sample_repo: Path):
    """干净仓库（无孤儿 + counts 一致）→ CI 通过。"""
    report = ci_check(sample_repo)
    assert report.passed, [v.reason for v in report.blockers]
    assert report.warnings == []


# ---------------------------------------------------------------------------
# counts 一致性
# ---------------------------------------------------------------------------


def test_counts_consistency_mismatch_warns(sample_repo: Path):
    """项目级 INDEX.md counts.assets=1 但实际有 2 个资产 → warning。"""
    (sample_repo / "rules" / "extra.md").write_text(
        "---\nid: rule-extra\ntype: rule\n---\n# extra\n", encoding="utf-8"
    )
    # 不登记 extra，counts.assets 仍为 1
    mismatches = check_counts_consistency(sample_repo)
    fields = [(m.field, m.declared, m.actual) for m in mismatches]
    assert ("assets", 1, 2) in fields


def test_counts_consistency_clean(sample_repo: Path):
    """干净仓库（counts 与实际匹配）→ 无 warning。"""
    assert check_counts_consistency(sample_repo) == []


def test_ci_report_passed_property():
    """blockers 非空 → passed=False。"""
    r1 = CIReport()
    assert r1.passed is True
    r2 = CIReport(blockers=[OrphanViolation("INDEX.md", "x.md", "未登记")])
    assert r2.passed is False


def test_ci_report_warnings_dont_block():
    r = CIReport(warnings=[CountsMismatch("INDEX.md", "assets", 1, 2)])
    assert r.passed is True  # warnings 不阻断


# ---------------------------------------------------------------------------
# 边界
# ---------------------------------------------------------------------------


def test_asset_dirs_constant():
    """资产目录常量须对齐技术方案 5 类。"""
    assert set(ASSET_DIRS) == {"rules", "memory", "skills", "tools", "prompts"}


def test_modules_and_submodules_dir_constants():
    assert MODULES_DIR == "modules"
    assert SUBMODULES_DIR == "submodules"
