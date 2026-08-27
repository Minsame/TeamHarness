"""SubTask 5.1 + 5.7 + 5.8 测试：agent_binding CRUD + 同事务级联 + 写时复制。

覆盖：
- 5.1 create / list / get / update_priority / delete（fixed/on-demand）
- 5.7 cascade_invalidate_asset + find_orphan_bindings
- 5.8 write_copy_on_asset_version_change + cleanup_superseded_bindings
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from server.binding.binding_service import SUPERSEDED_TTL_SECONDS, BindingService
from server.binding.tests.conftest import insert_asset


# ---------------------------------------------------------------------------
# SubTask 5.1: agent_binding CRUD
# ---------------------------------------------------------------------------


class TestBindingCRUD:
    """agent_binding 表 CRUD 测试。"""

    def test_create_fixed_binding(self, binding_service, database):
        """创建 fixed 类型装配。"""
        insert_asset(database, id="r1", category="rule-backend")
        bid = binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_type="fixed", priority="high"
        )
        assert bid.startswith("bind-")

        bindings = binding_service.list_bindings("a1")
        assert len(bindings) == 1
        b = bindings[0]
        assert b.agent_id == "a1"
        assert b.asset_id == "r1"
        assert b.binding_type == "fixed"
        assert b.priority == "high"
        assert b.enabled is True
        assert b.binding_version == "0.0.1"
        assert b.superseded_at is None

    def test_create_on_demand_binding_default(self, binding_service, database):
        """默认 on-demand + normal。"""
        insert_asset(database, id="r1")
        bid = binding_service.create_binding(agent_id="a1", asset_id="r1")
        assert bid.startswith("bind-")
        b = binding_service.list_bindings("a1")[0]
        assert b.binding_type == "on-demand"
        assert b.priority == "normal"

    def test_create_dedup_same_version(self, binding_service, database):
        """同 (agent_id, asset_id, binding_version) 已存在 → 更新而非新增。"""
        insert_asset(database, id="r1")
        bid1 = binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_type="fixed"
        )
        bid2 = binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_type="on-demand", priority="low"
        )
        assert bid1 == bid2
        b = binding_service.list_bindings("a1")[0]
        assert b.binding_type == "on-demand"
        assert b.priority == "low"

    def test_create_invalid_binding_type(self, binding_service, database):
        """非法 binding_type → 抛 ValueError。"""
        insert_asset(database, id="r1")
        with pytest.raises(ValueError, match="非法 binding_type"):
            binding_service.create_binding(
                agent_id="a1", asset_id="r1", binding_type="invalid"
            )

    def test_create_invalid_priority(self, binding_service, database):
        """非法 priority → 抛 ValueError。"""
        insert_asset(database, id="r1")
        with pytest.raises(ValueError, match="非法 priority"):
            binding_service.create_binding(
                agent_id="a1", asset_id="r1", priority="urgent"
            )

    def test_list_with_disabled(self, binding_service, database):
        """list 过滤 enabled=false（include_disabled 控制是否包含）。"""
        insert_asset(database, id="r1")
        insert_asset(database, id="r2")
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        binding_service.create_binding(agent_id="a1", asset_id="r2")
        # 失效 r1
        binding_service.cascade_invalidate_asset("r1")
        # 默认过滤 disabled
        active = binding_service.list_bindings("a1")
        assert len(active) == 1
        assert active[0].asset_id == "r2"
        # include_disabled=True 包含全部
        all_bindings = binding_service.list_bindings("a1", include_disabled=True)
        assert len(all_bindings) == 2

    def test_get_binding(self, binding_service, database):
        """查询单条装配。"""
        insert_asset(database, id="r1")
        bid = binding_service.create_binding(agent_id="a1", asset_id="r1")
        b = binding_service.get_binding(bid)
        assert b is not None
        assert b.id == bid
        # 不存在的 id
        assert binding_service.get_binding("nonexistent") is None

    def test_update_priority(self, binding_service, database):
        """更新优先级。"""
        insert_asset(database, id="r1")
        bid = binding_service.create_binding(agent_id="a1", asset_id="r1")
        assert binding_service.update_priority(bid, "high") is True
        b = binding_service.get_binding(bid)
        assert b.priority == "high"
        # 不存在的 binding_id
        assert binding_service.update_priority("nonexistent", "high") is False

    def test_delete_binding(self, binding_service, database):
        """物理删除装配。"""
        insert_asset(database, id="r1")
        bid = binding_service.create_binding(agent_id="a1", asset_id="r1")
        assert binding_service.delete_binding(bid) is True
        assert binding_service.list_bindings("a1") == []
        # 再删返回 False
        assert binding_service.delete_binding(bid) is False


# ---------------------------------------------------------------------------
# SubTask 5.7: 装配失效同事务级联更新
# ---------------------------------------------------------------------------


class TestCascadeInvalidate:
    """webhook 删除资产时同事务级联 enabled=false。"""

    def test_cascade_invalidate_single_asset(self, binding_service, database):
        """单资产失效：所有引用此资产的 enabled=true 装配 → enabled=false。"""
        insert_asset(database, id="r1")
        # 多个 agent 装配同一资产
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        binding_service.create_binding(agent_id="a2", asset_id="r1")
        binding_service.create_binding(agent_id="a3", asset_id="r1")
        # 失效 r1
        count = binding_service.cascade_invalidate_asset("r1")
        assert count == 3
        # 全部 enabled=false
        for agent_id in ("a1", "a2", "a3"):
            bindings = binding_service.list_bindings(
                agent_id, include_disabled=True
            )
            assert len(bindings) == 1
            assert bindings[0].enabled is False
            assert bindings[0].invalidated_at is not None

    def test_cascade_invalidate_idempotent(self, binding_service, database):
        """级联失效幂等：再次调用返回 0（已全部 enabled=false）。"""
        insert_asset(database, id="r1")
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        assert binding_service.cascade_invalidate_asset("r1") == 1
        assert binding_service.cascade_invalidate_asset("r1") == 0

    def test_cascade_invalidate_does_not_affect_other_assets(
        self, binding_service, database
    ):
        """失效 r1 不影响 r2 的装配。"""
        insert_asset(database, id="r1")
        insert_asset(database, id="r2")
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        binding_service.create_binding(agent_id="a1", asset_id="r2")
        binding_service.cascade_invalidate_asset("r1")
        active = binding_service.list_bindings("a1")
        assert len(active) == 1
        assert active[0].asset_id == "r2"

    def test_find_orphan_bindings(self, binding_service, database):
        """查找孤儿绑定：enabled=true 但资产非 active。"""
        insert_asset(database, id="r1", status="deleted")
        insert_asset(database, id="r2", status="active")
        # 直接造一个 enabled=true 但资产 deleted 的孤儿绑定
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        binding_service.create_binding(agent_id="a1", asset_id="r2")
        orphans = binding_service.find_orphan_bindings()
        # r1 status=deleted → 孤儿；r2 status=active → 正常
        assert len(orphans) == 1
        assert orphans[0].asset_id == "r1"

    def test_asset_index_delete_cascades_binding(
        self, binding_service, database, asset_index
    ):
        """验证 Agent 2 的 AssetIndex.delete 已内置级联（与 BindingService 互补）。"""
        # 写资产 + 装配
        from server.binding.tests.conftest import make_asset

        asset_index.upsert(make_asset(id="r1"), git_commit="c1")
        binding_service.create_binding(agent_id="a1", asset_id="r1")
        # AssetIndex.delete 应当同事务级联 enabled=false
        assert asset_index.delete("r1", git_commit="c2") is True
        # 验证装配 enabled=false
        bindings = binding_service.list_bindings("a1", include_disabled=True)
        assert len(bindings) == 1
        assert bindings[0].enabled is False
        assert bindings[0].invalidated_at is not None


# ---------------------------------------------------------------------------
# SubTask 5.8: 装配更新写时复制
# ---------------------------------------------------------------------------


class TestWriteCopyOnVersionChange:
    """资产版本变更时写时复制：新版本新行，旧版本 10 分钟清理。"""

    def test_write_copy_creates_new_row_and_supersedes_old(
        self, binding_service, database
    ):
        """版本变更：旧行 superseded_at + enabled=false，新行 enabled=true。"""
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        # 版本从 0.0.1 → 0.0.2
        new_id = binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        assert new_id.startswith("bind-")
        # 活跃装配（superseded_at IS NULL）应只有 0.0.2
        active = binding_service.get_active_bindings_for_asset("a1", "r1")
        assert len(active) == 1
        assert active[0].binding_version == "0.0.2"
        assert active[0].enabled is True
        # 全部装配（含 superseded）应有 2 行
        all_bindings = binding_service.list_bindings(
            "a1", include_disabled=True, include_superseded=True
        )
        assert len(all_bindings) == 2
        # 旧行 superseded_at 非 None，enabled=false
        old = [b for b in all_bindings if b.binding_version == "0.0.1"][0]
        assert old.superseded_at is not None
        assert old.enabled is False

    def test_write_copy_idempotent_same_version(self, binding_service, database):
        """同版本再次写时复制 → 幂等返回现有 active binding_id。"""
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        new_id_1 = binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        new_id_2 = binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        assert new_id_1 == new_id_2
        # 仍只有 1 个 active（0.0.2）+ 1 个 superseded（0.0.1）
        all_bindings = binding_service.list_bindings(
            "a1", include_disabled=True, include_superseded=True
        )
        assert len(all_bindings) == 2

    def test_write_copy_multi_version_chain(self, binding_service, database):
        """多版本链：0.0.1 → 0.0.2 → 0.0.3，活跃仅 0.0.3。"""
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.3"
        )
        active = binding_service.get_active_bindings_for_asset("a1", "r1")
        assert len(active) == 1
        assert active[0].binding_version == "0.0.3"
        # 全部 3 行（2 个 superseded + 1 个 active）
        all_bindings = binding_service.list_bindings(
            "a1", include_disabled=True, include_superseded=True
        )
        assert len(all_bindings) == 3

    def test_cleanup_superseded_bindings_within_ttl(self, binding_service, database):
        """TTL 内的 superseded 行不清理。"""
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        # 立即清理（默认 TTL=600s）→ 0 行（superseded_at 在 TTL 内）
        deleted = binding_service.cleanup_superseded_bindings()
        assert deleted == 0
        # 全部行仍存在
        all_bindings = binding_service.list_bindings(
            "a1", include_disabled=True, include_superseded=True
        )
        assert len(all_bindings) == 2

    def test_cleanup_superseded_bindings_after_ttl(self, binding_service, database):
        """超过 TTL 的 superseded 行被清理。"""
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        # 模拟时间流逝 11 分钟（超过 10 分钟 TTL）
        future = datetime.now(timezone.utc) + timedelta(seconds=SUPERSEDED_TTL_SECONDS + 60)
        deleted = binding_service.cleanup_superseded_bindings(now=future)
        assert deleted == 1
        # 仅剩活跃 0.0.2
        all_bindings = binding_service.list_bindings(
            "a1", include_disabled=True, include_superseded=True
        )
        assert len(all_bindings) == 1
        assert all_bindings[0].binding_version == "0.0.2"
        assert all_bindings[0].superseded_at is None

    def test_read_during_cleanup_no_orphan(self, binding_service, database):
        """竞态：清理时正好有读 — 读取仅看 superseded_at IS NULL 不会读到旧行。

        模拟：写时复制后立即读，应只看到新行。
        """
        insert_asset(database, id="r1", version="0.0.1")
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        # 写时复制（事务内：先 INSERT 新行再 UPDATE 旧行 superseded_at）
        new_id = binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        # 立即读活跃装配
        active = binding_service.get_active_bindings_for_asset("a1", "r1")
        assert len(active) == 1
        assert active[0].id == new_id
        assert active[0].binding_version == "0.0.2"

    def test_cleanup_does_not_touch_active_rows(self, binding_service, database):
        """清理只删 superseded_at IS NOT NULL 的行，不影响活跃行。"""
        insert_asset(database, id="r1", version="0.0.1")
        insert_asset(database, id="r2", version="0.0.1")
        # r1 写时复制（生成 superseded 旧行）
        binding_service.create_binding(
            agent_id="a1", asset_id="r1", binding_version="0.0.1"
        )
        binding_service.write_copy_on_asset_version_change(
            agent_id="a1", asset_id="r1", new_version="0.0.2"
        )
        # r2 单纯一个活跃装配
        binding_service.create_binding(agent_id="a1", asset_id="r2")
        future = datetime.now(timezone.utc) + timedelta(seconds=SUPERSEDED_TTL_SECONDS + 60)
        deleted = binding_service.cleanup_superseded_bindings(now=future)
        assert deleted == 1
        # r2 仍存在
        active_r2 = binding_service.get_active_bindings_for_asset("a1", "r2")
        assert len(active_r2) == 1
