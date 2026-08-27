"""categories.yaml 受控词汇表管理 + PR 校验。

对应技术方案 SubTask 1.6 + 3.2c：
- category 命名规范：两级 `<type>-<module>`，如 rule-backend、skill-db
- 受控词汇表存 .teamharness/categories.yaml（入 git），新 category 需 PR 登记
- PR Review 阶段校验资产 category 是否在 categories.yaml 登记，未登记阻断合入
- category 与 module_path 正交；<module> 须 INDEX.md 登记（由本模块联动校验）
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.common.models import AssetType
from server.infra_git.index_manager import IndexDoc

# category 两级命名正则：<type>-<module>
# type 限小写字母/数字；module 限小写字母数字与连字符，至少一段
CATEGORY_PATTERN = re.compile(r"^(?P<type>[a-z][a-z0-9]*)-(?P<module>[a-z0-9][a-z0-9-]*)$")
CATEGORIES_FILENAME = "categories.yaml"
CATEGORIES_DIR = ".teamharness"

# 合法 type 集合（与 AssetType 对齐 + 允许扩展）
VALID_TYPES: frozenset[str] = frozenset(t.value for t in AssetType)


@dataclass
class CategoryEntry:
    """categories.yaml 单条目。"""

    name: str
    description: str = ""
    modules: list[str] = field(default_factory=list)


@dataclass
class CategoriesRegistry:
    """受控词汇表整体。"""

    categories: dict[str, CategoryEntry] = field(default_factory=dict)
    source_path: Path | None = None

    def is_registered(self, category: str) -> bool:
        return category in self.categories


# ---------------------------------------------------------------------------
# 解析与序列化
# ---------------------------------------------------------------------------


def parse_categories_yaml(content: str) -> CategoriesRegistry:
    """解析 categories.yaml 为 registry。

    支持两种格式：
    1. 列表式：`categories: [{name, description, modules: []}]`
    2. 映射式：`categories: {rule-backend: {description, modules: []}}`
    """
    data = yaml.safe_load(content) or {}
    if not isinstance(data, dict):
        return CategoriesRegistry()
    raw = data.get("categories") or []
    registry = CategoriesRegistry()
    if isinstance(raw, list):
        for item in raw:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name", ""))
            if not name:
                continue
            registry.categories[name] = CategoryEntry(
                name=name,
                description=str(item.get("description", "")),
                modules=list(item.get("modules") or []),
            )
    elif isinstance(raw, dict):
        for name, meta in raw.items():
            name = str(name)
            modules: list[str] = []
            desc = ""
            if isinstance(meta, dict):
                desc = str(meta.get("description", ""))
                modules = list(meta.get("modules") or [])
            elif isinstance(meta, str):
                desc = meta
            registry.categories[name] = CategoryEntry(
                name=name, description=desc, modules=modules
            )
    return registry


def serialize_categories_yaml(registry: CategoriesRegistry) -> str:
    """序列化 registry 为 categories.yaml 文本。"""
    data = {
        "categories": [
            {"name": c.name, "description": c.description, "modules": c.modules}
            for c in registry.categories.values()
        ]
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_categories(repo_root: Path) -> CategoriesRegistry:
    """从仓库加载 categories.yaml。"""
    path = Path(repo_root) / CATEGORIES_DIR / CATEGORIES_FILENAME
    if not path.is_file():
        return CategoriesRegistry(source_path=path)
    registry = parse_categories_yaml(path.read_text(encoding="utf-8"))
    registry.source_path = path
    return registry


# ---------------------------------------------------------------------------
# 校验
# ---------------------------------------------------------------------------


@dataclass
class CategoryViolation:
    """category 校验违规。"""

    asset_path: str
    category: str
    reason: str


def validate_category_format(category: str) -> bool:
    """校验 category 是否符合两级 `<type>-<module>` 命名规范。"""
    if not category:
        return False
    m = CATEGORY_PATTERN.match(category)
    if not m:
        return False
    # type 须在合法集合内
    return m.group("type") in VALID_TYPES


def parse_category(category: str) -> tuple[str, str] | None:
    """拆分 category 为 (type, module)，非法返回 None。"""
    m = CATEGORY_PATTERN.match(category)
    if not m or m.group("type") not in VALID_TYPES:
        return None
    return m.group("type"), m.group("module")


def check_module_registered(
    category: str, docs: list[IndexDoc]
) -> bool:
    """校验 category 的 <module> 是否在任一 INDEX.md 的 module 字段登记。

    module 可跨层登记（project/module/submodule 任一匹配即通过）。
    """
    parsed = parse_category(category)
    if parsed is None:
        return False
    _, module = parsed
    registered_modules = {doc.module for doc in docs if doc.module}
    if module in registered_modules:
        return True
    # 允许 module 名为复合（如 api-conventions），与 INDEX.md module 字段或路径匹配
    for doc in docs:
        if module == doc.module:
            return True
        if doc.source_path is not None:
            # 路径段匹配（如 modules/api-conventions）
            if module in doc.source_path.parts:
                return True
    return False


def validate_pr_categories(
    changed_assets: list[tuple[str, str]],
    registry: CategoriesRegistry,
    docs: list[IndexDoc],
) -> list[CategoryViolation]:
    """PR 校验入口：检查变更资产的 category 是否合法且已登记。

    changed_assets: [(asset_path, category), ...]
    返回违规清单（非空 → 阻断合入）。
    """
    violations: list[CategoryViolation] = []
    for asset_path, category in changed_assets:
        if not validate_category_format(category):
            violations.append(
                CategoryViolation(
                    asset_path=asset_path,
                    category=category,
                    reason="category 不符合 `<type>-<module>` 命名规范或 type 非法",
                )
            )
            continue
        if not registry.is_registered(category):
            violations.append(
                CategoryViolation(
                    asset_path=asset_path,
                    category=category,
                    reason="category 未在 categories.yaml 登记，需先 PR 登记",
                )
            )
            continue
        if not check_module_registered(category, docs):
            violations.append(
                CategoryViolation(
                    asset_path=asset_path,
                    category=category,
                    reason="category 的 <module> 未在任一 INDEX.md 登记",
                )
            )
    return violations


def add_category(registry: CategoriesRegistry, name: str, description: str = "", modules: list[str] | None = None) -> None:
    """向 registry 追加新 category（PR 登记用）。"""
    if not validate_category_format(name):
        raise ValueError(f"category 非法：{name}")
    registry.categories[name] = CategoryEntry(
        name=name, description=description, modules=modules or []
    )
