"""binding: agent_binding 写时复制扩展 + Agent 5 装配服务新表

Revision ID: 0002_binding
Revises: 0001_initial
Create Date: 2026-01-02 00:00:00

对应 SubTask 5.2（task_routing 调度索引表）+ SubTask 5.5（pending_category）+
SubTask 5.8（写时复制：agent_binding 扩展 binding_version / superseded_at）+
SubTask 5.9（tool_review_record 签名/CODEOWNERS 审查留痕）+
SubTask 5.10（agent_api_key 鉴权表）。

变更：
1. agent_binding 表：
   - 新增 binding_version VARCHAR(32) NOT NULL DEFAULT '0.0.1'
   - 新增 superseded_at TIMESTAMPTZ NULL
   - 删除唯一约束 uq_agent_asset（按 agent_id+asset_id）
   - 新增唯一约束 uq_agent_asset_version（agent_id+asset_id+binding_version）
   - 新增索引 idx_binding_agent_active（agent_id, superseded_at）
2. task_routing：调度索引表（task_type + category → asset_id + priority + auto_bind）
3. pending_category：快速模式 post-hoc 校验未登记 module 的待办
4. agent_api_key：API Key 颁发/轮换/反查
5. tool_review_record：tool PR Review 留痕（CODEOWNERS 审查 + 签名验证结果）
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002_binding"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _is_postgresql() -> bool:
    bind = op.get_bind()
    return bind.dialect.name in ("postgresql", "psycopg", "psycopg2")


def upgrade() -> None:
    # 1. agent_binding 表扩展（写时复制）
    op.add_column(
        "agent_binding",
        sa.Column(
            "binding_version",
            sa.String(32),
            nullable=False,
            server_default="0.0.1",
        ),
    )
    op.add_column(
        "agent_binding",
        sa.Column("superseded_at", sa.DateTime(timezone=True), nullable=True),
    )
    # 删除旧唯一约束（按 agent_id+asset_id），改为含 binding_version 的复合唯一键
    op.drop_constraint("uq_agent_asset", "agent_binding", type_="unique")
    op.create_unique_constraint(
        "uq_agent_asset_version",
        "agent_binding",
        ["agent_id", "asset_id", "binding_version"],
    )
    op.create_index(
        "idx_binding_agent_active",
        "agent_binding",
        ["agent_id", "superseded_at"],
    )
    op.create_index(
        "idx_binding_superseded_at", "agent_binding", ["superseded_at"]
    )

    # 2. task_routing：调度索引表（按 task_type + category 自动绑定）
    op.create_table(
        "task_routing",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("task_type", sa.String(64), nullable=False),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("asset_id", sa.String(64), nullable=False),
        sa.Column("binding_type", sa.String(16), nullable=False, server_default="on-demand"),
        sa.Column("priority", sa.String(16), nullable=False, server_default="normal"),
        sa.Column("auto_bind", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint(
            "task_type", "category", "asset_id", name="uq_task_routing_asset"
        ),
    )
    op.create_index("idx_task_routing_type_category", "task_routing", ["task_type", "category"])
    op.create_index("idx_task_routing_asset", "task_routing", ["asset_id"])
    op.create_index("idx_task_routing_auto_bind", "task_routing", ["auto_bind"])

    # 3. pending_category：快速模式 post-hoc 校验待登记 module
    op.create_table(
        "pending_category",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("category", sa.String(64), nullable=False),
        sa.Column("module", sa.String(64), nullable=False),
        sa.Column("asset_path", sa.String(512), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("reason", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("alert_sent", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_pending_category_status", "pending_category", ["status"])
    op.create_index("idx_pending_category_module", "pending_category", ["module"])
    op.create_index("idx_pending_category_alert", "pending_category", ["alert_sent"])

    # 4. agent_api_key：API Key 颁发/轮换/反查
    op.create_table(
        "agent_api_key",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("agent_id", sa.String(64), nullable=False),
        sa.Column("member_id", sa.String(64), nullable=False),
        sa.Column("key_hash", sa.String(128), nullable=False, unique=True),
        sa.Column("key_prefix", sa.String(16), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="active"),
        sa.Column("rotated_from", sa.String(64), nullable=True),
        sa.Column("issued_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("rotated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_used_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("idx_agent_api_key_agent_id", "agent_api_key", ["agent_id"])
    op.create_index("idx_agent_api_key_member_id", "agent_api_key", ["member_id"])
    op.create_index("idx_agent_api_key_status", "agent_api_key", ["status"])
    op.create_index("idx_agent_api_key_key_hash", "agent_api_key", ["key_hash"])

    # 5. tool_review_record：tool PR Review 留痕
    op.create_table(
        "tool_review_record",
        sa.Column("id", sa.String(64), primary_key=True),
        sa.Column("pr_id", sa.String(64), nullable=False),
        sa.Column("asset_path", sa.String(512), nullable=False),
        sa.Column("commit_sha", sa.String(64), nullable=False, server_default=""),
        sa.Column("signature_present", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("signature_valid", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("codeowners_approved", sa.Boolean, nullable=False, server_default=sa.text("false")),
        sa.Column("trusted_reviewers_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("decision", sa.String(16), nullable=False, server_default="pending"),
        sa.Column("reason", sa.Text, nullable=False, server_default=""),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("idx_tool_review_pr_id", "tool_review_record", ["pr_id"])
    op.create_index("idx_tool_review_asset_path", "tool_review_record", ["asset_path"])
    op.create_index("idx_tool_review_decision", "tool_review_record", ["decision"])


def downgrade() -> None:
    op.drop_table("tool_review_record")
    op.drop_table("agent_api_key")
    op.drop_table("pending_category")
    op.drop_table("task_routing")
    op.drop_index("idx_binding_superseded_at", "agent_binding")
    op.drop_index("idx_binding_agent_active", "agent_binding")
    op.drop_constraint("uq_agent_asset_version", "agent_binding", type_="unique")
    op.create_unique_constraint(
        "uq_agent_asset", "agent_binding", ["agent_id", "asset_id"]
    )
    op.drop_column("agent_binding", "superseded_at")
    op.drop_column("agent_binding", "binding_version")
