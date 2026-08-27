"""BindingService — Agent 装配服务核心。

覆盖 SubTask：
- 5.1 agent_binding CRUD（fixed / on-demand）
- 5.2 调度索引表 task_routing + auto_bind 匹配
- 5.6 角色模板（builder / reviewer / scout 默认装配）
- 5.7 装配失效同事务级联更新（webhook 删除资产时 enabled=false）
- 5.8 装配更新写时复制（新版本新行，旧版本 10 分钟清理）

设计要点：
- 所有写操作通过 Database.session() 上下文管理器统一事务边界
- auto_bind 只装配 asset_index.status='active' 的资产（双重过滤，对齐缺陷 3.2）
- 写时复制：旧行 superseded_at=now + enabled=false，新行 binding_version=new
  读取时过滤 superseded_at IS NULL，旧行 10 分钟后清理
- 级联失效：cascade_invalidate_asset(asset_id) 在事务内 enabled=false + invalidated_at=now
  与 AssetIndex.delete 内置级联互补（供其他场景调用）
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Iterable

from sqlalchemy import and_, delete, select, update

from server.infra_db.db import Database
from server.infra_db.models import AgentBinding, AssetIndex as AssetIndexRow
from server.binding.models import TaskRouting
from server.binding.templates import RoleTemplateRegistry

logger = logging.getLogger(__name__)

# 写时复制旧版本清理 TTL（10 分钟，对齐技术方案）
SUPERSEDED_TTL_SECONDS = 600


@dataclass
class BindingVO:
    """装配值对象（API 返回用）。"""

    id: str
    agent_id: str
    agent_role: str
    asset_id: str
    binding_type: str  # fixed / on-demand
    priority: str  # high / normal / low
    enabled: bool
    binding_version: str
    superseded_at: datetime | None = None
    invalidated_at: datetime | None = None
    created_at: datetime | None = None

    @classmethod
    def from_row(cls, row: AgentBinding) -> "BindingVO":
        return cls(
            id=row.id,
            agent_id=row.agent_id,
            agent_role=row.agent_role,
            asset_id=row.asset_id,
            binding_type=row.binding_type,
            priority=row.priority,
            enabled=row.enabled,
            binding_version=row.binding_version,
            superseded_at=row.superseded_at,
            invalidated_at=row.invalidated_at,
            created_at=row.created_at,
        )


@dataclass
class AutoBindResult:
    """auto_bind 结果。"""

    agent_id: str
    task_type: str
    category: str
    matched_count: int = 0
    bound_count: int = 0
    skipped_inactive: int = 0
    skipped_existing: int = 0
    binding_ids: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class BindingService:
    """Agent 装配服务。

    用法：
        svc = BindingService(database, role_registry=RoleTemplateRegistry())
        binding_id = svc.create_binding(agent_id="a1", asset_id="r1", binding_type="fixed")
        result = svc.auto_bind(agent_id="a1", task_type="pr-review", category="rule-backend")
    """

    def __init__(
        self,
        database: Database,
        *,
        role_registry: RoleTemplateRegistry | None = None,
    ) -> None:
        self._db = database
        self._roles = role_registry or RoleTemplateRegistry()

    # ------------------------------------------------------------------
    # SubTask 5.1: agent_binding CRUD
    # ------------------------------------------------------------------

    def create_binding(
        self,
        *,
        agent_id: str,
        asset_id: str,
        binding_type: str = "on-demand",
        priority: str = "normal",
        agent_role: str = "",
        binding_version: str = "0.0.1",
    ) -> str:
        """创建装配。同一 (agent_id, asset_id, binding_version) 已存在则更新。

        binding_type：fixed（每次必加载）/ on-demand（按需召回）
        返回 binding_id。
        """
        if binding_type not in ("fixed", "on-demand"):
            raise ValueError(f"非法 binding_type: {binding_type}")
        if priority not in ("high", "normal", "low"):
            raise ValueError(f"非法 priority: {priority}")
        with self._db.session() as sess:
            existing = sess.scalars(
                select(AgentBinding).where(
                    AgentBinding.agent_id == agent_id,
                    AgentBinding.asset_id == asset_id,
                    AgentBinding.binding_version == binding_version,
                )
            ).first()
            if existing is not None:
                # 已存在 → 更新字段（保留 enabled 状态）
                existing.binding_type = binding_type
                existing.priority = priority
                existing.agent_role = agent_role or existing.agent_role
                return existing.id
            binding_id = f"bind-{uuid.uuid4().hex[:12]}"
            row = AgentBinding(
                id=binding_id,
                agent_id=agent_id,
                agent_role=agent_role,
                asset_id=asset_id,
                binding_type=binding_type,
                priority=priority,
                enabled=True,
                binding_version=binding_version,
                superseded_at=None,
                invalidated_at=None,
            )
            sess.add(row)
            return binding_id

    def list_bindings(
        self,
        agent_id: str,
        *,
        include_disabled: bool = False,
        include_superseded: bool = False,
    ) -> list[BindingVO]:
        """列出某 Agent 的装配。

        - include_disabled=False → 过滤 enabled=false
        - include_superseded=False → 过滤 superseded_at IS NOT NULL
        """
        with self._db.session() as sess:
            stmt = select(AgentBinding).where(AgentBinding.agent_id == agent_id)
            if not include_disabled:
                stmt = stmt.where(AgentBinding.enabled.is_(True))
            if not include_superseded:
                stmt = stmt.where(AgentBinding.superseded_at.is_(None))
            stmt = stmt.order_by(AgentBinding.created_at.desc())
            return [BindingVO.from_row(r) for r in sess.scalars(stmt)]

    def get_binding(self, binding_id: str) -> BindingVO | None:
        """查询单条装配。"""
        with self._db.session() as sess:
            row = sess.get(AgentBinding, binding_id)
            return BindingVO.from_row(row) if row else None

    def update_priority(self, binding_id: str, priority: str) -> bool:
        """更新装配优先级。"""
        if priority not in ("high", "normal", "low"):
            raise ValueError(f"非法 priority: {priority}")
        with self._db.session() as sess:
            result = sess.execute(
                update(AgentBinding)
                .where(AgentBinding.id == binding_id)
                .values(priority=priority)
            )
            return result.rowcount > 0

    def delete_binding(self, binding_id: str) -> bool:
        """物理删除装配（手动解绑）。"""
        with self._db.session() as sess:
            row = sess.get(AgentBinding, binding_id)
            if row is None:
                return False
            sess.delete(row)
            return True

    # ------------------------------------------------------------------
    # SubTask 5.2: 调度索引表 task_routing + auto_bind
    # ------------------------------------------------------------------

    def register_routing(
        self,
        *,
        task_type: str,
        category: str,
        asset_id: str,
        binding_type: str = "on-demand",
        priority: str = "normal",
        auto_bind: bool = True,
    ) -> str:
        """注册调度索引（task_type + category → asset_id）。"""
        with self._db.session() as sess:
            existing = sess.scalars(
                select(TaskRouting).where(
                    TaskRouting.task_type == task_type,
                    TaskRouting.category == category,
                    TaskRouting.asset_id == asset_id,
                )
            ).first()
            if existing is not None:
                existing.binding_type = binding_type
                existing.priority = priority
                existing.auto_bind = auto_bind
                return existing.id
            routing_id = f"route-{uuid.uuid4().hex[:12]}"
            sess.add(
                TaskRouting(
                    id=routing_id,
                    task_type=task_type,
                    category=category,
                    asset_id=asset_id,
                    binding_type=binding_type,
                    priority=priority,
                    auto_bind=auto_bind,
                )
            )
            return routing_id

    def list_routing(
        self, *, task_type: str | None = None, category: str | None = None
    ) -> list[TaskRouting]:
        """查询调度索引。"""
        with self._db.session() as sess:
            stmt = select(TaskRouting)
            if task_type:
                stmt = stmt.where(TaskRouting.task_type == task_type)
            if category:
                stmt = stmt.where(TaskRouting.category == category)
            return list(sess.scalars(stmt))

    def auto_bind(
        self,
        *,
        agent_id: str,
        task_type: str,
        category: str,
        agent_role: str = "",
    ) -> AutoBindResult:
        """按 task_type + category 自动匹配并绑定资产。

        流程：
        1. 查 task_routing WHERE task_type=? AND category=? AND auto_bind=true
        2. 对每个候选 asset_id 双重过滤：JOIN asset_index WHERE status='active'
        3. 已有同版本装配则跳过（避免重复）
        4. 创建 agent_binding
        """
        result = AutoBindResult(
            agent_id=agent_id, task_type=task_type, category=category, matched_count=0
        )
        with self._db.session() as sess:
            # 1. 查调度索引
            routings = list(
                sess.scalars(
                    select(TaskRouting).where(
                        TaskRouting.task_type == task_type,
                        TaskRouting.category == category,
                        TaskRouting.auto_bind.is_(True),
                    )
                )
            )
            result.matched_count = len(routings)
            for route in routings:
                # 2. 双重过滤：资产必须 active（缺陷 3.2）
                asset_row = sess.get(AssetIndexRow, route.asset_id)
                if asset_row is None or asset_row.status != "active":
                    result.skipped_inactive += 1
                    continue
                # 3. 已有同版本活跃装配 → 跳过
                existing = sess.scalars(
                    select(AgentBinding).where(
                        AgentBinding.agent_id == agent_id,
                        AgentBinding.asset_id == route.asset_id,
                        AgentBinding.binding_version == asset_row.version,
                        AgentBinding.superseded_at.is_(None),
                    )
                ).first()
                if existing is not None:
                    result.skipped_existing += 1
                    continue
                # 4. 创建装配
                binding_id = f"bind-{uuid.uuid4().hex[:12]}"
                sess.add(
                    AgentBinding(
                        id=binding_id,
                        agent_id=agent_id,
                        agent_role=agent_role,
                        asset_id=route.asset_id,
                        binding_type=route.binding_type,
                        priority=route.priority,
                        enabled=True,
                        binding_version=asset_row.version,
                        superseded_at=None,
                        invalidated_at=None,
                    )
                )
                result.binding_ids.append(binding_id)
                result.bound_count += 1
        return result

    # ------------------------------------------------------------------
    # SubTask 5.6: 角色模板默认装配
    # ------------------------------------------------------------------

    def apply_role_template(
        self,
        *,
        agent_id: str,
        role: str,
        available_categories: Iterable[str] | None = None,
    ) -> list[str]:
        """为 Agent 应用角色模板（继承默认装配）。

        - 模板按 category_prefix 匹配 available_categories（不传则全 task_routing）
        - 命中的资产在 task_routing 中按 category 找 → 创建 agent_binding
        - 受 max_per_category 限制
        返回创建的 binding_id 列表。
        """
        entries = self._roles.for_role(role)
        if not entries:
            return []
        binding_ids: list[str] = []
        with self._db.session() as sess:
            for entry in entries:
                # 找匹配 category（按前缀）
                if available_categories is not None:
                    matched_categories = [
                        c for c in available_categories if c.startswith(entry.category_prefix)
                    ]
                else:
                    # 从 task_routing 查所有匹配前缀的 category
                    all_routes = list(
                        sess.scalars(
                            select(TaskRouting).where(
                                TaskRouting.auto_bind.is_(True)
                            )
                        )
                    )
                    matched_categories = list(
                        {r.category for r in all_routes if r.category.startswith(entry.category_prefix)}
                    )
                for category in matched_categories:
                    # 找该 category 下 task_routing 资产，按 max_per_category 截断
                    routes_in_cat = list(
                        sess.scalars(
                            select(TaskRouting).where(
                                TaskRouting.category == category,
                                TaskRouting.auto_bind.is_(True),
                            )
                        )
                    )
                    for route in routes_in_cat[: entry.max_per_category]:
                        # 双重过滤 active
                        asset_row = sess.get(AssetIndexRow, route.asset_id)
                        if asset_row is None or asset_row.status != "active":
                            continue
                        # 已存在同版本活跃装配 → 跳过
                        existing = sess.scalars(
                            select(AgentBinding).where(
                                AgentBinding.agent_id == agent_id,
                                AgentBinding.asset_id == route.asset_id,
                                AgentBinding.binding_version == asset_row.version,
                                AgentBinding.superseded_at.is_(None),
                            )
                        ).first()
                        if existing is not None:
                            continue
                        binding_id = f"bind-{uuid.uuid4().hex[:12]}"
                        sess.add(
                            AgentBinding(
                                id=binding_id,
                                agent_id=agent_id,
                                agent_role=role,
                                asset_id=route.asset_id,
                                binding_type=entry.binding_type,
                                priority=entry.priority,
                                enabled=True,
                                binding_version=asset_row.version,
                                superseded_at=None,
                                invalidated_at=None,
                            )
                        )
                        binding_ids.append(binding_id)
        return binding_ids

    # ------------------------------------------------------------------
    # SubTask 5.7: 装配失效同事务级联更新
    # ------------------------------------------------------------------

    def cascade_invalidate_asset(self, asset_id: str) -> int:
        """资产失效时同事务级联更新所有 agent_binding.enabled=false。

        供场景：
        - webhook 删除资产时调用（AssetIndex.delete 已内置级联，本方法为补丁入口）
        - 资产 superseded 但未删除时（手动调用）
        - 测试与治理对账（孤儿绑定检测）

        返回失效的装配数。
        """
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            result = sess.execute(
                update(AgentBinding)
                .where(AgentBinding.asset_id == asset_id)
                .where(AgentBinding.enabled.is_(True))
                .values(enabled=False, invalidated_at=now)
            )
            return result.rowcount

    def find_orphan_bindings(self) -> list[BindingVO]:
        """查找孤儿绑定：enabled=true 但对应 asset_index.status != 'active'。

        治理对账用：webhook 级联未生效时（如事务回滚后）的兜底检测。
        """
        with self._db.session() as sess:
            # JOIN asset_index，找 enabled=true 但资产非 active
            stmt = (
                select(AgentBinding)
                .join(AssetIndexRow, AgentBinding.asset_id == AssetIndexRow.id)
                .where(AgentBinding.enabled.is_(True))
                .where(AssetIndexRow.status != "active")
            )
            return [BindingVO.from_row(r) for r in sess.scalars(stmt)]

    # ------------------------------------------------------------------
    # SubTask 5.8: 装配更新写时复制（新版本新行，旧版本 10 分钟清理）
    # ------------------------------------------------------------------

    def write_copy_on_asset_version_change(
        self,
        *,
        agent_id: str,
        asset_id: str,
        new_version: str,
        binding_type: str = "on-demand",
        priority: str = "normal",
        agent_role: str = "",
    ) -> str:
        """资产版本变更时写时复制装配。

        流程（单事务，避免竞态）：
        1. 查同 (agent_id, asset_id) 全部活跃绑定（superseded_at IS NULL）
        2. 若已有 new_version 的活跃绑定 → 直接返回（幂等）
        3. INSERT 新行（binding_version=new_version, enabled=true, superseded_at=NULL）
        4. UPDATE 旧行：superseded_at=now, enabled=false（清空 superseded_at 触发取代标记）
        5. 旧行由 cleanup_superseded_bindings 在 10 分钟后清理

        返回新绑定的 binding_id。
        """
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            # 1. 查活跃绑定
            active_rows = list(
                sess.scalars(
                    select(AgentBinding).where(
                        AgentBinding.agent_id == agent_id,
                        AgentBinding.asset_id == asset_id,
                        AgentBinding.superseded_at.is_(None),
                    )
                )
            )
            # 2. 幂等：已有 new_version 活跃绑定
            for row in active_rows:
                if row.binding_version == new_version and row.enabled:
                    return row.id
            # 3. 新建行
            new_binding_id = f"bind-{uuid.uuid4().hex[:12]}"
            sess.add(
                AgentBinding(
                    id=new_binding_id,
                    agent_id=agent_id,
                    agent_role=agent_role,
                    asset_id=asset_id,
                    binding_type=binding_type,
                    priority=priority,
                    enabled=True,
                    binding_version=new_version,
                    superseded_at=None,
                    invalidated_at=None,
                )
            )
            # 4. 标记旧行 superseded（在 flush 后再 UPDATE，确保新行先入库）
            sess.flush()
            for row in active_rows:
                row.superseded_at = now
                row.enabled = False
            return new_binding_id

    def cleanup_superseded_bindings(
        self, *, now: datetime | None = None, ttl_seconds: int = SUPERSEDED_TTL_SECONDS
    ) -> int:
        """清理 superseded_at 早于 TTL 的旧版本装配行。

        竞态保护：
        - 读路径过滤 superseded_at IS NULL，旧行被取代后不再可见
        - 清理只 DELETE 已 superseded_at 的行，不影响活跃绑定
        - 即使清理时正好有读，读到的是 superseded_at IS NOT NULL 的旧行（已被过滤）
        """
        cutoff = (now or datetime.now(timezone.utc)) - timedelta(seconds=ttl_seconds)
        with self._db.session() as sess:
            result = sess.execute(
                delete(AgentBinding).where(
                    AgentBinding.superseded_at.is_not(None),
                    AgentBinding.superseded_at < cutoff,
                )
            )
            return result.rowcount

    def get_active_bindings_for_asset(
        self, agent_id: str, asset_id: str
    ) -> list[BindingVO]:
        """查询某 (agent_id, asset_id) 的活跃装配（superseded_at IS NULL）。"""
        with self._db.session() as sess:
            stmt = (
                select(AgentBinding)
                .where(
                    AgentBinding.agent_id == agent_id,
                    AgentBinding.asset_id == asset_id,
                    AgentBinding.superseded_at.is_(None),
                )
                .order_by(AgentBinding.binding_version.desc())
            )
            return [BindingVO.from_row(r) for r in sess.scalars(stmt)]


__all__ = [
    "AutoBindResult",
    "BindingService",
    "BindingVO",
    "SUPERSEDED_TTL_SECONDS",
]
