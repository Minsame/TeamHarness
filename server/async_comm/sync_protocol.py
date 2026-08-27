"""上线同步协议。

对应 Task 18：当 peer 由不可达转为可达时，自动触发同步与对账。

同步流程：
1. **推送 outbox**：将本地 outbox 中该 peer 的 pending_delivery 消息通过 transport.deliver()
   投递给对方，投递成功后状态更新为 delivered
2. **拉取 inbox**：通过 transport.fetch() 拉取对方发来的消息，转为 ConversationEvent
   写入 mailbox inbox 与 ConversationLog
3. **对账**：对每条本地 simulated_answer 事件，通过 in_reply_to 回复链找到对应的
   对方 realtime_answer 事件，调用 ConflictResolver.resolve() 比较两者，根据
   decision（confirmed / revised / needs_human_review）创建对账结果事件

幂等性：基于 event_id 去重，mailbox 的 append_inbox 与 ConversationLog 的 append
都内置 event_id 幂等检查，重复同步不会产生副作用。

设计原则：
- **ConflictResolverProtocol 注入**：通过 Protocol 定义接口（由 Task 19 实现），避免循环依赖
- **路径透明**：返回 SyncProtocolResult 包含各阶段计数与错误信息
- **容错**：单个阶段失败不阻断其他阶段，错误收集到 result.errors
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Protocol

from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_REVISED,
    EVENT_SIMULATED_ANSWER,
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_PENDING_DELIVERY,
    STATUS_REVISED,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent, VectorClock
from server.transport.types import Message, SyncResult

logger = logging.getLogger(__name__)


class ConflictResolverProtocol(Protocol):
    """冲突解决器接口（由 Task 19 实现）。

    对比本地 simulated_answer 与对方 realtime_answer，根据语义相似度阈值
    决定 confirmed / revised / needs_human_review。
    """

    def resolve(
        self,
        *,
        simulated_answer: str,
        real_answer: str,
        event_id: str = "",
    ) -> tuple[str, str]:
        """对比模拟回答与真实回答，返回 (decision, note)。

        Args:
            simulated_answer: 本地基于快照生成的模拟回答。
            real_answer: 对方发来的真实回答。
            event_id: 关联的 simulated_answer 事件 ID（用于日志/追溯）。

        Returns:
            (decision, note) 元组：
            - decision: "confirmed" / "revised" / "needs_human_review"
            - note: 附加说明（如相似度分数、修订建议等）
        """
        ...


@dataclass
class SyncProtocolResult:
    """同步操作结果。

    记录一次 sync_with_peer 调用的各阶段计数与错误信息。
    """

    peer_id: str
    pushed_count: int = 0       # 推送的 outbox 消息数
    received_count: int = 0     # 接收的 inbox 消息数
    confirmed_count: int = 0    # 确认的模拟回答数
    revised_count: int = 0      # 修订的模拟回答数
    needs_review_count: int = 0 # 需人工介入的模拟回答数
    errors: list[str] = field(default_factory=list)


class SyncProtocol:
    """上线同步协议。

    peer 由不可达转为可达时触发同步与对账。由 ClientDaemon 检测到 peer 上线后调用
    ``sync_with_peer(peer_id)`` 执行完整同步流程。

    使用：
        sp = SyncProtocol(
            transport=transport,
            mailbox=mailbox,
            conversation_log=log,
            peer_snapshot_manager=snap_mgr,
            member_id="alice",
            conflict_resolver=resolver,
        )
        result = sp.sync_with_peer("bob")
    """

    def __init__(
        self,
        *,
        transport: Any,
        mailbox: Mailbox,
        conversation_log: ConversationLog,
        peer_snapshot_manager: PeerSnapshotManager,
        member_id: str = "",
        conflict_resolver: ConflictResolverProtocol | None = None,
    ) -> None:
        """初始化 SyncProtocol。

        Args:
            transport: 通信传输层（实现 SyncTransport Protocol）。
            mailbox: 本地信箱（管理 outbox / inbox）。
            conversation_log: 交流日志（append-only JSONL）。
            peer_snapshot_manager: peer 快照管理器。
            member_id: 本地成员 ID（作为 sender_id）。
            conflict_resolver: 冲突解决器（可选，无则跳过对账）。
        """
        self.transport = transport
        self.mailbox = mailbox
        self.conversation_log = conversation_log
        self.peer_snapshot_manager = peer_snapshot_manager
        self.member_id = member_id
        self.conflict_resolver = conflict_resolver

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def sync_with_peer(self, peer_id: str) -> SyncProtocolResult:
        """与指定 peer 执行完整同步。

        流程：
        1. 推送 outbox 中该 peer 的 pending_delivery 消息
        2. 拉取对方发来的消息，写入 inbox
        3. 对每条 simulated_answer 进行对账（如有 conflict_resolver）
        4. 返回 SyncProtocolResult

        单个阶段失败不阻断其他阶段，错误收集到 result.errors。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            SyncProtocolResult 包含各阶段计数与错误信息。
        """
        result = SyncProtocolResult(peer_id=peer_id)

        # 阶段 1：推送 outbox
        try:
            result.pushed_count = self._push_outbox(peer_id)
        except Exception as exc:  # noqa: BLE001 — 同步阶段容错
            logger.warning("推送 outbox 失败 peer=%s: %s", peer_id, exc)
            result.errors.append(f"push_outbox: {exc}")

        # 阶段 2：拉取 inbox
        try:
            result.received_count = self._pull_inbox(peer_id)
        except Exception as exc:  # noqa: BLE001 — 同步阶段容错
            logger.warning("拉取 inbox 失败 peer=%s: %s", peer_id, exc)
            result.errors.append(f"pull_inbox: {exc}")

        # 阶段 3：对账
        try:
            confirmed, revised, needs_review = self._reconcile_simulated_answers(peer_id)
            result.confirmed_count = confirmed
            result.revised_count = revised
            result.needs_review_count = needs_review
        except Exception as exc:  # noqa: BLE001 — 同步阶段容错
            logger.warning("对账失败 peer=%s: %s", peer_id, exc)
            result.errors.append(f"reconcile: {exc}")

        return result

    # ------------------------------------------------------------------
    # 阶段实现
    # ------------------------------------------------------------------

    def _push_outbox(self, peer_id: str) -> int:
        """推送 outbox 中该 peer 的 pending_delivery 消息。

        通过 transport.deliver() 投递，投递成功后更新状态为 delivered。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            成功投递的消息数。
        """
        pending = self.mailbox.load_outbox(status=STATUS_PENDING_DELIVERY)
        # 过滤出 recipient == peer_id 的消息（ConversationEvent.peer_id 即对方 peer_id）
        target_events = [e for e in pending if e.peer_id == peer_id]
        if not target_events:
            return 0

        messages = [self._build_message_from_event(e, peer_id) for e in target_events]
        sync_result: SyncResult = self.transport.deliver(peer_id, messages)

        # 判断投递成功的消息
        if sync_result.delivered_message_ids:
            delivered_set = set(sync_result.delivered_message_ids)
            successful = [
                e for e in target_events
                if e.event_id in delivered_set
            ]
        elif sync_result.success:
            # success=True 但未填充 delivered_message_ids，认为全部成功
            successful = list(target_events)
        else:
            successful = []

        count = 0
        for event in successful:
            try:
                if self.mailbox.update_status(event.event_id, STATUS_DELIVERED):
                    count += 1
            except ValueError:
                # 非法状态流转（如已是终态），跳过
                logger.debug(
                    "更新 outbox 状态失败 event_id=%s peer=%s",
                    event.event_id,
                    peer_id,
                )
        return count

    def _pull_inbox(self, peer_id: str) -> int:
        """拉取对方发来的消息，写入 inbox。

        通过 transport.fetch() 拉取，转换为 ConversationEvent 写入 mailbox inbox
        与 conversation_log。基于 event_id 幂等去重。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            接收的消息数（含已存在的幂等跳过计数）。
        """
        messages = self.transport.fetch(peer_id)
        if not messages:
            return 0

        count = 0
        for msg in messages:
            event = self._event_from_message(msg)
            # mailbox 与 conversation_log 都基于 event_id 幂等去重
            self.mailbox.append_inbox(event)
            self.conversation_log.append(event)
            count += 1
        return count

    def _reconcile_simulated_answers(self, peer_id: str) -> tuple[int, int, int]:
        """对账：对比本地 simulated_answer 与对方 realtime_answer。

        从 ConversationLog 中查找与该 peer 的事件：
        - 本地 simulated_answer 事件（degraded=True）
        - 对方发来的 realtime_answer 事件

        对每个 simulated_answer，通过 in_reply_to 回复链找到对应的 ask 事件，
        再找 in_reply_to 指向同一 ask 的 realtime_answer。若找到匹配，调用
        conflict_resolver.resolve()，根据 decision 创建 confirmed / revised /
        needs_human_review 事件。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            (confirmed_count, revised_count, needs_review_count)。
        """
        if self.conflict_resolver is None:
            return (0, 0, 0)

        events = self.conversation_log.load_by_peer(peer_id)

        # 本地 simulated_answer 事件
        simulated_events = [
            e for e in events if e.event_type == EVENT_SIMULATED_ANSWER
        ]
        if not simulated_events:
            return (0, 0, 0)

        # 对方发来的 realtime_answer 事件
        realtime_events = [
            e for e in events if e.event_type == EVENT_REALTIME_ANSWER
        ]

        confirmed_count = 0
        revised_count = 0
        needs_review_count = 0

        for sim in simulated_events:
            ask_id = sim.in_reply_to
            if not ask_id:
                continue
            # 找出指向同一 ask 的 realtime_answer
            matching = [
                r for r in realtime_events if r.in_reply_to == ask_id
            ]
            if not matching:
                continue

            real = matching[0]
            simulated_answer = str(sim.payload.get("answer", ""))
            real_answer = str(real.payload.get("answer", ""))

            try:
                decision, note = self.conflict_resolver.resolve(
                    simulated_answer=simulated_answer,
                    real_answer=real_answer,
                    event_id=sim.event_id,
                )
            except Exception as exc:  # noqa: BLE001 — 单条对账失败不影响其他
                logger.warning(
                    "conflict_resolver.resolve 失败 event_id=%s: %s",
                    sim.event_id,
                    exc,
                )
                continue

            # 创建对账结果事件并写入日志
            resolution_event = self._create_resolution_event(
                peer_id,
                decision,
                in_reply_to=sim.event_id,
                note=note,
                simulated_answer=simulated_answer,
                real_answer=real_answer,
            )
            self.conversation_log.append(resolution_event)

            # 更新 mailbox 中对应 simulated_answer 消息的状态
            self._update_mailbox_status_for_resolution(sim.event_id, decision)

            # 计数
            if decision == "confirmed":
                confirmed_count += 1
            elif decision == "revised":
                revised_count += 1
            elif decision == "needs_human_review":
                needs_review_count += 1

        return (confirmed_count, revised_count, needs_review_count)

    # ------------------------------------------------------------------
    # 内部：事件与消息构造
    # ------------------------------------------------------------------

    def _create_resolution_event(
        self,
        peer_id: str,
        decision: str,
        *,
        in_reply_to: str = "",
        note: str = "",
        simulated_answer: str = "",
        real_answer: str = "",
    ) -> ConversationEvent:
        """创建对账结果事件（confirmed / revised / needs_human_review）。

        Args:
            peer_id: 对方 peer_id。
            decision: conflict_resolver 返回的决策字符串。
            in_reply_to: 关联的 simulated_answer 事件 ID。
            note: conflict_resolver 返回的附加说明。
            simulated_answer: 模拟回答文本（写入 payload 供追溯）。
            real_answer: 真实回答文本（写入 payload 供追溯）。

        Returns:
            confirmed / revised / needs_human_review 类型的 ConversationEvent。
        """
        if decision == "confirmed":
            event_type = EVENT_CONFIRMED
        elif decision == "revised":
            event_type = EVENT_REVISED
        elif decision == "needs_human_review":
            event_type = EVENT_NEEDS_HUMAN_REVIEW
        else:
            # 容错：未知 decision 直接用作 event_type
            event_type = decision

        payload: dict[str, Any] = {
            "decision": decision,
            "note": note,
        }
        if simulated_answer:
            payload["simulated_answer"] = simulated_answer
        if real_answer:
            payload["real_answer"] = real_answer

        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type=event_type,
            peer_id=peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=VectorClock(),
            payload=payload,
            in_reply_to=in_reply_to,
            degraded=False,
            realtime=False,
        )

    def _build_message_from_event(
        self,
        event: ConversationEvent,
        peer_id: str,
    ) -> Message:
        """将 ConversationEvent 转为 transport Message。

        message_id 与 event_id 相同，便于 SyncResult.delivered_message_ids 回执匹配。

        Args:
            event: 源 ConversationEvent。
            peer_id: 接收方 peer_id。

        Returns:
            transport Message 实例。
        """
        return Message(
            message_id=event.event_id,
            event_id=event.event_id,
            sender_id=self.member_id,
            recipient_id=peer_id,
            msg_type=event.event_type,
            payload=dict(event.payload),
            timestamp=event.timestamp or self._utcnow_iso(),
            in_reply_to=event.in_reply_to,
        )

    def _event_from_message(self, msg: Message) -> ConversationEvent:
        """将 transport Message 转为 ConversationEvent。

        event.peer_id 设为 msg.sender_id（对方 peer_id）。
        event_type 从 payload["_event_type"] 读取（约定），或基于 msg_type 推断。

        Args:
            msg: transport Message。

        Returns:
            ConversationEvent 实例。
        """
        # 推断 event_type：优先读 payload 约定字段，否则按 msg_type 映射
        event_type = str(msg.payload.get("_event_type", "")) if msg.payload else ""
        if not event_type:
            if msg.msg_type == "answer":
                event_type = EVENT_REALTIME_ANSWER
            elif msg.msg_type == "ask":
                event_type = EVENT_ASK
            else:
                event_type = msg.msg_type

        # vector_clock 从 payload 提取（若有）
        vc_data = msg.payload.get("vector_clock") if msg.payload else None
        vector_clock = (
            VectorClock.from_dict(vc_data) if isinstance(vc_data, dict) else VectorClock()
        )

        # event_id 缺失时生成新 UUID
        event_id = msg.event_id or str(uuid.uuid4())

        # realtime 标记：realtime_answer 类型标记为 True（对方实时回答）
        realtime = event_type == EVENT_REALTIME_ANSWER

        return ConversationEvent(
            event_id=event_id,
            event_type=event_type,
            peer_id=msg.sender_id,
            timestamp=msg.timestamp or self._utcnow_iso(),
            vector_clock=vector_clock,
            payload=dict(msg.payload) if msg.payload else {},
            in_reply_to=msg.in_reply_to,
            degraded=False,
            realtime=realtime,
        )

    def _update_mailbox_status_for_resolution(
        self,
        event_id: str,
        decision: str,
    ) -> None:
        """根据对账决策更新 mailbox 中对应消息的状态。

        decision → status 映射：
        - confirmed → STATUS_CONFIRMED
        - revised → STATUS_REVISED
        - needs_human_review → STATUS_NEEDS_HUMAN_REVIEW

        消息不在 mailbox 中（如 simulated_answer 不在 outbox/inbox）时静默跳过。
        """
        status_map = {
            "confirmed": STATUS_CONFIRMED,
            "revised": STATUS_REVISED,
            "needs_human_review": STATUS_NEEDS_HUMAN_REVIEW,
        }
        new_status = status_map.get(decision)
        if not new_status:
            return
        try:
            self.mailbox.update_status(event_id, new_status)
        except ValueError:
            # 非法状态流转（如已是终态），静默跳过
            logger.debug(
                "更新 mailbox 状态失败 event_id=%s decision=%s",
                event_id,
                decision,
            )

    def _utcnow_iso(self) -> str:
        """当前 UTC ISO 时间戳。"""
        return datetime.now(timezone.utc).isoformat()


__all__ = [
    "ConflictResolverProtocol",
    "SyncProtocol",
    "SyncProtocolResult",
]
