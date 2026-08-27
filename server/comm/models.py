"""comm 域 SQLAlchemy ORM 模型 — 成员 AI 通信事件与 peer 状态。

对应 spec `add-shadow-comm-module` 的服务端数据持久化层。
字段与 async_comm/types.py 的 ConversationEvent 对齐，新增 from_member / to_peer / status 字段。

表：
- CommEvent：交流事件（ask / realtime_answer / simulated_answer / confirmed / revised / needs_human_review）
- CommPeerStatus：peer 在线状态（心跳维护，capabilities 从 Member.tags 实时读取）

设计原则：
- append-only：事件写入后不修改（仅 status 字段可流转，参考 mailbox 状态机）
- 幂等去重：event_id (UUID) 唯一
- 回复链：in_reply_to 串联 ask → answer
- 双向：from_member / to_peer 区分发起方与接收方
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    Boolean,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from server.infra_db.models import Base, _utcnow


class CommEvent(Base):
    """交流事件（服务端持久化）。

    对应客户端 async_comm/types.py 的 ConversationEvent，新增：
    - from_member：发起方 member_id（服务端视角区分方向）
    - to_peer：接收方 peer_id（= to_member）
    - status：消息状态（pending_delivery / delivered / confirmed / revised / needs_human_review）
    - direction：冗余字段，便于查询（outgoing: from_member=me / incoming: to_peer=me）

    事件类型（event_type）：
    - ask：发起询问
    - realtime_answer：实时回答（peer 在线时）
    - simulated_answer：影子联络模拟回答（peer 离线时）
    - confirmed：对账确认（相似度 ≥ 0.8）
    - revised：对账修订（相似度 0.3~0.8）
    - needs_human_review：需人工介入（相似度 < 0.3）

    状态机（status，对应 mailbox）：
    - pending_delivery → delivered / confirmed / revised / needs_human_review
    - delivered → confirmed / revised / needs_human_review
    - confirmed / revised / needs_human_review → 终态
    """

    __tablename__ = "comm_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    # 发起方与接收方（服务端视角）
    from_member: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    to_peer: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    # 事件时间戳（ISO 字符串，与客户端 ConversationEvent.timestamp 对齐）
    timestamp: Mapped[str] = mapped_column(String(40), nullable=False)
    # vector_clock 序列化为 JSON 字符串
    vector_clock: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # payload 序列化为 JSON 字符串（含 question / answer / probe 等）
    payload: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # 回复链：本事件回复的 ask 事件 ID
    in_reply_to: Mapped[str] = mapped_column(String(64), nullable=False, default="", index=True)
    # 标记位
    degraded: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    realtime: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    based_on: Mapped[str] = mapped_column(String(64), nullable=False, default="")
    snapshot_stale: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    conversation_state: Mapped[str] = mapped_column(String(32), nullable=False, default="active")
    # 消息状态（对应 mailbox 状态机）
    status: Mapped[str] = mapped_column(
        String(32), nullable=False, default="pending_delivery", index=True
    )
    # DB 时间戳（用于排序与 TTL）
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, index=True
    )

    __table_args__ = (
        UniqueConstraint("event_id", name="uq_comm_event_id"),
        Index("idx_comm_from_member_created", "from_member", "created_at"),
        Index("idx_comm_to_peer_created", "to_peer", "created_at"),
        Index("idx_comm_pair", "from_member", "to_peer"),
        Index("idx_comm_status_type", "status", "event_type"),
    )


class CommPeerStatus(Base):
    """peer 在线状态（心跳维护）。

    单行 per member（member_id 为主键）。
    capabilities（标签）不在此表缓存，从 Member.tags 实时读取（spec 设计决策）。
    在线判断：last_heartbeat 距当前时间 ≤ heartbeat_timeout_seconds（默认 120s）。
    """

    __tablename__ = "comm_peer_status"

    member_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    last_heartbeat: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow
    )
    # 在线状态冗余字段（便于查询，由心跳定期更新）
    online: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    # peer 自报的 endpoint（P2P 模式用，central 模式留空）
    endpoint: Mapped[str] = mapped_column(String(256), nullable=False, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=_utcnow, onupdate=_utcnow
    )

    __table_args__ = (
        Index("idx_comm_peer_online", "online"),
    )


__all__ = ["CommEvent", "CommPeerStatus"]
