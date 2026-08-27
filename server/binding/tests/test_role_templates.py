"""SubTask 5.6 测试：角色模板 builder/reviewer/scout 默认装配。"""

from __future__ import annotations

from server.binding.templates import (
    DEFAULT_ROLE_TEMPLATES,
    RoleTemplateEntry,
    RoleTemplateRegistry,
)
from server.binding.tests.conftest import insert_asset


class TestRoleTemplateRegistry:
    """角色模板注册表。"""

    def test_default_templates_loaded(self):
        """内置模板含 builder/reviewer/scout 三角色。"""
        registry = RoleTemplateRegistry()
        roles = {e.role for e in registry.entries}
        assert roles == {"builder", "reviewer", "scout"}

    def test_for_role_filter(self):
        """for_role 返回指定角色的全部规则。"""
        registry = RoleTemplateRegistry()
        builder_entries = registry.for_role("builder")
        assert len(builder_entries) >= 2
        assert all(e.role == "builder" for e in builder_entries)

    def test_add_custom_template(self):
        """追加自定义模板规则。"""
        registry = RoleTemplateRegistry()
        custom = RoleTemplateEntry(
            role="custom-role",
            category_prefix="prompt-",
            binding_type="fixed",
            priority="high",
        )
        registry.add(custom)
        assert custom in registry.entries
        assert len(registry.for_role("custom-role")) == 1


class TestApplyRoleTemplate:
    """apply_role_template 为 Agent 继承默认装配。"""

    def test_builder_inherits_rule_and_tool(
        self, binding_service, database
    ):
        """builder 角色继承 rule-* + tool-* 装配（fixed）。"""
        # 准备资产 + 调度索引
        insert_asset(database, id="r1", category="rule-backend")
        insert_asset(database, id="r2", category="rule-api")
        insert_asset(database, id="t1", category="tool-lint")
        for aid, cat in (("r1", "rule-backend"), ("r2", "rule-api"), ("t1", "tool-lint")):
            binding_service.register_routing(
                task_type="any", category=cat, asset_id=aid
            )
        # 应用 builder 模板
        binding_ids = binding_service.apply_role_template(
            agent_id="a1", role="builder"
        )
        assert len(binding_ids) == 3
        # 验证装配
        bindings = binding_service.list_bindings("a1")
        assert len(bindings) == 3
        types = {b.binding_type for b in bindings}
        assert types == {"fixed"}  # builder 全部 fixed
        priorities = {b.priority for b in bindings}
        assert priorities == {"high", "normal"}  # rule- 高，tool- 普通

    def test_reviewer_inherits_on_demand(self, binding_service, database):
        """reviewer 角色装配为 on-demand。"""
        insert_asset(database, id="r1", category="rule-backend")
        insert_asset(database, id="m1", category="memory-api", type="memory")
        binding_service.register_routing(
            task_type="any", category="rule-backend", asset_id="r1"
        )
        binding_service.register_routing(
            task_type="any", category="memory-api", asset_id="m1"
        )
        binding_ids = binding_service.apply_role_template(
            agent_id="a2", role="reviewer"
        )
        assert len(binding_ids) == 2
        bindings = binding_service.list_bindings("a2")
        types = {b.binding_type for b in bindings}
        assert types == {"on-demand"}

    def test_scout_inherits_memory_and_skill(self, binding_service, database):
        """scout 角色继承 memory-* + skill-* 装配。"""
        insert_asset(database, id="m1", category="memory-api", type="memory")
        insert_asset(database, id="s1", category="skill-db", type="skill")
        binding_service.register_routing(
            task_type="any", category="memory-api", asset_id="m1"
        )
        binding_service.register_routing(
            task_type="any", category="skill-db", asset_id="s1"
        )
        binding_ids = binding_service.apply_role_template(
            agent_id="a3", role="scout"
        )
        assert len(binding_ids) == 2

    def test_role_template_skips_inactive_asset(self, binding_service, database):
        """资产 status != active → 跳过。"""
        insert_asset(database, id="r1", status="deleted", category="rule-backend")
        binding_service.register_routing(
            task_type="any", category="rule-backend", asset_id="r1"
        )
        binding_ids = binding_service.apply_role_template(
            agent_id="a1", role="builder"
        )
        assert binding_ids == []

    def test_role_template_skips_existing_binding(self, binding_service, database):
        """已存在同版本活跃装配 → 跳过。"""
        insert_asset(database, id="r1", category="rule-backend")
        binding_service.register_routing(
            task_type="any", category="rule-backend", asset_id="r1"
        )
        # 先手动创建一个
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        # 再应用模板 → 跳过
        binding_ids = binding_service.apply_role_template(
            agent_id="a1", role="builder"
        )
        assert binding_ids == []
        # 仍只有 1 个装配
        assert len(binding_service.list_bindings("a1")) == 1

    def test_role_template_unknown_role_returns_empty(self, binding_service):
        """未知角色 → 空列表。"""
        assert (
            binding_service.apply_role_template(agent_id="a1", role="unknown") == []
        )

    def test_role_template_respects_max_per_category(
        self, binding_service, database
    ):
        """max_per_category 限制：单 category 超过限制只装配前 N 个。"""
        # builder 模板 rule- 的 max_per_category=10，准备 12 个资产
        for i in range(12):
            insert_asset(database, id=f"r{i}", category="rule-backend")
            binding_service.register_routing(
                task_type="any", category="rule-backend", asset_id=f"r{i}"
            )
        binding_ids = binding_service.apply_role_template(
            agent_id="a1", role="builder"
        )
        # builder.rule- max_per_category=10
        assert len(binding_ids) == 10

    def test_role_template_with_explicit_categories(
        self, binding_service, database
    ):
        """available_categories 显式传入 → 只匹配列表内的 category。"""
        insert_asset(database, id="r1", category="rule-backend")
        insert_asset(database, id="r2", category="rule-api")
        binding_service.register_routing(
            task_type="any", category="rule-backend", asset_id="r1"
        )
        binding_service.register_routing(
            task_type="any", category="rule-api", asset_id="r2"
        )
        # 只装配 rule-backend
        binding_ids = binding_service.apply_role_template(
            agent_id="a1", role="builder", available_categories=["rule-backend"]
        )
        assert len(binding_ids) == 1
        assert binding_service.list_bindings("a1")[0].asset_id == "r1"
