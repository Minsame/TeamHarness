"""分层仓库 INDEX.md 解析与防孤岛校验。

对应技术方案 3.1.4：
- INDEX.md 规范（level/parent/module/assets/submodules/counts）
- 分层结构（项目级/模块级/子模块级递归）
- 防孤岛强制校验：资产文件存在但 INDEX.md 未登记 → 阻断合入
- counts 一致性校验（不一致告警，不阻断）

INDEX.md 采用 YAML frontmatter 格式（--- 分隔），frontmatter 之后可放自由说明。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.common.models import INDEXLevel

# 资产目录名（对应技术方案 3.1.1 资产类型）
ASSET_DIRS: tuple[str, ...] = ("rules", "memory", "skills", "tools", "prompts")
# 子模块目录名
SUBMODULES_DIR = "submodules"
# 模块根目录名
MODULES_DIR = "modules"
INDEX_FILENAME = "INDEX.md"
# 视为资产文件的扩展名
ASSET_EXTENSIONS: tuple[str, ...] = (".md",)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class AssetEntry:
    """INDEX.md assets 条目。"""

    id: str
    path: str
    type: str
    purpose: str = ""


@dataclass
class SubmoduleEntry:
    """INDEX.md submodules 条目。"""

    name: str
    path: str
    purpose: str = ""


@dataclass
class IndexDoc:
    """一份 INDEX.md 的解析结果。"""

    level: INDEXLevel
    module: str
    parent: str | None = None
    assets: list[AssetEntry] = field(default_factory=list)
    submodules: list[SubmoduleEntry] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    source_path: Path | None = None  # 解析来源文件路径（本地模式）

    def asset_paths(self) -> set[str]:
        """已登记资产相对路径集合（规范化为 POSIX 风格）。"""
        return {_normalize_path(a.path) for a in self.assets}

    def submodule_paths(self) -> set[str]:
        return {_normalize_path(s.path) for s in self.submodules}


@dataclass
class OrphanViolation:
    """防孤岛校验违规项。"""

    index_path: str
    asset_path: str
    reason: str


@dataclass
class CountsMismatch:
    """counts 一致性问题。"""

    index_path: str
    field: str
    declared: int
    actual: int


@dataclass
class CIReport:
    """防孤岛 CI 校验脚本报告。

    blockers 非空 → 阻断合入；warnings 不阻断。
    """

    blockers: list[OrphanViolation] = field(default_factory=list)
    warnings: list[CountsMismatch] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.blockers


# ---------------------------------------------------------------------------
# frontmatter 解析
# ---------------------------------------------------------------------------

_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """解析 YAML frontmatter，返回 (frontmatter_dict, body)。

    无 frontmatter 时返回 ({}, 原文)。
    """
    match = _FRONTMATTER_RE.match(content)
    if not match:
        return {}, content
    fm_text, body = match.group(1), match.group(2)
    data = yaml.safe_load(fm_text) or {}
    if not isinstance(data, dict):
        return {}, content
    return data, body


def parse_index_md(content: str) -> IndexDoc:
    """解析 INDEX.md 文本为 IndexDoc。"""
    fm, _ = parse_frontmatter(content)
    level_raw = str(fm.get("level", "project")).lower()
    try:
        level = INDEXLevel(level_raw)
    except ValueError:
        level = INDEXLevel.PROJECT

    assets = [
        AssetEntry(
            id=str(a.get("id", "")),
            path=str(a.get("path", "")),
            type=str(a.get("type", "")),
            purpose=str(a.get("purpose", "")),
        )
        for a in (fm.get("assets") or [])
        if isinstance(a, dict)
    ]
    submodules = [
        SubmoduleEntry(
            name=str(s.get("name", "")),
            path=str(s.get("path", "")),
            purpose=str(s.get("purpose", "")),
        )
        for s in (fm.get("submodules") or [])
        if isinstance(s, dict)
    ]
    counts_raw = fm.get("counts") or {}
    counts = {k: int(v) for k, v in counts_raw.items()} if isinstance(counts_raw, dict) else {}

    return IndexDoc(
        level=level,
        module=str(fm.get("module", "")),
        parent=fm.get("parent"),
        assets=assets,
        submodules=submodules,
        counts=counts,
    )


def serialize_index_md(doc: IndexDoc) -> str:
    """将 IndexDoc 序列化为 INDEX.md 文本（含 frontmatter）。"""
    fm: dict[str, Any] = {
        "level": doc.level.value,
        "parent": doc.parent,
        "module": doc.module,
        "assets": [
            {"id": a.id, "path": a.path, "type": a.type, "purpose": a.purpose}
            for a in doc.assets
        ],
        "submodules": [
            {"name": s.name, "path": s.path, "purpose": s.purpose}
            for s in doc.submodules
        ],
        "counts": doc.counts,
    }
    fm_text = yaml.safe_dump(fm, sort_keys=False, allow_unicode=True)
    return f"---\n{fm_text}---\n"


def load_index(path: Path) -> IndexDoc:
    """从本地文件加载 INDEX.md。"""
    doc = parse_index_md(path.read_text(encoding="utf-8"))
    doc.source_path = path
    return doc


# ---------------------------------------------------------------------------
# 分层递归发现
# ---------------------------------------------------------------------------


def discover_levels(repo_root: Path) -> list[IndexDoc]:
    """递归发现仓库内所有 INDEX.md（项目级 → 模块级 → 子模块级）。

    扫描策略：
    1. repo_root/INDEX.md（项目级）
    2. repo_root/modules/<module>/INDEX.md（模块级）
    3. repo_root/modules/<module>/submodules/<sub>/INDEX.md（子模块级，递归）
    """
    root = Path(repo_root)
    docs: list[IndexDoc] = []
    root_index = root / INDEX_FILENAME
    if root_index.exists():
        docs.append(load_index(root_index))

    modules_root = root / MODULES_DIR
    if modules_root.is_dir():
        for module_dir in sorted(modules_root.iterdir()):
            if not module_dir.is_dir():
                continue
            _collect_module_index(module_dir, docs)
    return docs


def _collect_module_index(module_dir: Path, docs: list[IndexDoc]) -> None:
    """收集模块级及其子模块级 INDEX.md（递归）。"""
    idx = module_dir / INDEX_FILENAME
    if idx.exists():
        docs.append(load_index(idx))
    sub_root = module_dir / SUBMODULES_DIR
    if sub_root.is_dir():
        for sub_dir in sorted(sub_root.iterdir()):
            if not sub_dir.is_dir():
                continue
            _collect_module_index(sub_dir, docs)


def _normalize_path(p: str) -> str:
    """统一为 POSIX 风格相对路径，去除首尾 ./ 与多余斜杠。"""
    return re.sub(r"^\./+", "", p.replace("\\", "/")).strip("/")


# ---------------------------------------------------------------------------
# 防孤岛校验
# ---------------------------------------------------------------------------


def _list_asset_files(directory: Path) -> list[Path]:
    """列出某目录下所有资产文件（递归）。"""
    files: list[Path] = []
    if not directory.is_dir():
        return files
    for f in directory.rglob("*"):
        if f.is_file() and f.suffix in ASSET_EXTENSIONS and f.name != INDEX_FILENAME:
            files.append(f)
    return files


def _rel(path: Path, base: Path) -> str:
    """返回相对 base 的 POSIX 路径。"""
    return _normalize_path(path.relative_to(base).as_posix())


def validate_no_orphan(repo_root: Path) -> list[OrphanViolation]:
    """防孤岛校验：资产文件存在但 INDEX.md 未登记 → 违规。

    同时校验：模块目录存在但未在父级 INDEX.md 的 submodules 登记。
    本函数运行于本地工作副本（CI 场景），返回违规清单（非阻断）。
    """
    root = Path(repo_root)
    violations: list[OrphanViolation] = []

    # 1. 项目级：根目录资产目录下的文件须在根 INDEX.md 登记
    root_index = root / INDEX_FILENAME
    root_doc = load_index(root_index) if root_index.exists() else None
    if root_doc is not None:
        registered = root_doc.asset_paths()
        for asset_dir_name in ASSET_DIRS:
            asset_dir = root / asset_dir_name
            for f in _list_asset_files(asset_dir):
                rel = _rel(f, root)
                if rel not in registered:
                    violations.append(
                        OrphanViolation(
                            index_path=str(root_index.relative_to(root) or INDEX_FILENAME),
                            asset_path=rel,
                            reason="资产文件未在项目级 INDEX.md 登记",
                        )
                    )
        # 校验 modules/ 下模块是否在 submodules 登记
        modules_root = root / MODULES_DIR
        if modules_root.is_dir():
            registered_subs = root_doc.submodule_paths()
            for module_dir in sorted(modules_root.iterdir()):
                if not module_dir.is_dir():
                    continue
                sub_rel = _normalize_path(f"{MODULES_DIR}/{module_dir.name}/")
                if not any(
                    registered_subs_candidate.startswith(module_dir.name)
                    for registered_subs_candidate in registered_subs
                ) and sub_rel not in registered_subs and not registered_subs:
                    pass  # 留空，下方统一处理
                _check_module_registered(
                    module_dir, root, root_doc, MODULES_DIR, violations
                )

    # 2. 模块级与子模块级递归
    for module_dir in sorted((root / MODULES_DIR).glob("*")) if (root / MODULES_DIR).is_dir() else []:
        if module_dir.is_dir():
            _validate_module_recursive(module_dir, module_dir, violations)

    return violations


def _check_module_registered(
    module_dir: Path,
    parent_dir: Path,
    parent_doc: IndexDoc,
    parent_container: str,
    violations: list[OrphanViolation],
) -> None:
    """校验模块目录是否在父级 INDEX.md 的 submodules 中登记。"""
    parent_index = parent_dir / INDEX_FILENAME
    rel = _normalize_path(f"{parent_container}/{module_dir.name}/")
    # 在 submodule_paths 中查找匹配（按目录名或路径匹配）
    name_registered = any(
        s.name == module_dir.name
        or _normalize_path(s.path).rstrip("/").endswith(module_dir.name)
        for s in parent_doc.submodules
    )
    if not name_registered:
        violations.append(
            OrphanViolation(
                index_path=str(parent_index.relative_to(parent_dir.parents[0]) if parent_index.parent != parent_dir.parents[0] else parent_index.name),
                asset_path=rel,
                reason="模块目录未在父级 INDEX.md 的 submodules 登记",
            )
        )


def _validate_module_recursive(
    module_dir: Path, root_module_dir: Path, violations: list[OrphanViolation]
) -> None:
    """递归校验模块级 INDEX.md 的资产与子模块登记。"""
    idx = module_dir / INDEX_FILENAME
    if not idx.exists():
        return
    doc = load_index(idx)
    registered = doc.asset_paths()

    # 校验本模块资产目录
    for asset_dir_name in ASSET_DIRS:
        asset_dir = module_dir / asset_dir_name
        for f in _list_asset_files(asset_dir):
            # 相对模块目录的路径
            rel = _rel(f, module_dir)
            if rel not in registered:
                violations.append(
                    OrphanViolation(
                        index_path=str(idx.relative_to(root_module_dir.parents[0])) if root_module_dir.parents else idx.name,
                        asset_path=rel,
                        reason="资产文件未在模块级 INDEX.md 登记",
                    )
                )

    # 校验子模块登记
    sub_root = module_dir / SUBMODULES_DIR
    if sub_root.is_dir():
        registered_subs = {s.name for s in doc.submodules}
        for sub_dir in sorted(sub_root.iterdir()):
            if not sub_dir.is_dir():
                continue
            if sub_dir.name not in registered_subs:
                violations.append(
                    OrphanViolation(
                        index_path=str(idx.relative_to(root_module_dir.parents[0])) if root_module_dir.parents else idx.name,
                        asset_path=_normalize_path(f"{SUBMODULES_DIR}/{sub_dir.name}/"),
                        reason="子模块目录未在父级 INDEX.md 的 submodules 登记",
                    )
                )
            _validate_module_recursive(sub_dir, root_module_dir, violations)


# ---------------------------------------------------------------------------
# counts 一致性校验
# ---------------------------------------------------------------------------


def check_counts_consistency(repo_root: Path) -> list[CountsMismatch]:
    """校验 INDEX.md counts 字段与实际数量是否一致（不一致告警，不阻断）。

    仅校验 INDEX.md 显式声明了 counts 的层级。
    """
    root = Path(repo_root)
    mismatches: list[CountsMismatch] = []
    docs = discover_levels(root)
    for doc in docs:
        if doc.source_path is None:
            continue
        # 实际资产数：本 INDEX.md 同级的资产目录文件数
        base = doc.source_path.parent
        actual_assets = 0
        for asset_dir_name in ASSET_DIRS:
            actual_assets += len(_list_asset_files(base / asset_dir_name))
        # 实际子模块数：项目级查 modules/，模块级/子模块级查 submodules/
        if doc.level == INDEXLevel.PROJECT:
            sub_container = base / MODULES_DIR
        else:
            sub_container = base / SUBMODULES_DIR
        if sub_container.is_dir():
            actual_subs = sum(1 for d in sub_container.iterdir() if d.is_dir())
        else:
            actual_subs = 0
        idx_rel = str(doc.source_path.relative_to(root)) if doc.source_path != root else INDEX_FILENAME
        if "assets" in doc.counts and doc.counts["assets"] != actual_assets:
            mismatches.append(
                CountsMismatch(idx_rel, "assets", doc.counts["assets"], actual_assets)
            )
        if "submodules" in doc.counts and doc.counts["submodules"] != actual_subs:
            mismatches.append(
                CountsMismatch(idx_rel, "submodules", doc.counts["submodules"], actual_subs)
            )
    return mismatches


# ---------------------------------------------------------------------------
# CI 校验脚本入口
# ---------------------------------------------------------------------------


def ci_check(repo_root: Path) -> CIReport:
    """防孤岛 CI 校验脚本入口。

    blockers（资产/模块未登记）→ 阻断合入；
    warnings（counts 不一致）→ 仅告警。
    对应技术方案 SubTask 1.3。
    """
    blockers = validate_no_orphan(repo_root)
    warnings = check_counts_consistency(repo_root)
    return CIReport(blockers=blockers, warnings=warnings)
