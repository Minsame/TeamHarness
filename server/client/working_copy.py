"""本地记忆文件夹读写（git working copy 管理）。

对应 SubTask 6.1 + 技术方案 3.5.1 职责 1：
- 本地记忆文件夹即 git working copy，所有 rules/memory/skills/tools/prompts 资产
  以文件形式落盘并通过 git 跟踪。
- 提供资产文件的 CRUD（创建/读/改/删）+ 列举 + frontmatter 头维护。
- 文件路径解析结合 mapping.yaml（逻辑层 → 物理层映射），但本模块只关心
  **逻辑层**视角；物理层映射由 mapping.py 负责。

不直接调 git 命令——所有 git 写入由 git_sync.py 统一封装，本模块只读写工作区文件。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.common.models import AssetType, Scope
from server.infra_git.trae_adapter import (
    TraeFrontmatter,
    parse_frontmatter_dual,
    serialize_frontmatter_dual,
)

# 资产类型 → 逻辑目录名（与 index_manager.ASSET_DIRS 对齐）
ASSET_TYPE_TO_DIR: dict[AssetType, str] = {
    AssetType.RULE: "rules",
    AssetType.MEMORY: "memory",
    AssetType.SKILL: "skills",
    AssetType.TOOL: "tools",
    AssetType.PROMPT: "prompts",
}
DIR_TO_ASSET_TYPE: dict[str, AssetType] = {v: k for k, v in ASSET_TYPE_TO_DIR.items()}

# 资产文件扩展名（与 index_manager.ASSET_EXTENSIONS 对齐）
ASSET_SUFFIX = ".md"

# kebab-case + 前缀 命名校验
KEBAB_CASE_RE = re.compile(r"^[a-z][a-z0-9]*(?:-[a-z0-9]+)*$")


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class AssetFile:
    """资产文件值对象（本地视图）。

    `relative_path` 相对仓库根的 POSIX 路径；
    `frontmatter` 为 teamharness 区字段（id/type/owner/scope/tags/...）；
    `body` 为正文。
    """

    relative_path: str
    frontmatter: dict[str, Any] = field(default_factory=dict)
    body: str = ""
    coding_fields: dict[str, Any] = field(default_factory=dict)

    @property
    def asset_id(self) -> str:
        return str(self.frontmatter.get("id", ""))

    @property
    def asset_type(self) -> AssetType | None:
        raw = self.frontmatter.get("type")
        if not raw:
            return None
        try:
            return AssetType(raw)
        except ValueError:
            return None

    @property
    def owner(self) -> str:
        return str(self.frontmatter.get("owner", ""))

    @property
    def scope(self) -> Scope:
        raw = self.frontmatter.get("scope", Scope.PRIVATE.value)
        try:
            return Scope(raw)
        except ValueError:
            return Scope.PRIVATE

    @property
    def tags(self) -> list[str]:
        raw = self.frontmatter.get("tags") or []
        return [str(t) for t in raw] if isinstance(raw, list) else []

    @property
    def module_path(self) -> str:
        return str(self.frontmatter.get("module_path", ""))


# ---------------------------------------------------------------------------
# 路径解析
# ---------------------------------------------------------------------------


def asset_logical_dir(asset_type: AssetType | str) -> str:
    """资产类型 → 逻辑目录名。"""
    if isinstance(asset_type, str):
        try:
            asset_type = AssetType(asset_type)
        except ValueError as exc:
            raise ValueError(f"未知资产类型: {asset_type}") from exc
    if asset_type not in ASSET_TYPE_TO_DIR:
        raise ValueError(f"未知资产类型: {asset_type}")
    return ASSET_TYPE_TO_DIR[asset_type]


def resolve_asset_path(
    repo_root: Path,
    asset_type: AssetType | str,
    name: str,
    *,
    module_path: str = "",
) -> Path:
    """根据资产类型 + 名称 + module_path 解析资产文件路径。

    - module_path 为空 → repo_root/<type_dir>/<name>.md
    - module_path='modules/backend' → repo_root/modules/backend/<type_dir>/<name>.md
    - module_path='modules/backend/submodules/auth' → 同上递归

    name 若已含 .md 后缀则不再追加。
    """
    type_dir = asset_logical_dir(asset_type)
    filename = name if name.endswith(ASSET_SUFFIX) else f"{name}{ASSET_SUFFIX}"
    parts: list[Path] = [Path(repo_root)]
    if module_path:
        # module_path 形如 modules/backend 或 modules/backend/submodules/auth
        # 规范化路径分隔符
        mp = module_path.replace("\\", "/").strip("/")
        parts.extend(Path(mp).parts)
    parts.append(Path(type_dir))
    parts.append(Path(filename))
    return Path(*parts)


# ---------------------------------------------------------------------------
# 命名校验
# ---------------------------------------------------------------------------


def validate_asset_name(name: str) -> bool:
    """资产文件名校验：kebab-case（不含扩展名）。"""
    if not name:
        return False
    stem = name.removesuffix(ASSET_SUFFIX)
    return bool(KEBAB_CASE_RE.match(stem))


def ensure_prefix(asset_type: AssetType | str, name: str, prefixes: dict[str, str] | None = None) -> str:
    """按 mapping.yaml naming.prefix 规则补充前缀。

    prefixes 形如 {'rule': 'rule-', 'memory': 'mem-'}。
    若 name 已含正确前缀则不重复添加。
    """
    if prefixes is None:
        return name
    type_key = asset_type.value if isinstance(asset_type, AssetType) else str(asset_type)
    prefix = prefixes.get(type_key, "")
    if not prefix:
        return name
    if name.startswith(prefix):
        return name
    return f"{prefix}{name}"


# ---------------------------------------------------------------------------
# WorkingCopy
# ---------------------------------------------------------------------------


class WorkingCopy:
    """本地记忆文件夹读写封装。

    所有方法均工作于文件系统层面（git working copy），不调 git 命令；
    git 提交由 GitSync 负责。frontmatter 采用双区设计（Trae 兼容）。
    """

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)

    # ------------------------------------------------------------------
    # 读
    # ------------------------------------------------------------------

    def read_asset(self, relative_path: str) -> AssetFile:
        """读取资产文件并解析 frontmatter。

        raises FileNotFoundError 若文件不存在。
        """
        target = self._resolve(relative_path)
        if not target.is_file():
            raise FileNotFoundError(f"资产文件不存在: {relative_path}")
        content = target.read_text(encoding="utf-8")
        fm = parse_frontmatter_dual(content)
        return AssetFile(
            relative_path=self._rel(target),
            frontmatter=dict(fm.teamharness_fields),
            body=fm.body,
            coding_fields=dict(fm.coding_fields),
        )

    def list_assets(
        self,
        *,
        asset_type: AssetType | str | None = None,
        module_path: str | None = None,
        include_private: bool = True,
    ) -> list[AssetFile]:
        """列举资产文件。

        - asset_type 为 None → 列举所有类型
        - module_path 为 None → 递归整个仓库；否则仅列举该模块下
        - include_private=False → 过滤掉 scope=private 的资产
        """
        results: list[AssetFile] = []
        type_dirs = (
            [asset_logical_dir(asset_type)] if asset_type else list(DIR_TO_ASSET_TYPE.keys())
        )
        search_roots: list[Path] = []
        if module_path:
            mp = module_path.replace("\\", "/").strip("/")
            base = self.repo_root.joinpath(*mp.split("/"))
            for td in type_dirs:
                search_roots.append(base / td)
        else:
            # 项目级 + modules/*/{type_dir} + modules/*/submodules/*/{type_dir}
            for td in type_dirs:
                search_roots.append(self.repo_root / td)
            modules_root = self.repo_root / "modules"
            if modules_root.is_dir():
                for module_dir in modules_root.iterdir():
                    if not module_dir.is_dir():
                        continue
                    for td in type_dirs:
                        search_roots.append(module_dir / td)
                    subs = module_dir / "submodules"
                    if subs.is_dir():
                        for sub in subs.iterdir():
                            if not sub.is_dir():
                                continue
                            for td in type_dirs:
                                search_roots.append(sub / td)

        for root in search_roots:
            if not root.is_dir():
                continue
            for f in sorted(root.rglob(f"*{ASSET_SUFFIX}")):
                if not f.is_file():
                    continue
                if f.name == "INDEX.md":
                    continue
                asset = self.read_asset(self._rel(f))
                if not include_private and asset.scope == Scope.PRIVATE:
                    continue
                results.append(asset)
        return results

    def exists(self, relative_path: str) -> bool:
        return self._resolve(relative_path).is_file()

    # ------------------------------------------------------------------
    # 写
    # ------------------------------------------------------------------

    def write_asset(
        self,
        relative_path: str,
        frontmatter: dict[str, Any],
        body: str,
        *,
        coding_fields: dict[str, Any] | None = None,
    ) -> Path:
        """写入资产文件（自动创建父目录）。

        frontmatter 必须含 id/type/owner 字段，否则抛 ValueError。
        coding_fields 为 coding 软件专用字段（如 {'coding': 'trae', 'enabled': true}）。
        """
        self._validate_frontmatter(frontmatter)
        target = self._resolve(relative_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        fm_obj = TraeFrontmatter(
            coding_fields=dict(coding_fields) if coding_fields else {"coding": "trae"},
            teamharness_fields=dict(frontmatter),
            body=body,
        )
        text = serialize_frontmatter_dual(fm_obj)
        target.write_text(text, encoding="utf-8")
        return target

    def create_asset(
        self,
        asset_type: AssetType | str,
        name: str,
        owner: str,
        body: str,
        *,
        module_path: str = "",
        scope: Scope = Scope.PRIVATE,
        tags: list[str] | None = None,
        category: str | None = None,
        version: str = "0.0.1",
        coding_fields: dict[str, Any] | None = None,
        prefixes: dict[str, str] | None = None,
    ) -> Path:
        """便捷创建：自动生成 id + 解析路径 + 写入。

        id 规则：{type}-{name}（如 rule-backend-lint）。
        name 应已包含 module 前缀以保证唯一性（如 'backend-lint'）；
        module_path 仅用于路径解析，不参与 id 生成。
        name 不符合命名规范时抛 ValueError。
        """
        if not validate_asset_name(name):
            raise ValueError(f"资产名称不符合 kebab-case: {name}")
        name = ensure_prefix(asset_type, name, prefixes)
        type_key = asset_type.value if isinstance(asset_type, AssetType) else str(asset_type)
        # id 规则：type-name（name 已含 module 前缀，不再重复拼接）
        asset_id = f"{type_key}-{name}"
        frontmatter: dict[str, Any] = {
            "id": asset_id,
            "type": type_key,
            "owner": owner,
            "scope": scope.value,
            "tags": tags or [],
            "version": version,
            "module_path": module_path or "",
        }
        if category:
            frontmatter["category"] = category
        path = resolve_asset_path(self.repo_root, asset_type, name, module_path=module_path)
        return self.write_asset(
            self._rel(path), frontmatter, body, coding_fields=coding_fields
        )

    def update_body(self, relative_path: str, new_body: str) -> Path:
        """仅更新 body，保留原 frontmatter。"""
        asset = self.read_asset(relative_path)
        return self.write_asset(
            relative_path,
            asset.frontmatter,
            new_body,
            coding_fields=asset.coding_fields,
        )

    def update_frontmatter(
        self, relative_path: str, updates: dict[str, Any]
    ) -> Path:
        """合并更新 frontmatter 字段（保留未涉及字段与 body）。"""
        asset = self.read_asset(relative_path)
        merged = {**asset.frontmatter, **updates}
        return self.write_asset(
            relative_path,
            merged,
            asset.body,
            coding_fields=asset.coding_fields,
        )

    def delete_asset(self, relative_path: str) -> bool:
        """删除资产文件。返回是否实际删除（不存在则 False）。"""
        target = self._resolve(relative_path)
        if not target.is_file():
            return False
        target.unlink()
        return True

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    def _resolve(self, relative_path: str) -> Path:
        """将相对路径解析为绝对路径，防路径穿越。"""
        target = (self.repo_root / relative_path).resolve()
        root = self.repo_root.resolve()
        try:
            target.relative_to(root)
        except ValueError as exc:
            raise PermissionError(f"路径越界仓库根: {relative_path}") from exc
        return target

    def _rel(self, abs_path: Path) -> str:
        """绝对路径 → POSIX 风格相对路径。"""
        return abs_path.relative_to(self.repo_root).as_posix()

    @staticmethod
    def _validate_frontmatter(frontmatter: dict[str, Any]) -> None:
        for required in ("id", "type", "owner"):
            if not frontmatter.get(required):
                raise ValueError(f"frontmatter 缺少必填字段: {required}")


# ---------------------------------------------------------------------------
# 简单 YAML 兜底（无 frontmatter 时）
# ---------------------------------------------------------------------------


def parse_simple_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    """兼容解析单块 YAML frontmatter（旧式资产文件，无 coding 区）。

    主要供 WorkingCopy 内部使用；新写文件统一走双区设计。
    """
    # 复用 index_manager.parse_frontmatter，避免重复实现
    from server.infra_git.index_manager import parse_frontmatter
    return parse_frontmatter(content)


__all__ = [
    "ASSET_SUFFIX",
    "ASSET_TYPE_TO_DIR",
    "DIR_TO_ASSET_TYPE",
    "AssetFile",
    "WorkingCopy",
    "asset_logical_dir",
    "ensure_prefix",
    "parse_simple_frontmatter",
    "resolve_asset_path",
    "validate_asset_name",
]
