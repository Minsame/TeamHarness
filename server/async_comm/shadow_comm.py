"""影子联络（Shadow Communication）模块。

对应 Task 17：在 peer 离线时基于其本地 harness 快照进行模拟交流。

核心设计：
- **离线模拟交流**：peer 不在线时，基于其本地 PeerSnapshot 的 harness 快照生成模拟回答
- **模拟回答生成**：通过注入的 ``answer_generator`` 回调函数生成回答（调用本地 AI + peer
  快照资产）。未配置时返回占位回答
- **降级标记**：所有模拟回答标记 ``degraded=True``，``based_on=<snapshot_version>``
- **快照过期检测**：超过 ``snapshot_ttl_days`` 的快照标记 ``snapshot_stale=True``
- **outbox 持久化**：问题写入 outbox（pending_delivery），待 peer 上线时同步

ShadowComm 是独立模块，不依赖 peer_comm.py（避免循环依赖）。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Callable

from server.async_comm.constants import (
    DEFAULT_SNAPSHOT_TTL_DAYS,
    EVENT_ASK,
    EVENT_SIMULATED_ANSWER,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent, PeerSnapshot, VectorClock

# 模拟回答生成器类型：接收 (question, peer_snapshot) 返回回答文本
AnswerGenerator = Callable[[str, PeerSnapshot], str]


def default_answer_generator(question: str, snapshot: PeerSnapshot) -> str:
    """默认回答生成器（占位）。

    未配置 answer_generator 时使用，返回占位文本说明这是基于快照的模拟回答。
    """
    return (
        f"[影子联络模拟回答] 基于快照 {snapshot.snapshot_version} 生成。"
        f"问题：{question}。"
        "注意：未配置实际 AI 生成器，此为占位回答。"
    )


class ShadowComm:
    """影子联络处理器。

    在 peer 离线时基于其本地 harness 快照进行模拟交流。
    所有模拟回答标记 ``degraded=True``，基于快照版本生成。

    使用：
        sc = ShadowComm(
            mailbox=mb,
            peer_snapshot_manager=mgr,
            conversation_log=log,
            member_id="alice",
        )
        answer_event = sc.ask_peer("bob", "如何配置 lint?")
    """

    def __init__(
        self,
        *,
        mailbox: Mailbox,
        peer_snapshot_manager: PeerSnapshotManager,
        conversation_log: ConversationLog,
        member_id: str = "",
        answer_generator: AnswerGenerator | None = None,
        snapshot_ttl_days: int = DEFAULT_SNAPSHOT_TTL_DAYS,
    ) -> None:
        """初始化 ShadowComm。

        Args:
            mailbox: 本地信箱（写 outbox）。
            peer_snapshot_manager: peer 快照管理器。
            conversation_log: 交流日志。
            member_id: 本地成员 ID。
            answer_generator: 模拟回答生成器（默认用 default_answer_generator）。
            snapshot_ttl_days: 快照过期天数。
        """
        self._mailbox = mailbox
        self._peer_snapshot_manager = peer_snapshot_manager
        self._conversation_log = conversation_log
        self._member_id = member_id
        self._answer_generator: AnswerGenerator = (
            answer_generator if answer_generator is not None else default_answer_generator
        )
        self._snapshot_ttl_days = int(snapshot_ttl_days)

    def ask_peer(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """离线影子提问。

        流程：
        1. 创建 ask 事件，写入 ConversationLog 和 outbox（pending_delivery）
        2. 读取本地 PeerSnapshot[peer_id]
        3. 检测快照是否过期（snapshot_stale）
        4. 调用 answer_generator 生成模拟回答
        5. 创建 simulated_answer 事件（degraded=True, based_on=snapshot_version）
        6. 写入 ConversationLog
        7. 返回 simulated_answer 事件

        无快照时：
        - 仍创建 simulated_answer 事件，但 based_on="" 且 snapshot_stale=True
        - payload 中含 warning 说明无快照

        Args:
            peer_id: 对方 peer_id。
            question: 提问内容。
            in_reply_to: 回复的事件 ID（用于串联会话）。

        Returns:
            simulated_answer 事件。
        """
        # 1. 创建 ask 事件并持久化
        ask_event = self._create_ask_event(peer_id, question, in_reply_to=in_reply_to)
        self._conversation_log.append(ask_event)
        self._mailbox.append_outbox(ask_event)

        # 2. 读取本地 PeerSnapshot
        snapshot = self._peer_snapshot_manager.get_snapshot(peer_id)
        no_snapshot = snapshot is None
        if no_snapshot:
            # 无快照时创建空的 PeerSnapshot 占位
            snapshot = PeerSnapshot(peer_id=peer_id, snapshot_version="")

        # 3. 检测快照过期
        snapshot_stale = True if no_snapshot else self._peer_snapshot_manager.is_stale(peer_id)
        snapshot_version = "" if no_snapshot else self._peer_snapshot_manager.get_snapshot_version(peer_id)

        # 4. 调用 answer_generator 生成模拟回答
        answer_text = self._answer_generator(question, snapshot)

        # 5. 创建 simulated_answer 事件（in_reply_to 关联 ask 事件）
        # 加载 peer 的 vector_clock 作为事件向量
        vector_clock = self._peer_snapshot_manager.load_vector_clock(peer_id)
        simulated_event = self._create_simulated_answer_event(
            peer_id,
            answer_text,
            in_reply_to=ask_event.event_id,
            snapshot_version=snapshot_version,
            snapshot_stale=snapshot_stale,
            vector_clock=vector_clock,
        )
        # 无快照时在 payload 中追加 warning
        if no_snapshot:
            simulated_event.payload["warning"] = "no local snapshot available for peer"

        # 6. 写入 ConversationLog
        self._conversation_log.append(simulated_event)

        # 7. 返回 simulated_answer 事件
        return simulated_event

    def _create_ask_event(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """创建 ask 事件。

        Args:
            peer_id: 对方 peer_id。
            question: 提问内容。
            in_reply_to: 回复的事件 ID。

        Returns:
            ask 类型的 ConversationEvent（degraded=False, realtime=False）。
        """
        vector_clock = self._peer_snapshot_manager.load_vector_clock(peer_id)
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_ASK,
            peer_id=peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=vector_clock,
            payload={"question": question},
            in_reply_to=in_reply_to,
            degraded=False,
            realtime=False,
            based_on="",
            snapshot_stale=False,
        )

    def _create_simulated_answer_event(
        self,
        peer_id: str,
        answer: str,
        *,
        in_reply_to: str = "",
        snapshot_version: str = "",
        snapshot_stale: bool = False,
        vector_clock: VectorClock | None = None,
    ) -> ConversationEvent:
        """创建 simulated_answer 事件。

        标记 ``degraded=True``, ``realtime=False``, ``based_on=snapshot_version``。

        Args:
            peer_id: 对方 peer_id。
            answer: 模拟回答文本。
            in_reply_to: 回复的事件 ID（通常为对应 ask 事件的 event_id）。
            snapshot_version: 基于的快照版本号。
            snapshot_stale: 快照是否过期。
            vector_clock: 版本向量（None 时使用空 VectorClock）。

        Returns:
            simulated_answer 类型的 ConversationEvent。
        """
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id=peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=vector_clock if vector_clock is not None else VectorClock(),
            payload={"answer": answer},
            in_reply_to=in_reply_to,
            degraded=True,
            realtime=False,
            based_on=snapshot_version,
            snapshot_stale=snapshot_stale,
        )

    def _utcnow_iso(self) -> str:
        """当前 UTC ISO 时间戳（含时区后缀 Z）。"""
        return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "AnswerGenerator",
    "ShadowComm",
    "default_answer_generator",
]
