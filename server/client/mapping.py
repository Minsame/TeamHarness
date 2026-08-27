"""mapping.yaml 目录映射（两层适配模型）。

对应 SubTask 6.3 + 技术方案 3.5.2：
- TeamHarness 逻辑层（软性规范，跨软件统一）：rules / memory / skills / tools / prompts
- coding 软件物理层（硬性约束，各软件不同）：.trae-cn/memory / ~/.openclaw/workspace 等
- 通过 mapping.yaml 把逻辑层路径映射到物理层路径，且支持双向查询：
    - 逻辑→物理：write_asset 时算出物理路径
    - 物理→逻辑：从 coding 软件当前路径反查逻辑 module_path（见 module_path.py）

mapping.yaml 示例（对应技术方案 3.5.2）：
    target: trae              # 目标 coding 软件
    root: .trae-cn/memory     # 物理根目录
    layout:                   # 逻辑分类 → 物理子目录
      rules:   rules/
      memory:  memory/
      skills:  skills/
      tools:   tools/
    naming:
      convention: kebab-case
      prefix:
        rule:  "rule-"
        memory: "mem-"
    module_paths:             # 物理 cwd 子路径 → 逻辑 module_path（module_path 反查表）
      "modules/backend": "modules/backend"
      "modules/backend/submodules/auth": "modules/backend/submodules/auth"
    index: .teamharness/manifest.json

设计要点：
- root 可为相对（仓库根）或绝对路径
- layout 值为相对 root 的子目录（可空字符串表示 root 本身）
- module_paths 表用于从 coding 软件 cwd 反查逻辑 module_path
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.client.config import MAPPING_FILENAME, TEAMHARNESS_DIR
from server.common.models import AssetType

# 默认物理根（Trae）
DEFAULT_TARGET = "trae"
DEFAULT_ROOT = ".trae-cn/memory"
DEFAULT_LAYOUT: dict[str, str] = {
    "rules": "rules/",
    "memory": "memory/",
    "skills": "skills/",
    "tools": "tools/",
    "prompts": "prompts/",
}
DEFAULT_NAMING_CONVENTION = "kebab-case"
DEFAULT_INDEX_PATH = f"{TEAMHARNESS_DIR}/manifest.json"

# AssetType singular (rule/memory/skill/tool/prompt) → layout dict 键（plural 目录名）
# layout 表的键为 plural（rules/memory/skills/tools/prompts），而 AssetType.value 为 singular，
# 故 logical_to_physical 中需经此映射查 layout 子目录。
_TYPE_TO_LAYOUT_KEY: dict[str, str] = {
    "rule": "rules",
    "memory": "memory",
    "skill": "skills",
    "tool": "tools",
    "prompt": "prompts",
}


@dataclass
class MappingConfig:
    """mapping.yaml 解析结果。

    target：目标 coding 软件名（trae / cursor / openclaw / custom）。
    root：物理根目录（相对仓库根或绝对路径）。
    layout：逻辑分类 → 物理子目录（相对 root）。
    naming：命名规范（convention + per-type prefix）。
    module_paths：物理路径片段 → 逻辑 module_path 反查表。
    index：manifest.json 路径（相对仓库根）。
    """

    target: str = DEFAULT_TARGET
    root: str = DEFAULT_ROOT
    layout: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_LAYOUT))
    naming: dict[str, Any] = field(default_factory=lambda: {"convention": DEFAULT_NAMING_CONVENTION})
    module_paths: dict[str, str] = field(default_factory=dict)
    index: str = DEFAULT_INDEX_PATH
    source_path: Path | None = None

    # ------------------------------------------------------------------
    # 路径映射
    # ------------------------------------------------------------------

    def physical_root(self, repo_root: Path | str) -> Path:
        """返回物理根绝对路径。root 可为绝对或相对仓库根。"""
        root_path = Path(self.root)
        if root_path.is_absolute():
            return root_path
        return Path(repo_root) / root_path

    def logical_to_physical(
        self,
        repo_root: Path | str,
        asset_type: AssetType | str,
        name: str,
        *,
        module_path: str = "",
    ) -> Path:
        """逻辑层（asset_type + name + module_path）→ 物理层绝对路径。

        映射规则：physical_root / [module_path 物理段] / layout[type] / name.md
        若 module_path 在 module_paths 表中存在映射，使用映射后的物理段；
        否则原样拼接（仍以 modules/<m>/submodules/<s>/ 物理布局）。
        """
        type_key = asset_type.value if isinstance(asset_type, AssetType) else str(asset_type)
        # 1. 解析 module_path 到物理段
        physical_module = ""
        if module_path:
            mp = module_path.replace("\\", "/").strip("/")
            physical_module = self.module_paths.get(mp, mp)
        # 2. layout 子目录（去尾斜杠）；layout 键为 plural 目录名，需从 singular 类型映射
        layout_key = _TYPE_TO_LAYOUT_KEY.get(type_key, type_key)
        layout_subdir = (self.layout.get(layout_key, "") or "").rstrip("/")
        # 3. 文件名（保证 .md 后缀）
        filename = name if name.endswith(".md") else f"{name}.md"
        # 4. 拼接（joinpath 会正确处理含路径分隔符的字符串）
        parts: list[str] = []
        if physical_module:
            parts.append(physical_module)
        if layout_subdir:
            parts.append(layout_subdir)
        parts.append(filename)
        return self.physical_root(repo_root).joinpath(*parts)

    def physical_to_logical_module(self, physical_cwd: str | Path) -> str | None:
        """物理 cwd 子路径 → 逻辑 module_path（用于 module_path 上下文推断）。

        匹配策略（按精确到模糊）：
        1. cwd 子路径精确匹配 module_paths 键 → 返回对应值
        2. cwd 子路径以某 key 开头（含路径分隔符）→ 返回对应值（最长匹配优先）
        3. cwd 子路径含 'modules/<seg>' 形式 → 直接返回 'modules/<seg>/...'
        4. 否则返回 None（表示未识别到模块，可能为根级资产）
        """
        cwd_str = str(physical_cwd).replace("\\", "/").strip("/")
        if not cwd_str:
            return None
        # 1. 精确匹配
        if cwd_str in self.module_paths:
            return self.module_paths[cwd_str]
        # 2. 前缀匹配（最长优先）
        best_match: str | None = None
        best_len = -1
        for key, val in self.module_paths.items():
            key_norm = key.replace("\\", "/").strip("/")
            if cwd_str.startswith(key_norm + "/") and len(key_norm) > best_len:
                best_match = val
                best_len = len(key_norm)
        if best_match is not None:
            return best_match
        # 3. modules/<seg> 形式自动识别
        parts = cwd_str.split("/")
        for i, p in enumerate(parts):
            if p == "modules" and i + 1 < len(parts):
                # 收集 modules/... 直到末尾或遇到 layout 子目录名（rules/memory/...）
                collected: list[str] = ["modules"]
                for seg in parts[i + 1 :]:
                    if seg in self.layout or seg in ("rules", "memory", "skills", "tools", "prompts"):
                        break
                    collected.append(seg)
                if len(collected) >= 2:
                    return "/".join(collected)
        return None

    def get_prefix(self, asset_type: AssetType | str) -> str:
        """获取某类型的命名前缀（无则空字符串）。"""
        type_key = asset_type.value if isinstance(asset_type, AssetType) else str(asset_type)
        prefixes = self.naming.get("prefix") or {}
        if not isinstance(prefixes, dict):
            return ""
        return str(prefixes.get(type_key, ""))

    def naming_convention(self) -> str:
        return str(self.naming.get("convention", DEFAULT_NAMING_CONVENTION))


# ---------------------------------------------------------------------------
# 解析与序列化
# ---------------------------------------------------------------------------


def parse_mapping_yaml(content: str) -> MappingConfig:
    """解析 mapping.yaml 文本。"""
    data = yaml.safe_load(content) or {}
    if not isinstance(data, dict):
        return MappingConfig()
    layout_raw = data.get("layout") or {}
    layout = {str(k): str(v) for k, v in layout_raw.items()} if isinstance(layout_raw, dict) else {}
    naming_raw = data.get("naming") or {}
    naming = dict(naming_raw) if isinstance(naming_raw, dict) else {}
    module_paths_raw = data.get("module_paths") or {}
    module_paths: dict[str, str] = {}
    if isinstance(module_paths_raw, dict):
        for k, v in module_paths_raw.items():
            module_paths[str(k)] = str(v)
    elif isinstance(module_paths_raw, list):
        # 列表式：每项 {"physical": ..., "logical": ...}
        for item in module_paths_raw:
            if isinstance(item, dict) and "physical" in item and "logical" in item:
                module_paths[str(item["physical"])] = str(item["logical"])
    return MappingConfig(
        target=str(data.get("target", DEFAULT_TARGET)),
        root=str(data.get("root", DEFAULT_ROOT)),
        layout=layout or dict(DEFAULT_LAYOUT),
        naming=naming or {"convention": DEFAULT_NAMING_CONVENTION},
        module_paths=module_paths,
        index=str(data.get("index", DEFAULT_INDEX_PATH)),
    )


def serialize_mapping_yaml(cfg: MappingConfig) -> str:
    """序列化 MappingConfig 为 yaml 文本。"""
    data: dict[str, Any] = {
        "target": cfg.target,
        "root": cfg.root,
        "layout": cfg.layout,
        "naming": cfg.naming,
        "module_paths": cfg.module_paths,
        "index": cfg.index,
    }
    return yaml.safe_dump(data, sort_keys=False, allow_unicode=True)


def load_mapping(repo_root: Path | str) -> MappingConfig:
    """从仓库加载 mapping.yaml；不存在则返回默认配置（向后兼容）。"""
    path = Path(repo_root) / TEAMHARNESS_DIR / MAPPING_FILENAME
    if not path.is_file():
        return MappingConfig(source_path=path)
    cfg = parse_mapping_yaml(path.read_text(encoding="utf-8"))
    cfg.source_path = path
    return cfg


def save_mapping(cfg: MappingConfig, *, path: Path | None = None) -> Path:
    """写回 mapping.yaml。"""
    target = path or cfg.source_path or (cfg.physical_root(".") / TEAMHARNESS_DIR / MAPPING_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(serialize_mapping_yaml(cfg), encoding="utf-8")
    return target


__all__ = [
    "DEFAULT_INDEX_PATH",
    "DEFAULT_LAYOUT",
    "DEFAULT_NAMING_CONVENTION",
    "DEFAULT_ROOT",
    "DEFAULT_TARGET",
    "MappingConfig",
    "load_mapping",
    "parse_mapping_yaml",
    "save_mapping",
    "serialize_mapping_yaml",
]
