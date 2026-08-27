"""comm 域业务逻辑服务。

封装 CommEvent 与 CommPeerStatus 的读写操作，供 api.py 调用。
不直接依赖 FastAPI，便于测试注入。

核心职责：
- 记录 ask / answer 事件（含状态机流转）
- 查询对话历史（按 peer / type / 方向过滤）
- 查询影子对账状态（degraded=true 的事件）
- 查询 outbox（pending_delivery 状态的消息）
- 维护 peer 心跳与在线状态
- 列出 peer（从 Member 表 + CommPeerStatus 心跳）

在线判断：last_heartbeat 距当前时间 ≤ heartbeat_timeout_seconds（默认 120s）
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import desc, func, select
from sqlalchemy.exc import IntegrityError

from server.comm.models import CommEvent, CommPeerStatus
from server.infra_db.db import Database
from server.infra_db.models import Member

# 心跳超时：超过此时间未心跳则视为离线
HEARTBEAT_TIMEOUT_SECONDS = 120


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    """确保 datetime 带时区。

    SQLite 读出的 datetime 是 naive（无 tzinfo），与 aware datetime 比较会抛
    TypeError。此处统一补上 UTC 时区，保证跨 DB（SQLite 测试 / PG 生产）兼容。
    """
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _iso_now() -> str:
    """当前 UTC ISO 时间戳（与客户端 ConversationEvent.timestamp 对齐）。"""
    return _utcnow().isoformat()


class CommService:
    """通信事件与 peer 状态的业务服务。

    用法：
        svc = CommService(db)
        event = svc.record_ask(from_member="alice", to_peer="bob", question="...")
        peers = svc.list_peers(current_member="alice")
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # 事件记录
    # ------------------------------------------------------------------

    def record_ask(
        self,
        *,
        from_member: str,
        to_peer: str,
        question: str,
        in_reply_to: str = "",
    ) -> dict[str, Any]:
        """记录 ask 事件（发起询问）。

        - 检测 to_peer 在线状态：在线 → realtime=true, status=delivered
          离线 → degraded=true, status=pending_delivery（待 peer 上线后处理）
        - 返回事件 dict（含 event_id）
        """
        online = self._is_peer_online(to_peer)
        event_id = str(uuid.uuid4())
        timestamp = _iso_now()
        payload = json.dumps({"question": question}, ensure_ascii=False)

        event = CommEvent(
            event_id=event_id,
            event_type="ask",
            from_member=from_member,
            to_peer=to_peer,
            timestamp=timestamp,
            vector_clock="",
            payload=payload,
            in_reply_to=in_reply_to,
            degraded=not online,
            realtime=online,
            based_on="",
            snapshot_stale=False,
            conversation_state="active",
            status="delivered" if online else "pending_delivery",
        )
        with self._db.session() as sess:
            sess.add(event)
            try:
                sess.commit()
            except IntegrityError:
                sess.rollback()
                raise ValueError(f"事件已存在：{event_id}")
            sess.refresh(event)
        return self._event_to_dict(event)

    def record_answer(
        self,
        *,
        event_id: str,
        answer: str,
        realtime: bool,
        based_on: str = "",
        snapshot_stale: bool = False,
    ) -> dict[str, Any]:
        """记录 answer 事件（回复询问）。

        - 找到原 ask 事件（by event_id 或 in_reply_to）
        - 创建 answer 事件（realtime_answer / simulated_answer）
        - 更新原 ask 事件状态（delivered → confirmed）

        Args:
            event_id: 原 ask 事件的 event_id（作为 in_reply_to）
            answer: 回答内容
            realtime: True → realtime_answer, False → simulated_answer
            based_on: 影子快照版本（如 "bob_v38"）
            snapshot_stale: 快照是否过期
        """
        with self._db.session() as sess:
            ask_event = sess.get(CommEvent, event_id)
            if ask_event is None:
                raise ValueError(f"原 ask 事件不存在：{event_id}")

            answer_event_id = str(uuid.uuid4())
            event_type = "realtime_answer" if realtime else "simulated_answer"
            payload = json.dumps({"answer": answer}, ensure_ascii=False)

            answer_event = CommEvent(
                event_id=answer_event_id,
                event_type=event_type,
                from_member=ask_event.to_peer,  # 回答方 = 原 ask 的 to_peer
                to_peer=ask_event.from_member,  # 接收方 = 原 ask 的 from_member
                timestamp=_iso_now(),
                vector_clock="",
                payload=payload,
                in_reply_to=event_id,
                degraded=not realtime,
                realtime=realtime,
                based_on=based_on,
                snapshot_stale=snapshot_stale,
                conversation_state="active",
                status="confirmed" if realtime else "pending_delivery",
            )
            sess.add(answer_event)

            # 更新原 ask 事件状态：pending_delivery → delivered / confirmed
            if ask_event.status == "pending_delivery":
                ask_event.status = "delivered" if not realtime else "confirmed"
            elif ask_event.status == "delivered" and realtime:
                ask_event.status = "confirmed"

            try:
                sess.commit()
            except IntegrityError:
                sess.rollback()
                raise ValueError(f"回答事件已存在：{answer_event_id}")
            sess.refresh(answer_event)
        return self._event_to_dict(answer_event)

    def update_reconciliation(
        self,
        *,
        event_id: str,
        verdict: str,
        revised_answer: str = "",
    ) -> dict[str, Any]:
        """更新对账结果（peer 上线后对比模拟回答与真实回答）。

        Args:
            event_id: simulated_answer 事件的 event_id
            verdict: confirmed / revised / needs_human_review
            revised_answer: 修订后的回答（verdict=revised 时填写）
        """
        if verdict not in ("confirmed", "revised", "needs_human_review"):
            raise ValueError(f"非法对账结果：{verdict}")

        with self._db.session() as sess:
            event = sess.get(CommEvent, event_id)
            if event is None:
                raise ValueError(f"事件不存在：{event_id}")
            if event.event_type != "simulated_answer":
                raise ValueError(f"仅 simulated_answer 事件可对账，当前类型：{event.event_type}")

            # 合法状态流转：pending_delivery / delivered → confirmed / revised / needs_human_review
            if event.status in ("confirmed", "revised", "needs_human_review"):
                raise ValueError(f"事件已是对账终态：{event.status}")

            event.status = verdict
            if verdict == "revised" and revised_answer:
                payload = json.loads(event.payload) if event.payload else {}
                payload["revised_answer"] = revised_answer
                event.payload = json.dumps(payload, ensure_ascii=False)

            sess.commit()
            sess.refresh(event)
        return self._event_to_dict(event)

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_conversations(
        self,
        *,
        current_member: str,
        peer: str | None = None,
        event_type: str | None = None,
        direction: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询对话历史。

        Args:
            current_member: 当前登录成员（从认证上下文推导，不信任客户端传入）
            peer: 对方 peer_id 过滤
            event_type: 事件类型过滤（ask/realtime_answer/simulated_answer/...）
            direction: outgoing（我发起的）/ incoming（对方发起的）/ None（双向）
            limit/offset: 分页
        """
        with self._db.session() as sess:
            stmt = select(CommEvent).where(
                (CommEvent.from_member == current_member)
                | (CommEvent.to_peer == current_member)
            )
            if peer:
                stmt = stmt.where(
                    (CommEvent.from_member == peer) | (CommEvent.to_peer == peer)
                )
            if event_type:
                stmt = stmt.where(CommEvent.event_type == event_type)
            if direction == "outgoing":
                stmt = stmt.where(CommEvent.from_member == current_member)
            elif direction == "incoming":
                stmt = stmt.where(CommEvent.to_peer == current_member)

            # 总数
            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = sess.scalar(count_stmt) or 0

            # 分页查询（按创建时间倒序）
            stmt = stmt.order_by(desc(CommEvent.created_at)).limit(limit).offset(offset)
            items = [self._event_to_dict(row) for row in sess.scalars(stmt)]

        return {"items": items, "total": total}

    def get_thread(self, *, event_id: str) -> dict[str, Any]:
        """获取对话线程（基于 in_reply_to 回复链展开）。

        从任意事件出发，向上找 ask 根事件，向下找所有回复。
        """
        with self._db.session() as sess:
            # 找到根事件（in_reply_to 为空的事件）
            current = sess.get(CommEvent, event_id)
            if current is None:
                raise ValueError(f"事件不存在：{event_id}")

            root_id = event_id
            while current and current.in_reply_to:
                parent = sess.get(CommEvent, current.in_reply_to)
                if parent is None:
                    break
                current = parent
                root_id = current.event_id

            # 查询所有 in_reply_to = root_id 的事件 + 根事件本身
            stmt = (
                select(CommEvent)
                .where(
                    (CommEvent.event_id == root_id)
                    | (CommEvent.in_reply_to == root_id)
                )
                .order_by(CommEvent.created_at)
            )
            items = [self._event_to_dict(row) for row in sess.scalars(stmt)]

        return {"root_event_id": root_id, "items": items}

    def list_shadow_log(
        self,
        *,
        current_member: str,
        status: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询影子对账状态（degraded=true 的事件）。

        Args:
            current_member: 当前登录成员
            status: 状态过滤（pending_delivery/delivered/confirmed/revised/needs_human_review）
        """
        with self._db.session() as sess:
            stmt = select(CommEvent).where(
                CommEvent.degraded.is_(True),
                (CommEvent.from_member == current_member)
                | (CommEvent.to_peer == current_member),
            )
            if status:
                stmt = stmt.where(CommEvent.status == status)

            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = sess.scalar(count_stmt) or 0

            stmt = stmt.order_by(desc(CommEvent.created_at)).limit(limit).offset(offset)
            items = [self._event_to_dict(row) for row in sess.scalars(stmt)]

        return {"items": items, "total": total}

    def list_outbox(
        self,
        *,
        current_member: str,
        status: str | None = "pending_delivery",
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """查询 outbox（待投递消息）。

        默认只返回 pending_delivery 状态。
        """
        with self._db.session() as sess:
            stmt = select(CommEvent).where(CommEvent.from_member == current_member)
            if status:
                stmt = stmt.where(CommEvent.status == status)

            count_stmt = select(func.count()).select_from(stmt.subquery())
            total = sess.scalar(count_stmt) or 0

            stmt = stmt.order_by(desc(CommEvent.created_at)).limit(limit).offset(offset)
            items = [self._event_to_dict(row) for row in sess.scalars(stmt)]

        return {"items": items, "total": total}

    # ------------------------------------------------------------------
    # Peer 状态
    # ------------------------------------------------------------------

    def heartbeat(self, *, member_id: str, endpoint: str = "") -> dict[str, Any]:
        """更新 peer 心跳。

        若 CommPeerStatus 行不存在则创建（upsert）。
        """
        now = _utcnow()
        with self._db.session() as sess:
            row = sess.get(CommPeerStatus, member_id)
            if row is None:
                row = CommPeerStatus(
                    member_id=member_id,
                    last_heartbeat=now,
                    online=True,
                    endpoint=endpoint,
                )
                sess.add(row)
            else:
                row.last_heartbeat = now
                row.online = True
                if endpoint:
                    row.endpoint = endpoint
            sess.commit()
        return {"member_id": member_id, "online": True, "heartbeat_at": now.isoformat()}

    def list_peers(self, *, current_member: str) -> list[dict[str, Any]]:
        """列出所有 peer（从 Member 表 + CommPeerStatus 心跳）。

        capabilities（标签）从 Member.tags 实时读取（spec 设计决策，不缓存）。
        在线状态从 CommPeerStatus.last_heartbeat 判断（≤ 120s 视为在线）。
        """
        cutoff = _utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
        with self._db.session() as sess:
            # 先刷新过期的在线状态（last_heartbeat < cutoff → online=false）
            sess.execute(
                CommPeerStatus.__table__.update()
                .where(CommPeerStatus.last_heartbeat < cutoff)
                .values(online=False)
            )
            sess.commit()

            # 查询所有成员 + 心跳状态（left join）
            stmt = (
                select(Member, CommPeerStatus)
                .outerjoin(CommPeerStatus, Member.member_id == CommPeerStatus.member_id)
                .where(Member.member_id != current_member)
                .where(Member.status == "active")
                .order_by(Member.member_id)
            )
            peers: list[dict[str, Any]] = []
            for member, status in sess.execute(stmt):
                tags: list[str] = []
                try:
                    tags = json.loads(member.tags) if member.tags else []
                except (json.JSONDecodeError, TypeError):
                    tags = []
                online = bool(
                    status
                    and status.online
                    and status.last_heartbeat is not None
                    and _ensure_aware(status.last_heartbeat) >= cutoff
                )
                peers.append({
                    "member_id": member.member_id,
                    "display_name": member.display_name,
                    "tags": tags,
                    "online": online,
                    "last_heartbeat": (
                        status.last_heartbeat.isoformat() if status and status.last_heartbeat else None
                    ),
                })
        return peers

    def _is_peer_online(self, member_id: str) -> bool:
        """判断 peer 是否在线（心跳在 HEARTBEAT_TIMEOUT_SECONDS 内）。"""
        cutoff = _utcnow() - timedelta(seconds=HEARTBEAT_TIMEOUT_SECONDS)
        with self._db.session() as sess:
            row = sess.get(CommPeerStatus, member_id)
            if row is None:
                return False
            return bool(
                row.online
                and row.last_heartbeat is not None
                and _ensure_aware(row.last_heartbeat) >= cutoff
            )

    # ------------------------------------------------------------------
    # 序列化
    # ------------------------------------------------------------------

    @staticmethod
    def _event_to_dict(row: CommEvent) -> dict[str, Any]:
        """CommEvent 行 → dict（含 payload/vector_clock 反序列化）。"""
        try:
            payload = json.loads(row.payload) if row.payload else {}
        except (json.JSONDecodeError, TypeError):
            payload = {}
        try:
            vector_clock = json.loads(row.vector_clock) if row.vector_clock else {}
        except (json.JSONDecodeError, TypeError):
            vector_clock = {}
        return {
            "event_id": row.event_id,
            "event_type": row.event_type,
            "from_member": row.from_member,
            "to_peer": row.to_peer,
            "timestamp": row.timestamp,
            "vector_clock": vector_clock,
            "payload": payload,
            "in_reply_to": row.in_reply_to,
            "degraded": row.degraded,
            "realtime": row.realtime,
            "based_on": row.based_on,
            "snapshot_stale": row.snapshot_stale,
            "conversation_state": row.conversation_state,
            "status": row.status,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }


__all__ = ["CommService", "HEARTBEAT_TIMEOUT_SECONDS"]
