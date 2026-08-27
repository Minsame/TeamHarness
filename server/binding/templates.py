"""角色模板：builder / reviewer / scout 默认装配规则。

对应 SubTask 5.6：新 Agent 按角色继承默认装配。
- builder：构建任务，默认装配 rule-* + tool-* 类资产（fixed）
- reviewer：审查任务，默认装配 rule-* + memory-* 类资产（on-demand）
- scout：探索任务，默认装配 memory-* + skill-* 类资产（on-demand）

模板按 (role, category_pattern, binding_type, priority) 描述，
新 Agent 创建时按 role 查模板 → 按 category 在 task_routing 中匹配 → 生成 agent_binding。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class RoleTemplateEntry:
    """角色模板单条规则。"""

    role: str
    # category 前缀匹配（type- 前缀），如 "rule-" 匹配所有 rule-* category
    category_prefix: str
    binding_type: str = "on-demand"  # fixed / on-demand
    priority: str = "normal"  # high / normal / low
    # 限制每个 category 装配的资产数（避免单个 type 装太多）
    max_per_category: int = 5


# 内置角色模板（按技术方案 8.1 角色定义）
DEFAULT_ROLE_TEMPLATES: list[RoleTemplateEntry] = [
    # builder：构建任务，强依赖 rule + tool
    RoleTemplateEntry(
        role="builder",
        category_prefix="rule-",
        binding_type="fixed",
        priority="high",
        max_per_category=10,
    ),
    RoleTemplateEntry(
        role="builder",
        category_prefix="tool-",
        binding_type="fixed",
        priority="normal",
        max_per_category=5,
    ),
    # reviewer：审查任务，依赖 rule + memory（经验）
    RoleTemplateEntry(
        role="reviewer",
        category_prefix="rule-",
        binding_type="on-demand",
        priority="high",
        max_per_category=10,
    ),
    RoleTemplateEntry(
        role="reviewer",
        category_prefix="memory-",
        binding_type="on-demand",
        priority="normal",
        max_per_category=5,
    ),
    # scout：探索任务，依赖 memory + skill
    RoleTemplateEntry(
        role="scout",
        category_prefix="memory-",
        binding_type="on-demand",
        priority="normal",
        max_per_category=5,
    ),
    RoleTemplateEntry(
        role="scout",
        category_prefix="skill-",
        binding_type="on-demand",
        priority="normal",
        max_per_category=5,
    ),
]


@dataclass
class RoleTemplateRegistry:
    """角色模板注册表。"""

    entries: list[RoleTemplateEntry] = field(
        default_factory=lambda: list(DEFAULT_ROLE_TEMPLATES)
    )

    def for_role(self, role: str) -> list[RoleTemplateEntry]:
        """返回指定角色的全部模板规则。"""
        return [e for e in self.entries if e.role == role]

    def add(self, entry: RoleTemplateEntry) -> None:
        """追加自定义模板规则。"""
        self.entries.append(entry)


__all__ = [
    "DEFAULT_ROLE_TEMPLATES",
    "RoleTemplateEntry",
    "RoleTemplateRegistry",
]
