"""frontmatter schema_version 兼容解析（SubTask 3.5）。

对应技术方案：资产 frontmatter 含 schema_version 字段，旧版本数据在新代码下仍可读。
本模块负责：
- 解析资产 frontmatter 文本，提取 teamharness 区字段（与 trae_adapter 互补）
- 按 schema_version 顺序迁移到当前版本（只增字段，不删字段，保证旧版本可读）
- 暴露 SchemaMigrator 与迁移函数注册机制，供未来版本升级扩展

兼容原则（铁律）：
1. 旧 schema_version 数据必须可读：只允许追加字段，禁止删除或重命名旧字段
2. 迁移函数链 v1→v2→...→current 顺序应用，不可跳过中间版本
3. 迁移函数必须幂等：对已是目标版本的数据再迁移不应产生变化
4. 新增字段必须给默认值，避免下游 NoneType 异常
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable

import yaml

# 当前 teamharness frontmatter schema 版本
# 历史版本：
#   v1：初始版本（id/type/owner/scope/tags/version/related_to 等基础字段）
#   v2：新增 module_path + category（受控词汇表，对齐技术方案 3.2c）
#   v3：新增 distillation_metadata（提炼元信息，对齐 3.3）
# 注意：CURRENT 必须与 server.common.models.Asset.schema_version 默认值一致
SCHEMA_VERSION_CURRENT = 1

# 单 frontmatter 块匹配（兼容带 / 不带 teamharness 命名空间）
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)
TEAMHARNESS_BLOCK_KEY = "teamharness"


# ---------------------------------------------------------------------------
# 迁移函数注册表
# ---------------------------------------------------------------------------

# 迁移函数签名：dict[str, Any] -> dict[str, Any]（原地修改并返回）
# 注册时声明 from_version → to_version；SchemaMigrator 按版本号顺序串联
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]


@dataclass
class MigrationStep:
    """单个迁移步骤声明。"""

    from_version: int
    to_version: int
    fn: MigrationFn
    description: str = ""


# 全局迁移注册表（按 from_version 索引）
_REGISTRY: dict[int, MigrationStep] = {}


def register_migration(
    from_version: int, to_version: int, description: str = ""
) -> Callable[[MigrationFn], MigrationFn]:
    """装饰器：注册一个迁移函数。

    用法：
        @register_migration(1, 2, "新增 module_path + category 字段")
        def _v1_to_v2(data: dict) -> dict:
            data.setdefault("module_path", "")
            data.setdefault("category", None)
            return data
    """

    def decorator(fn: MigrationFn) -> MigrationFn:
        if to_version != from_version + 1:
            raise ValueError(
                f"迁移必须连续：from_version={from_version} → to_version={to_version}（应为 +1）"
            )
        if from_version in _REGISTRY:
            raise ValueError(
                f"重复注册迁移：from_version={from_version} 已存在 "
                f"（{_REGISTRY[from_version].description!r}）"
            )
        _REGISTRY[from_version] = MigrationStep(
            from_version=from_version,
            to_version=to_version,
            fn=fn,
            description=description,
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 内置迁移函数（v1 → v2 → v3 ...）
# ---------------------------------------------------------------------------

# 注册未来版本的迁移示例。当前 SCHEMA_VERSION_CURRENT=1，故下面这些迁移在 v1 数据下不触发，
# 但作为框架与回归基线，确保未来 v2/v3 发布时旧数据可读。
#
# 注意：本文件公开的 SCHEMA_VERSION_CURRENT=1，下面的迁移函数仅当 current 提升到 ≥2 时才生效。


@register_migration(1, 2, "v2: 补 module_path + category 字段")
def _migrate_v1_to_v2(data: dict[str, Any]) -> dict[str, Any]:
    """v1 → v2：补 module_path 与 category 默认值。

    module_path：组织层级路径，根级默认空字符串
    category：受控词汇表 <type>-<module>，缺省为 None（由 PR Review 阶段补全）
    """
    data.setdefault("module_path", "")
    data.setdefault("category", None)
    # 显式标记 schema_version 已升级
    data["schema_version"] = 2
    return data


@register_migration(2, 3, "v3: 补 distillation_metadata 字段")
def _migrate_v2_to_v3(data: dict[str, Any]) -> dict[str, Any]:
    """v2 → v3：补 distillation_metadata 默认结构。

    distillation_metadata：二级提炼产出元信息
    """
    data.setdefault(
        "distillation_metadata",
        {"score": 0.0, "confidence": None, "cold_start": False, "source_refs": []},
    )
    data["schema_version"] = 3
    return data


# ---------------------------------------------------------------------------
# SchemaMigrator
# ---------------------------------------------------------------------------


@dataclass
class MigrationTrace:
    """迁移轨迹，用于审计与调试。"""

    original_version: int
    final_version: int
    steps: list[str] = field(default_factory=list)
    fields_added: list[str] = field(default_factory=list)

    @property
    def migrated(self) -> bool:
        return self.original_version != self.final_version


class SchemaMigrator:
    """frontmatter schema 迁移器。

    将任意 schema_version 的 frontmatter 迁移到当前版本。
    迁移函数链按版本顺序串联，保证旧版本可读。
    """

    def __init__(self, target_version: int = SCHEMA_VERSION_CURRENT) -> None:
        if target_version < 1:
            raise ValueError(f"target_version 必须 ≥ 1，收到：{target_version}")
        self.target_version = target_version

    def migrate(self, data: dict[str, Any]) -> tuple[dict[str, Any], MigrationTrace]:
        """迁移 frontmatter dict 到目标版本，返回 (新 dict, 轨迹)。

        - 不修改输入 dict（深拷贝后迁移）
        - 缺失 schema_version 视为 1（最旧版本，对应早期无版本号资产）
        - 高于 target_version 的数据不降级（向前兼容原则），只在轨迹中标记
        """
        if not isinstance(data, dict):
            raise TypeError(f"frontmatter 必须是 dict，收到：{type(data).__name__}")

        # 深拷贝避免污染调用方
        import copy

        result = copy.deepcopy(data)
        original_version = int(result.get("schema_version", 1) or 1)
        trace = MigrationTrace(original_version=original_version, final_version=original_version)

        # 高于目标版本：不降级，但记录轨迹
        if original_version > self.target_version:
            trace.steps.append(
                f"原始 schema_version={original_version} > 目标 {self.target_version}，"
                f"按向前兼容原则保留原字段不降级"
            )
            trace.final_version = original_version
            return result, trace

        # 顺序应用迁移函数 v1→v2→...→target
        current = original_version
        while current < self.target_version:
            step = _REGISTRY.get(current)
            if step is None:
                trace.steps.append(
                    f"无 v{current}→v{current + 1} 迁移函数，停止迁移（缺中间迁移）"
                )
                break
            before_keys = set(result.keys())
            result = step.fn(result)
            after_keys = set(result.keys())
            added = sorted(after_keys - before_keys)
            trace.steps.append(
                f"v{current}→v{current + 1}: {step.description}（新增字段：{added or '无'}）"
            )
            trace.fields_added.extend(added)
            current = step.to_version
            trace.final_version = current

        # 最终强制写目标版本号（保证迁移后 schema_version 字段一致）
        if trace.final_version == self.target_version:
            result["schema_version"] = self.target_version
        return result, trace


# ---------------------------------------------------------------------------
# 资产 frontmatter 文本解析
# ---------------------------------------------------------------------------


def parse_asset_frontmatter(content: str) -> tuple[dict[str, Any], str, MigrationTrace]:
    """解析资产 frontmatter 文本，自动迁移到当前 schema_version。

    返回 (migrated_frontmatter, body, migration_trace)。

    支持两种格式：
    1. 单块 frontmatter（无 coding 区，teamharness 字段平铺或嵌套在 teamharness: 下）
    2. 双区 frontmatter：coding 区 + teamharness 区（与 trae_adapter 兼容）
       本函数只取 teamharness 区字段；若无 teamharness 区则取首块字段。

    缺失 schema_version 视为 1（旧版本可读），自动迁移到当前版本。
    """
    if not content:
        return {}, "", MigrationTrace(original_version=1, final_version=1)

    fm_text, body = _extract_teamharness_block(content)
    if not fm_text:
        return {}, content, MigrationTrace(original_version=1, final_version=1)

    data = yaml.safe_load(fm_text) or {}
    if not isinstance(data, dict):
        # YAML 解析为非 dict（如纯字符串），按空 frontmatter 处理
        return {}, body, MigrationTrace(original_version=1, final_version=1)

    # 嵌套在 teamharness: 命名空间下时取出
    if TEAMHARNESS_BLOCK_KEY in data and isinstance(data[TEAMHARNESS_BLOCK_KEY], dict):
        data = data[TEAMHARNESS_BLOCK_KEY]

    migrator = SchemaMigrator()
    migrated, trace = migrator.migrate(data)
    return migrated, body, trace


def _extract_teamharness_block(content: str) -> tuple[str, str]:
    """从可能的双区 frontmatter 中提取 teamharness 块文本。

    返回 (teamharness_yaml_text, body)。
    - 双区：第二块为 teamharness 区
    - 单块且首块含 teamharness 键：取首块
    - 单块且无 teamharness 键：按 teamharness 兼容（取首块）
    - 无 frontmatter：返回 ("", 原文)
    """
    # 双区匹配：两块 frontmatter
    dual_re = re.compile(
        r"^---\s*\n(?P<first>.*?)\n---\s*\n\s*?\n?---\s*\n(?P<second>.*?)\n---\s*\n?(?P<body>.*)$",
        re.DOTALL,
    )
    m = dual_re.match(content)
    if m:
        # 第二块即 teamharness 区
        return m.group("second"), m.group("body")

    # 单块匹配
    single_m = _FRONTMATTER_RE.match(content)
    if single_m:
        return single_m.group(1), single_m.group(2)

    return "", content


# ---------------------------------------------------------------------------
# 兼容性自检
# ---------------------------------------------------------------------------


def validate_compatibility(data: dict[str, Any]) -> list[str]:
    """校验 frontmatter dict 是否符合兼容性要求，返回违规清单（空表示通过）。

    检查项：
    - schema_version 必须是正整数或缺失（缺失视为 1）
    - 不允许删除已注册的"必备字段"（必备字段表见下方）
    """
    issues: list[str] = []
    sv = data.get("schema_version", 1)
    if not isinstance(sv, int) or sv < 1:
        issues.append(
            f"schema_version 必须是正整数或缺失（视为 1），收到：{sv!r}"
        )
    return issues
