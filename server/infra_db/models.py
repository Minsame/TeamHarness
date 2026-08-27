"""SQLAlchemy ORM 模型 — DB 派生索引层全部表。

对应技术方案第 5 节"数据模型" + Agent 2 SubTask 2.1：
- asset_index：资产索引（含 module_path / category / status / git 追溯）
- agent_binding：Agent 装配表（fixed/on-demand）
- module_stats：模块统计镜像（从 INDEX.md counts 派生，治理看板数据源）
- recall_log：召回日志（按月分区，二级提炼晋升门禁数据源）
- embedding_task_queue：outbox 队列（与 asset_index 同事务写入，异步 worker 消费）
- index_sync_state：DB 索引同步状态（追踪与 git 的同步水位）
- adoption_event：采纳事件（客户端上报，prompt 采纳率数据源）
- asset_recall_stats：物化视图（每资产召回次数聚合，召回率统计）

设计原则：
- 全部为派生索引层，可从 git 重建
- 主键统一用 UUID 字符串（与 frontmatter id 对齐）
- 时间字段统一带时区（PG TIMESTAMPTZ / SQLite TEXT）
- status 字段：active / superseded / deleted（召回双重过滤依赖，见缺陷 3.2）
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    """统一 UTC 当前时间。"""
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    """ORM 基类。"""

    pass


class AssetIndex(Base):
    """资产索引（DB 派生索引层核心表）。

    从 git frontmatter + 文件内容派生，可重建。
    - module_path：组织层级路径（如 modules/backend），根级为空字符串
    - category：功能分类（受控词汇表 <type>-<module>），与 module_path 正交
    - status：active / superseded / deleted（召回双重过滤依赖）
    - embedding_id：指向向量库的引用，初始为 NULL（outbox 异步回写）
    """

    __tablename__ = "asset_index"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    owner: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    scope: Mapped[str] = mapped_column(String(16), nullable=False, default="team")
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    # embedding_id 初始为 NULL，由 outbox worker 异步回写
    embedding_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    active_embedding_version: Mapped[str | None] = mapped_column(
        String(32), nullable=True
    )
    version: Mapped[str] = mapped_column(String(32), nullable=False, default="0.0.1")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON 数组
    related_to: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON 数组
    git_path: Mapped[str] = mapped_column(String(512), nullable=False)
    git_commit: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module_path: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    category: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    schema_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    indexed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # 内容快照（用于召回 BM25 + 二级提炼聚类，避免每次回 git）
    content_snapshot: Mapped[str] = mapped_column(Text, nullable=False, default="")

    __table_args__ = (
        Index("idx_asset_module", "module_path"),
        Index("idx_asset_category", "category"),
        Index("idx_asset_status_type", "status", "type"),
        Index("idx_asset_owner_scope", "owner", "scope"),
        UniqueConstraint("id", name="uq_asset_index_id"),
    )


class AgentBinding(Base):
    """Agent 装配表（哪个 Agent 装备哪些资产）。

    binding_type：fixed（每次必加载）/ on-demand（按需召回）
    enabled=false 时表示资产已删除但绑定未清理（webhook 同事务级联，见缺陷 3.2）
    invalidated_at：失效时间戳（治理告警用）

    SubTask 5.8 写时复制扩展（Agent 5 装配服务）：
    - binding_version：绑定时资产版本号（对应 asset_index.version）
    - superseded_at：被新版本取代的时间戳（非空表示此行已被取代，10 分钟清理）
    - 唯一约束改为 (agent_id, asset_id, binding_version)：同一资产不同版本可多行共存
    - 读取时优先 enabled=true 且 superseded_at IS NULL 的"活跃"绑定
    """

    __tablename__ = "agent_binding"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_role: Mapped[str] = mapped_column(String(32), nullable=False, default="")
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("asset_index.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    binding_type: Mapped[str] = mapped_column(String(16), nullable=False, default="on-demand")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # 写时复制：绑定的资产版本号（对应 asset_index.version），缺省 "0.0.1"
    binding_version: Mapped[str] = mapped_column(
        String(32), nullable=False, default="0.0.1"
    )
    # 被新版本取代的时间戳（清理用，10 分钟 TTL）
    superseded_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        # 写时复制：同一资产不同版本可多行共存，故唯一键含 binding_version
        UniqueConstraint(
            "agent_id", "asset_id", "binding_version", name="uq_agent_asset_version"
        ),
        Index("idx_binding_agent_enabled", "agent_id", "enabled"),
        Index("idx_binding_type", "binding_type"),
        # 写时复制活跃绑定查询索引（agent_id + superseded_at IS NULL）
        Index("idx_binding_agent_active", "agent_id", "superseded_at"),
    )


class ModuleStats(Base):
    """模块统计镜像（从 INDEX.md counts 派生，治理看板数据源）。

    对应技术方案 3.1.4：webhook 同步时从 git INDEX.md 读取 counts 写入本表。
    注意：缺陷 8.1 修复后，治理看板用 asset_index 实时派生为准，
    本表保留作为 INDEX.md 声明值的镜像，用于 counts 一致性校验。
    """

    __tablename__ = "module_stats"

    module_path: Mapped[str] = mapped_column(String(256), primary_key=True)
    declared_asset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    declared_submodule_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_asset_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    actual_submodule_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    # counts 不一致标记（治理看板告警源）
    counts_consistent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    last_synced_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    last_synced_commit: Mapped[str] = mapped_column(String(64), nullable=False, default="")


class RecallLog(Base):
    """召回日志（按月分区）。

    分区表在 PG 用 PARTITION BY RANGE (recalled_at)，
    SQLite 不支持分区，回退为普通表（测试可用）。
    每月一个分区，6 个月 TTL（drop 老分区）。
    二级提炼晋升门禁"被召回次数"数据源。
    """

    __tablename__ = "recall_log"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    recalled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    module_path: Mapped[str] = mapped_column(String(256), nullable=False, default="", index=True)
    query: Mapped[str] = mapped_column(Text, nullable=False, default="")
    relevance_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    # trace_id 用于 OpenTelemetry 链路追踪
    trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)

    __table_args__ = (
        Index("idx_recall_log_asset_time", "asset_id", "recalled_at"),
        Index("idx_recall_log_module_time", "module_path", "recalled_at"),
    )


class EmbeddingTaskQueue(Base):
    """outbox 队列：与 asset_index 同事务写入，异步 worker 消费写向量库。

    对应缺陷 1.2 双存储原子性核心：
    - asset_index + embedding_task_queue 在同一 PG 事务中写入
    - 异步 worker 从队列取出任务，调用向量库 upsert，成功后回写 embedding_id
    - 若 worker 失败 → asset_index 已提交但 embedding_id 为 NULL，由对账任务补偿
    - 若 PG 事务回滚 → 队列任务一起回滚，向量库不会被写入（无孤儿）

    状态机：
    - pending：待处理
    - in_progress：worker 已领走（带 lease_at 防止丢失）
    - done：向量库已写，embedding_id 已回填
    - failed：重试超限，需人工介入
    - orphan_compensated：补偿删除（asset_index 已回滚但 worker 已写向量库的兜底）
    """

    __tablename__ = "embedding_task_queue"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 任务类型：upsert（写）/ delete（删）/ reindex（重建索引）
    task_type: Mapped[str] = mapped_column(String(16), nullable=False, default="upsert")
    # 双写过渡期：model_version 标记写哪个向量库
    model_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    status: Mapped[str] = mapped_column(String(24), nullable=False, default="pending", index=True)
    # worker 领取后写 lease_at，超时未完成可被其他 worker 重新领取
    leased_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # 完成后回填 embedding_id 到 asset_index
    embedding_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_retries: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 与 asset_index 同事务创建（用于孤儿补偿校验）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        Index("idx_emb_queue_status_created", "status", "created_at"),
        Index("idx_emb_queue_asset", "asset_id"),
    )


class IndexSyncState(Base):
    """DB 索引同步状态（追踪与 git 的同步水位）。

    单行表（singleton），id 固定为 'singleton'。
    reconciliation cron 比对 last_synced_commit 与 git HEAD 判断是否需补同步。
    """

    __tablename__ = "index_sync_state"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default="singleton")
    last_synced_commit: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    last_synced_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # syncing / ok / error / lagging
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="ok")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # 连续滞后周期数（reconciliation 连续 3 周期滞后触发告警）
    lag_periods: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )


class AdoptionEvent(Base):
    """采纳事件（客户端上报，prompt 采纳率数据源）。

    对应技术方案 3.3.6：发布后跟踪被 pull/引用次数与修改率。
    缺陷 6.3 修复：召回次数 + read 次数服务端可采，客户端上报仅辅助。
    """

    __tablename__ = "adoption_event"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # event_type：read / pull / modify / reject
    event_type: Mapped[str] = mapped_column(String(16), nullable=False, index=True)
    # stale 标记：连续 7 天无上报则 stale=true（缺陷 6.3）
    stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="")  # JSON

    __table_args__ = (
        Index("idx_adoption_asset_event", "asset_id", "event_type"),
        Index("idx_adoption_occurred", "occurred_at"),
    )


class EmbeddingVector(Base):
    """资产向量（embedding 存储，PGVector 后端）。

    双写过渡期通过 model_version 区分新旧两套向量。
    active_embedding_version 在 asset_index 上控制召回使用哪一套。
    """

    __tablename__ = "asset_embedding"

    id: Mapped[int] = mapped_column(BigInteger().with_variant(Integer, "sqlite"), primary_key=True, autoincrement=True)
    asset_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    model_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1", index=True)
    # 向量字段：PGVector 后端用 Vector(N) 类型；此处用 Text 通用存储（PGVector 实现内做转换）
    embedding: Mapped[str] = mapped_column(Text, nullable=False)
    dim: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("asset_id", "model_version", name="uq_asset_model_version"),
        Index("idx_embedding_asset_model", "asset_id", "model_version"),
    )


class DistillationJob(Base):
    """二级提炼任务（团队侧，服务端执行）。

    Agent 8 维护详细字段，本表在此仅占位以支撑 Agent 2 schema 全表创建与
    Agent 9 治理看板的 distillation_job 表 trigger_source / cluster_fingerprint 引用。
    """

    __tablename__ = "distillation_job"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    trigger_source: Mapped[str] = mapped_column(String(32), nullable=False, default="incremental")
    cluster_fingerprint: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    snapshot_commit: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    input_asset_ids: Mapped[str] = mapped_column(Text, nullable=False, default="")
    output_prompt_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (Index("idx_distill_job_cluster", "cluster_fingerprint"),)


class AssetLink(Base):
    """资产关联图（资产图谱）。

    把 AssetIndex.related_to（扁平 list）升级为带类型的有向图：
    - derived_from：A 由 B 提炼而来（如 rule 由 memory 提炼）
    - supersedes：A 取代 B（新版本）
    - related_to：松散关联（业务相关）
    - module_parent：模块层级（module_path 父子）
    - triggers：A 触发 B（如 rule 触发 tool 执行）

    查询支持：正向（A 引用了谁）+ 反向（谁引用了 A）+ 多跳遍历。
    """

    __tablename__ = "asset_link"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    src_asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("asset_index.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    dst_asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("asset_index.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # link_type ∈ {derived_from, supersedes, related_to, module_parent, triggers}
    link_type: Mapped[str] = mapped_column(String(32), nullable=False, default="related_to")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("src_asset_id", "dst_asset_id", "link_type", name="uq_asset_link_src_dst_type"),
        Index("idx_asset_link_src_type", "src_asset_id", "link_type"),
        Index("idx_asset_link_dst_type", "dst_asset_id", "link_type"),
    )


class AssetAcl(Base):
    """资产访问控制列表（restricted 资产的精准授权）。

    当 asset_index.scope='restricted' 时，本表记录谁被授权访问：
    - grantee_type='user'：授权某成员（member_id）
    - grantee_type='agent'：授权某 Agent（agent_id）
    - grantee_type='role'：授权某角色（预留，当前不用）

    permission ∈ {read, execute, admin}
    """

    __tablename__ = "asset_acl"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("asset_index.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    grantee_type: Mapped[str] = mapped_column(String(16), nullable=False)
    # member_id / agent_id / role_name（取决于 grantee_type）
    grantee_id: Mapped[str] = mapped_column(String(64), nullable=False)
    permission: Mapped[str] = mapped_column(String(16), nullable=False, default="read")
    granted_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    granted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("asset_id", "grantee_type", "grantee_id", name="uq_asset_acl_grantee"),
        Index("idx_asset_acl_asset", "asset_id"),
        Index("idx_asset_acl_grantee", "grantee_type", "grantee_id"),
    )


# ---------------------------------------------------------------------------
# 团队与成员管理
# ---------------------------------------------------------------------------


class Member(Base):
    """系统成员。

    - member_id 与 asset_index.owner 对齐（字符串）
    - role: admin / member（admin 可管理团队和成员）
    - status: active / disabled
    - created_by: 创建人（首个 admin 为 "system"）
    """

    __tablename__ = "members"

    member_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    display_name: Mapped[str] = mapped_column(String(128), nullable=False, default="")
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active")
    tags: Mapped[str] = mapped_column(Text, nullable=False, default="[]")  # JSON 数组，如 ["前端","后端"]
    created_by: Mapped[str] = mapped_column(String(64), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


class Team(Base):
    """团队（支持嵌套，parent_team_id 形成树形结构）。

    - parent_team_id 为空 → 顶层团队
    - parent_team_id 非空 → 子团队（只能由父团队的 admin 创建）
    - owner_id: 创建者 member_id
    - path: 物化路径（如 /root-team/sub-team/sub-sub），便于查询子树
    """

    __tablename__ = "teams"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    parent_team_id: Mapped[str | None] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="CASCADE"), nullable=True, index=True
    )
    owner_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    path: Mapped[str] = mapped_column(String(512), nullable=False, default="", index=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        Index("idx_team_parent", "parent_team_id"),
        Index("idx_team_path", "path"),
    )


class TeamMember(Base):
    """团队成员关联表。

    - team_id + member_id 联合唯一
    - role: admin / member（团队级角色，admin 可增删成员和创建子团队）
    """

    __tablename__ = "team_members"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    team_id: Mapped[str] = mapped_column(
        String(64), ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    member_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    role: Mapped[str] = mapped_column(String(16), nullable=False, default="member")
    added_by: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("team_id", "member_id", name="uq_team_member"),
        Index("idx_team_member_team", "team_id"),
        Index("idx_team_member_member", "member_id"),
    )


__all__ = [
    "AdoptionEvent",
    "AgentBinding",
    "AssetAcl",
    "AssetIndex",
    "AssetLink",
    "Base",
    "DistillationJob",
    "EmbeddingTaskQueue",
    "EmbeddingVector",
    "IndexSyncState",
    "Member",
    "ModuleStats",
    "RecallLog",
    "Team",
    "TeamMember",
]
