"""binding 域 SQLAlchemy ORM 模型 — Agent 5 装配服务专用表。

对应 SubTask 5.2 / 5.5 / 5.9 / 5.10，建表 DDL 见 Alembic migration 0002_binding。

包含：
- TaskRouting：调度索引表（task_type + category → asset_id + auto_bind）
- PendingCategory：快速模式 post-hoc 校验未登记 module 的待办
- AgentApiKey：API Key 颁发/轮换/反查
- ToolReviewRecord：tool PR Review 留痕（CODEOWNERS + 签名验证）

注：AgentBinding 已由 infra_db/models.py 定义（Agent 2 域），
SubTask 5.8 写时复制在 AgentBinding 上扩展了 binding_version / superseded_at 字段。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.infra_db.models import Base, _utcnow


class TaskRouting(Base):
    """调度索引表：按 task_type + category 自动绑定资产。

    auto_bind=true 的行参与 SubTask 5.2 auto_bind 匹配：
    给定 (task_type, category) → 命中所有 auto_bind=true 的资产 → 创建 agent_binding。
    auto_bind=false 的行仅作记录（手动绑定参考），不参与自动装配。
    """

    __tablename__ = "task_routing"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("asset_index.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    binding_type: Mapped[str] = mapped_column(String(16), nullable=False, default="on-demand")
    priority: Mapped[str] = mapped_column(String(16), nullable=False, default="normal")
    auto_bind: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )

    __table_args__ = (
        UniqueConstraint("task_type", "category", "asset_id", name="uq_task_routing_asset"),
        Index("idx_task_routing_type_category", "task_type", "category"),
    )


class PendingCategory(Base):
    """快速模式 post-hoc 校验待登记 module。

    对应 SubTask 5.5：push main 后 post-hoc 校验资产 category，
    若 <module> 未在 INDEX.md 登记 → 创建 pending 行 + 告警。
    人工补登记后 status=resolved。
    """

    __tablename__ = "pending_category"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    category: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    module: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_path: Mapped[str] = mapped_column(String(512), nullable=False)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    reason: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    alert_sent: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    resolved_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class AgentApiKey(Base):
    """Agent API Key 颁发/轮换/反查表。

    对应 SubTask 5.10：
    - key_hash：sha256(key) 存储，客户端只持有原始 key
    - key_prefix：key 前 8 字符明文（便于人类识别）
    - status：active / rotated / revoked
    - rotated_from：轮换时指向上一把 active 的 key id
    - 反查：通过 key_hash 找到 agent_id（鉴权时用）
    """

    __tablename__ = "agent_api_key"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    member_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    key_hash: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    key_prefix: Mapped[str] = mapped_column(String(16), nullable=False, default="")
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="active", index=True)
    rotated_from: Mapped[str | None] = mapped_column(String(64), nullable=True)
    issued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    rotated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_used_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class ToolReviewRecord(Base):
    """tool PR Review 留痕（CODEOWNERS + 签名验证结果）。

    对应 SubTask 5.9：
    - signature_present：tool 文件 frontmatter 是否含 signature 字段
    - signature_valid：签名是否通过公钥验签
    - codeowners_approved：是否至少一名 trusted reviewer approve
    - trusted_reviewers_count：trusted reviewer 审批数
    - decision：approved / rejected / pending
    """

    __tablename__ = "tool_review_record"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    pr_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    asset_path: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    commit_sha: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    signature_present: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    signature_valid: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    codeowners_approved: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    trusted_reviewers_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    decision: Mapped[str] = mapped_column(String(16), nullable=False, default="pending", index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    reviewed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )


__all__ = [
    "AgentApiKey",
    "PendingCategory",
    "TaskRouting",
    "ToolReviewRecord",
]
