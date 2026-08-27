"""SubTask 5.2 测试：调度索引表 task_routing + auto_bind 匹配。"""

from __future__ import annotations

import pytest

from server.binding.tests.conftest import insert_asset


class TestTaskRouting:
    """调度索引表 CRUD + auto_bind 匹配。"""

    def test_register_routing(self, binding_service):
        """注册调度索引。"""
        rid = binding_service.register_routing(
            task_type="pr-review",
            category="rule-backend",
            asset_id="r1",
        )
        assert rid.startswith("route-")
        routes = binding_service.list_routing(task_type="pr-review")
        assert len(routes) == 1
        assert routes[0].category == "rule-backend"
        assert routes[0].auto_bind is True

    def test_register_routing_dedup(self, binding_service):
        """同 (task_type, category, asset_id) → 更新而非新增。"""
        rid1 = binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        rid2 = binding_service.register_routing(
            task_type="pr-review",
            category="rule-backend",
            asset_id="r1",
            binding_type="fixed",
            priority="high",
        )
        assert rid1 == rid2
        routes = binding_service.list_routing()
        assert len(routes) == 1
        assert routes[0].binding_type == "fixed"
        assert routes[0].priority == "high"

    def test_list_routing_filter(self, binding_service):
        """按 task_type / category 过滤。"""
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        binding_service.register_routing(
            task_type="pr-review", category="skill-db", asset_id="s1"
        )
        binding_service.register_routing(
            task_type="build", category="rule-backend", asset_id="r2"
        )
        # 按 task_type
        assert len(binding_service.list_routing(task_type="pr-review")) == 2
        assert len(binding_service.list_routing(task_type="build")) == 1
        # 按 category
        assert len(binding_service.list_routing(category="rule-backend")) == 2
        assert len(binding_service.list_routing(category="skill-db")) == 1
        # 同时按
        assert (
            len(
                binding_service.list_routing(
                    task_type="pr-review", category="rule-backend"
                )
            )
            == 1
        )


class TestAutoBind:
    """auto_bind 按 task_type + category 自动匹配并绑定。"""

    def test_auto_bind_success(self, binding_service, database):
        """正常路径：匹配 task_routing → 创建 agent_binding。"""
        insert_asset(database, id="r1", category="rule-backend")
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        result = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert result.matched_count == 1
        assert result.bound_count == 1
        assert result.skipped_inactive == 0
        assert result.skipped_existing == 0
        assert len(result.binding_ids) == 1
        # 验证装配已创建
        bindings = binding_service.list_bindings("a1")
        assert len(bindings) == 1
        assert bindings[0].asset_id == "r1"
        assert bindings[0].binding_version == "0.0.1"

    def test_auto_bind_skips_inactive_asset(self, binding_service, database):
        """缺陷 3.2 双重过滤：asset_index.status != 'active' → 跳过。"""
        insert_asset(database, id="r1", status="deleted", category="rule-backend")
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        result = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert result.matched_count == 1
        assert result.bound_count == 0
        assert result.skipped_inactive == 1
        assert result.binding_ids == []

    def test_auto_bind_skips_superseded_asset(self, binding_service, database):
        """status='superseded' 也跳过（active 是唯一可绑定状态）。"""
        insert_asset(
            database, id="r1", status="superseded", category="rule-backend"
        )
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        result = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert result.bound_count == 0
        assert result.skipped_inactive == 1

    def test_auto_bind_skips_existing_active_binding(self, binding_service, database):
        """已有同版本活跃装配 → 跳过（避免重复绑定）。"""
        insert_asset(database, id="r1", version="0.0.1", category="rule-backend")
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        # 第一次 auto_bind
        r1 = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert r1.bound_count == 1
        # 第二次 auto_bind → 跳过
        r2 = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert r2.bound_count == 0
        assert r2.skipped_existing == 1

    def test_auto_bind_multiple_matches(self, binding_service, database):
        """一个 (task_type, category) 对应多个 asset → 全部绑定。"""
        insert_asset(database, id="r1", category="rule-backend")
        insert_asset(database, id="r2", category="rule-backend")
        insert_asset(database, id="r3", category="rule-backend")
        for aid in ("r1", "r2", "r3"):
            binding_service.register_routing(
                task_type="pr-review", category="rule-backend", asset_id=aid
            )
        result = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert result.matched_count == 3
        assert result.bound_count == 3
        assert len(result.binding_ids) == 3

    def test_auto_bind_no_match(self, binding_service):
        """无 task_routing 匹配 → matched_count=0。"""
        result = binding_service.auto_bind(
            agent_id="a1", task_type="unknown-task", category="rule-backend"
        )
        assert result.matched_count == 0
        assert result.bound_count == 0

    def test_auto_bind_excludes_auto_bind_false(self, binding_service, database):
        """auto_bind=false 的 routing 不参与自动装配。"""
        insert_asset(database, id="r1", category="rule-backend")
        binding_service.register_routing(
            task_type="pr-review",
            category="rule-backend",
            asset_id="r1",
            auto_bind=False,
        )
        result = binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        assert result.matched_count == 0  # auto_bind=False 不计入 matched

    def test_auto_bind_binds_asset_version(self, binding_service, database):
        """auto_bind 创建的 binding 的 binding_version = asset.version。"""
        insert_asset(database, id="r1", version="1.2.0", category="rule-backend")
        binding_service.register_routing(
            task_type="pr-review", category="rule-backend", asset_id="r1"
        )
        binding_service.auto_bind(
            agent_id="a1", task_type="pr-review", category="rule-backend"
        )
        b = binding_service.list_bindings("a1")[0]
        assert b.binding_version == "1.2.0"
