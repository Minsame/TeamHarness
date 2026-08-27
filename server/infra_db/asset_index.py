"""AssetIndex — 资产索引服务（对外契约 API）。

对应 SubTask 2.4（outbox 模式） + 对外契约：
- upsert(asset) → 写 asset_index + 同事务写 embedding_task_queue（pending）
- delete(asset_id) → 标记 status=deleted + 同事务写 embedding_task_queue（delete）
- query(filter) → 查询 asset_index
- get_status(asset_id) → 返回 (status, embedding_id, indexed_at)

outbox 模式核心（缺陷 1.2 双存储原子性）：
- asset_index + embedding_task_queue 在同一 PG 事务中写入
- 事务提交后异步 worker 消费队列写向量库
- 事务回滚 → 两表一起回滚，向量库不会被写入（无孤儿）
- worker 已写向量库但事务回滚的兜底：孤儿补偿（见 outbox.py）

双写过渡（SubTask 2.7）：
- shadow_version 非空时，upsert 写入两条 embedding_task_queue（active + shadow）
- 切换 active 后，旧版本任务进入 pending 由 worker 删除
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import and_, or_, select, update
from sqlalchemy.orm import Session

from server.common.models import Asset as AssetVO
from server.infra_db.db import Database
from server.infra_db.models import (
    AgentBinding,
    AssetIndex as AssetIndexRow,
    EmbeddingTaskQueue,
)

logger = logging.getLogger(__name__)


@dataclass
class AssetFilter:
    """资产查询过滤器。"""

    types: list[str] = field(default_factory=list)
    module_paths: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    scopes: list[str] = field(default_factory=list)
    owners: list[str] = field(default_factory=list)
    statuses: list[str] = field(default_factory=lambda: ["active"])
    tags_any: list[str] = field(default_factory=list)
    # module_path 前缀匹配（递归召回子模块）
    module_path_prefix: str | None = None

    def is_empty(self) -> bool:
        return not (
            self.types
            or self.module_paths
            or self.categories
            or self.scopes
            or self.owners
            or self.tags_any
            or self.module_path_prefix
        ) and self.statuses == ["active"]


@dataclass
class AssetStatus:
    """资产状态查询结果。"""

    asset_id: str
    status: str
    embedding_id: str | None
    active_embedding_version: str | None
    indexed_at: datetime
    updated_at: datetime
    git_path: str
    git_commit: str
    module_path: str = ""


class AssetIndex:
    """资产索引服务（对外契约 API）。

    所有写操作通过 outbox 模式保证双存储原子性。
    依赖 Database 提供同步会话，内部用 sessionmaker 显式开启事务。
    """

    def __init__(
        self,
        database: Database,
        *,
        active_embedding_version: str = "v1",
        shadow_embedding_version: str = "",
    ) -> None:
        self._db = database
        self._active_version = active_embedding_version
        self._shadow_version = shadow_embedding_version

    def set_embedding_versions(
        self, *, active: str, shadow: str = ""
    ) -> None:
        """更新 embedding 版本配置（双写过渡期切换）。"""
        self._active_version = active
        self._shadow_version = shadow

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def upsert(
        self,
        asset: AssetVO,
        *,
        git_commit: str,
        content_snapshot: str = "",
        enqueue_embedding: bool = True,
    ) -> str:
        """写入或更新资产索引，同事务投递 embedding 任务到 outbox 队列。

        - 已存在（按 id）→ 更新字段并重新投递 embedding 任务
        - 不存在 → 插入新行
        - enqueue_embedding=False → 仅写索引不投递任务（用于对账任务避免重复投递）

        返回 asset_id。
        """
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset.id)
            now = datetime.now(timezone.utc)
            tags_json = json.dumps(asset.tags, ensure_ascii=False)
            related_json = json.dumps(asset.related_to, ensure_ascii=False)
            if row is None:
                row = AssetIndexRow(
                    id=asset.id,
                    type=asset.type.value if hasattr(asset.type, "value") else str(asset.type),
                    owner=asset.owner,
                    scope=asset.scope.value if hasattr(asset.scope, "value") else str(asset.scope),
                    content_hash=asset.content_hash,
                    embedding_id=None,
                    active_embedding_version=self._active_version,
                    version=asset.version,
                    tags=tags_json,
                    related_to=related_json,
                    git_path=asset.content_file_ref or "",
                    git_commit=git_commit,
                    module_path=asset.module_path or "",
                    category=asset.category,
                    status="active",
                    schema_version=asset.schema_version,
                    created_at=now,
                    updated_at=now,
                    indexed_at=now,
                    content_snapshot=content_snapshot or asset.content,
                )
                sess.add(row)
            else:
                row.type = asset.type.value if hasattr(asset.type, "value") else str(asset.type)
                row.owner = asset.owner
                row.scope = asset.scope.value if hasattr(asset.scope, "value") else str(asset.scope)
                row.content_hash = asset.content_hash
                # embedding_id 保留原值；若版本切换则置 None 触发重算
                if (
                    row.active_embedding_version
                    and row.active_embedding_version != self._active_version
                ):
                    row.embedding_id = None
                row.active_embedding_version = self._active_version
                row.version = asset.version
                row.tags = tags_json
                row.related_to = related_json
                row.git_path = asset.content_file_ref or row.git_path
                row.git_commit = git_commit
                row.module_path = asset.module_path or ""
                row.category = asset.category
                row.status = "active"
                row.schema_version = asset.schema_version
                row.updated_at = now
                row.indexed_at = now
                row.content_snapshot = content_snapshot or asset.content

            if enqueue_embedding:
                # 投递 active 版本任务
                self._enqueue_embedding_task(
                    sess,
                    asset_id=asset.id,
                    task_type="upsert",
                    model_version=self._active_version,
                )
                # 双写过渡：shadow 版本任务
                if self._shadow_version:
                    self._enqueue_embedding_task(
                        sess,
                        asset_id=asset.id,
                        task_type="upsert",
                        model_version=self._shadow_version,
                    )
        return asset.id

    def delete(
        self,
        asset_id: str,
        *,
        git_commit: str = "",
        soft_delete: bool = True,
        enqueue_embedding: bool = True,
    ) -> bool:
        """删除资产索引。

        - soft_delete=True → 标记 status=deleted，保留行（recall/read 410 Gone 用）
        - soft_delete=False → 物理删除行
        - enqueue_embedding=True → 同事务投递 delete 任务到 outbox，worker 清向量库
        返回是否实际删除（资产存在）。
        """
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset_id)
            if row is None:
                return False
            if soft_delete:
                row.status = "deleted"
                row.updated_at = datetime.now(timezone.utc)
                # 同事务级联：agent_binding.enabled=false（缺陷 3.2 装配失效双重过滤）
                sess.execute(
                    update(AgentBinding)
                    .where(AgentBinding.asset_id == asset_id)
                    .where(AgentBinding.enabled.is_(True))
                    .values(enabled=False, invalidated_at=datetime.now(timezone.utc))
                )
            else:
                sess.delete(row)
            if enqueue_embedding:
                # 投递 delete 任务：清理所有版本（active + shadow）的向量
                self._enqueue_embedding_task(
                    sess,
                    asset_id=asset_id,
                    task_type="delete",
                    model_version=self._active_version,
                )
                if self._shadow_version:
                    self._enqueue_embedding_task(
                        sess,
                        asset_id=asset_id,
                        task_type="delete",
                        model_version=self._shadow_version,
                    )
        return True

    def query(self, filter: AssetFilter, *, limit: int = 100) -> list[AssetIndexRow]:
        """按过滤器查询资产索引。"""
        with self._db.session() as sess:
            stmt = select(AssetIndexRow)
            if filter.types:
                stmt = stmt.where(AssetIndexRow.type.in_(filter.types))
            if filter.module_paths:
                stmt = stmt.where(AssetIndexRow.module_path.in_(filter.module_paths))
            if filter.module_path_prefix:
                # 前缀匹配（递归召回子模块）：module_path LIKE 'prefix%'
                stmt = stmt.where(
                    AssetIndexRow.module_path.like(f"{filter.module_path_prefix}%")
                )
            if filter.categories:
                stmt = stmt.where(AssetIndexRow.category.in_(filter.categories))
            if filter.scopes:
                stmt = stmt.where(AssetIndexRow.scope.in_(filter.scopes))
            if filter.owners:
                stmt = stmt.where(AssetIndexRow.owner.in_(filter.owners))
            if filter.statuses:
                stmt = stmt.where(AssetIndexRow.status.in_(filter.statuses))
            if filter.tags_any:
                # tags JSON 数组，任一命中（OR 语义）
                clauses = [
                    AssetIndexRow.tags.like(f'%"{t}"%') for t in filter.tags_any
                ]
                stmt = stmt.where(or_(*clauses))
            stmt = stmt.limit(limit)
            return list(sess.scalars(stmt))

    def get_status(self, asset_id: str) -> AssetStatus | None:
        """查询资产状态（status / embedding_id / indexed_at 等）。"""
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset_id)
            if row is None:
                return None
            return AssetStatus(
                asset_id=row.id,
                status=row.status,
                embedding_id=row.embedding_id,
                active_embedding_version=row.active_embedding_version,
                indexed_at=row.indexed_at,
                updated_at=row.updated_at,
                git_path=row.git_path,
                git_commit=row.git_commit,
                module_path=row.module_path,
            )

    def get_by_id(self, asset_id: str) -> AssetIndexRow | None:
        """直接读取 ORM 行（内部调用）。"""
        with self._db.session() as sess:
            return sess.get(AssetIndexRow, asset_id)

    def list_null_embedding(
        self, *, stale_seconds: int = 3600, limit: int = 500
    ) -> list[AssetIndexRow]:
        """列出 embedding_id IS NULL 的资产（对账任务用，缺陷 1.1）。

        stale_seconds：只返回 indexed_at 早于 N 秒前的资产（避免与刚投递的任务争抢）。
        """
        from datetime import timedelta

        with self._db.session() as sess:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_seconds)
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.embedding_id.is_(None))
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.indexed_at < cutoff)
                .order_by(AssetIndexRow.indexed_at.asc())
                .limit(limit)
            )
            return list(sess.scalars(stmt))

    # ------------------------------------------------------------------
    # outbox 投递（同事务）
    # ------------------------------------------------------------------

    def _enqueue_embedding_task(
        self,
        sess: Session,
        *,
        asset_id: str,
        task_type: str,
        model_version: str,
    ) -> EmbeddingTaskQueue:
        """在当前 session 内投递 embedding 任务到 outbox 队列。

        与 asset_index 写入共用同一 session，保证同事务提交或回滚。
        """
        task = EmbeddingTaskQueue(
            asset_id=asset_id,
            task_type=task_type,
            model_version=model_version,
            status="pending",
            retry_count=0,
            max_retries=3,
        )
        sess.add(task)
        sess.flush()  # 拿到自增 id，但仍在事务内
        return task

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    def reindex_module(
        self, module_path: str, *, git_commit: str
    ) -> int:
        """重建某模块下全部资产的 embedding 任务（治理用）。

        不重写 asset_index，只投递 reindex 任务到 outbox。
        """
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.module_path == module_path)
                .where(AssetIndexRow.status == "active")
            )
            count = 0
            for row in sess.scalars(stmt):
                self._enqueue_embedding_task(
                    sess,
                    asset_id=row.id,
                    task_type="reindex",
                    model_version=self._active_version,
                )
                count += 1
            return count


__all__ = [
    "AssetFilter",
    "AssetIndex",
    "AssetStatus",
]
