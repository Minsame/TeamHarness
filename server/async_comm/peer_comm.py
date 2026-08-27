"""成员 AI 通信核心入口。

对应 Task 16。

统一调度在线实时通信与离线影子联络两条路径，调用方不感知 peer 是否在线：
- ``ask_peer()`` 自动探测可达性，在线走 transport 实时路径，离线委托 ShadowComm
- ``share_asset()`` 资产定向共享，可达实时推送，不可达写入 outbox 等待同步
- 可达性按 ``network_check_interval_seconds`` 缓存，避免每次调用都探测

设计原则：
- **路径透明**：返回结构一致（ConversationEvent），仅 ``degraded`` / ``realtime`` 标记不同
- **ShadowComm 注入**：通过构造函数注入（Protocol 定义接口，避免循环依赖）
- **VectorClock 集成**：每次 ask 递增本地 counter，事件携带当前版本向量
"""

from __future__ import annotations

import logging
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from server.async_comm.constants import (
    CONV_STATE_ACTIVE,
    CONV_STATE_PAUSED,
    DEFAULT_PROBE_TIMEOUT_SECONDS,
    DEFAULT_TAG_FALLBACK_SHADOW,
    EVENT_ASK,
    EVENT_REALTIME_ANSWER,
    MSG_TYPE_PROBE,
    MSG_TYPE_TAGS_SYNC,
    STATUS_DELIVERED,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent, VectorClock
from server.transport.protocol import SyncTransport
from server.transport.types import Message, PeerInfo, SyncResult

logger = logging.getLogger(__name__)


class ShadowCommProtocol(Protocol):
    """ShadowComm 协议（由 Task 17 实现，这里只定义接口）。

    离线影子联络处理器，在 peer 不可达时由 PeerComm 委托调用。
    """

    def ask_peer(
        self, peer_id: str, question: str, *, in_reply_to: str = ""
    ) -> ConversationEvent:
        """离线影子提问。

        Args:
            peer_id: 对方 peer_id。
            question: 提问内容。
            in_reply_to: 关联的 ask 事件 ID（回复链）。

        Returns:
            simulated_answer 事件（degraded=True, realtime=False）。
        """
        ...


class PeerComm:
    """成员 AI 通信核心入口。

    自动选择在线实时通信或离线影子联络路径。调用方不感知 peer 是否在线。

    使用：
        comm = PeerComm(
            transport=transport,
            mailbox=mailbox,
            conversation_log=log,
            peer_snapshot_manager=snap_mgr,
            member_id="alice",
            shadow_comm=shadow,
        )
        answer_event = comm.ask_peer("bob", "如何处理 X？")
    """

    def __init__(
        self,
        *,
        transport: SyncTransport,
        mailbox: Mailbox,
        conversation_log: ConversationLog,
        peer_snapshot_manager: PeerSnapshotManager,
        member_id: str = "",
        network_check_interval_seconds: int = 60,
        shadow_comm: ShadowCommProtocol | None = None,
    ) -> None:
        """初始化 PeerComm。

        Args:
            transport: 通信传输层（central / p2p / hybrid）。
            mailbox: 本地信箱（管理 outbox / inbox）。
            conversation_log: 交流日志（append-only JSONL）。
            peer_snapshot_manager: peer 快照管理器。
            member_id: 本地成员 ID（作为 sender_id 与 vector_clock 的 key）。
            network_check_interval_seconds: 可达性缓存有效期（秒）。
            shadow_comm: 影子联络处理器（离线时使用，可选）。
        """
        self.transport = transport
        self.mailbox = mailbox
        self.conversation_log = conversation_log
        self.peer_snapshot_manager = peer_snapshot_manager
        self.member_id = member_id
        self._network_check_interval_seconds = int(network_check_interval_seconds)
        self.shadow_comm = shadow_comm
        # 可达性缓存：{peer_id: (is_reachable, timestamp_unix)}
        self._reachability_cache: dict[str, tuple[bool, float]] = {}
        # 本地版本向量（每次 ask 递增本地 counter）
        self._local_vector_clock = VectorClock()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def ask_peer(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
        tag: str = "",
    ) -> ConversationEvent:
        """向 peer 提问（统一入口，自动路径选择）。

        流程：
        1. 创建 ask 事件写入 ConversationLog
        2. 探测 peer 可达性（带缓存）
        3. peer 可达 → 在线实时路径：
           - transport.deliver() 投递 ask 消息
           - transport.fetch() 拉取回答
           - 写入 realtime_answer 事件（realtime=True）
        4. peer 不可达 → 影子联络路径：
           - 写入 ask 事件到 outbox（pending_delivery）
           - 委托 shadow_comm.ask_peer()（为 None 时抛 RuntimeError）
        5. 返回回答事件（realtime_answer 或 simulated_answer）

        Task 25 扩展：
        - ``tag`` 非空时走 ``_ask_by_tag`` 多候选混合策略（``peer_id`` 仅用于事件归属）
        - ``tag`` 与 ``peer_id`` 二选一：``tag`` 优先（``peer_id`` 仅作为 fallback / 标识）

        Args:
            peer_id: 对方 peer_id（``tag`` 模式下仅作为 fallback 与事件归属）。
            question: 提问内容。
            in_reply_to: 关联的前驱事件 ID（回复链）。
            tag: 按标签路由（如 "运维"），非空时走多候选混合策略。

        Returns:
            回答事件（realtime_answer 或 simulated_answer）。
        """
        # Task 25：tag 模式优先（多候选混合策略）
        if tag:
            return self._ask_by_tag(
                tag, question, fallback_peer_id=peer_id, in_reply_to=in_reply_to
            )

        # 1. 创建 ask 事件并写入 ConversationLog
        ask_event = self._create_ask_event(peer_id, question, in_reply_to=in_reply_to)
        self.conversation_log.append(ask_event)

        # 2. 探测可达性（带缓存）
        if self._is_peer_reachable(peer_id):
            # 3. 在线实时路径
            return self._realtime_ask(ask_event, peer_id, question, in_reply_to=in_reply_to)

        # 4. 离线影子路径
        return self._offline_ask(ask_event, peer_id, question)

    def share_asset(
        self,
        asset_id: str,
        to_peer_id: str,
        *,
        asset_content: dict[str, Any] | None = None,
    ) -> SyncResult:
        """资产定向共享。

        peer 可达 → transport.deliver() 实时推送；
        peer 不可达 → 写入 outbox 等待上线同步。

        Args:
            asset_id: 资产 ID。
            to_peer_id: 接收方 peer_id。
            asset_content: 资产内容（可选）。

        Returns:
            SyncResult 反映投递结果。
        """
        payload: dict[str, Any] = {"asset_id": asset_id, "content": asset_content or {}}
        message = self._build_message(
            to_peer_id,
            "share_asset",
            payload,
            event_id=str(uuid.uuid4()),
        )

        if self._is_peer_reachable(to_peer_id):
            # 可达 → 实时推送
            return self.transport.deliver(to_peer_id, [message])

        # 不可达 → 写入 outbox 等待上线同步
        event = ConversationEvent(
            event_id=message.event_id,
            event_type="share_asset",
            peer_id=to_peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=VectorClock.from_dict(self._local_vector_clock.to_dict()),
            payload=payload,
        )
        self.mailbox.append_outbox(event)
        return SyncResult(success=False, pending_count=1)

    def list_peers(self) -> list[str]:
        """列出已知 peer（通过 transport.discover_peers()）。

        Returns:
            peer_id 列表。
        """
        peers = self.transport.discover_peers()
        return [p.peer_id for p in peers]

    def resume_conversation(self, peer_id: str) -> list[ConversationEvent]:
        """恢复与指定 peer 的对话（Task 27）。

        委托 ConversationLog.resume_conversation：
        - 读取 conversation_state.json 中该 peer 的 paused/timeout_disconnect 状态
        - 基于 in_reply_to 链重建上下文（load_thread）
        - 标记为 resumed
        - 返回历史事件列表

        peer 在线时由 daemon._run_online_sync 自动调用；
        也可手动调用以恢复特定 peer 的对话上下文。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            重建的对话上下文事件列表（按时间排序），无需恢复时返回空列表。
        """
        return self.conversation_log.resume_conversation(peer_id)

    # ------------------------------------------------------------------
    # 内部：路径实现
    # ------------------------------------------------------------------

    def _realtime_ask(
        self,
        ask_event: ConversationEvent,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """在线实时路径：deliver + fetch。"""
        # 构建 ask 消息并投递
        message = self._build_message(
            peer_id,
            "ask",
            {
                "question": question,
                "vector_clock": ask_event.vector_clock.to_dict(),
            },
            event_id=ask_event.event_id,
            in_reply_to=in_reply_to,
        )
        self.transport.deliver(peer_id, [message])

        # 拉取回答
        responses = self.transport.fetch(peer_id)
        answer_msg: Message | None = None
        for msg in responses:
            if msg.in_reply_to == ask_event.event_id:
                answer_msg = msg
                break

        if answer_msg is not None:
            answer_text = str(answer_msg.payload.get("answer", ""))
            answer_event = self._create_realtime_answer_event(
                peer_id,
                answer_text,
                in_reply_to=ask_event.event_id,
            )
        else:
            # fetch 无回答，返回 pending 状态的回答事件（仍标记 realtime=True）
            answer_event = self._create_realtime_answer_event(
                peer_id,
                "",
                in_reply_to=ask_event.event_id,
            )
            answer_event.payload["status"] = "pending"

        self.conversation_log.append(answer_event)
        # Task 27：在线实时交互成功 → 标记对话为 active
        self.conversation_log.set_conversation_state(
            peer_id,
            CONV_STATE_ACTIVE,
            last_event_id=answer_event.event_id,
            reason="realtime_active",
        )
        return answer_event

    def _offline_ask(
        self,
        ask_event: ConversationEvent,
        peer_id: str,
        question: str,
    ) -> ConversationEvent:
        """离线影子路径：写 outbox + 委托 shadow_comm。"""
        # 写入 ask 事件到 outbox（状态 pending_delivery）
        self.mailbox.append_outbox(ask_event)

        # Task 27：peer 不可达 → 标记对话为 paused（等待恢复）
        self.conversation_log.set_conversation_state(
            peer_id,
            CONV_STATE_PAUSED,
            last_event_id=ask_event.event_id,
            reason="peer_offline",
        )

        # shadow_comm 为 None 时抛 RuntimeError
        if self.shadow_comm is None:
            raise RuntimeError(
                "shadow_comm not configured for offline communication"
            )

        # 委托 shadow_comm（in_reply_to 关联 ask 事件）
        return self.shadow_comm.ask_peer(
            peer_id,
            question,
            in_reply_to=ask_event.event_id,
        )

    # ------------------------------------------------------------------
    # Task 25：按 tag 路由 + 多候选混合策略
    # ------------------------------------------------------------------

    def _ask_by_tag(
        self,
        tag: str,
        question: str,
        *,
        fallback_peer_id: str = "",
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """按 tag 路由的多候选混合策略入口。

        Task 25 SubTask 25.7：
        1. 先广播轻量 probe 消息给所有匹配 tag 的候选 peer
        2. 收到响应后定向追问第一个响应者（_targeted_ask）
        3. 无响应（超时）转 fallback_peer_id 的影子联络（_offline_ask）

        Args:
            tag: 标签（如 "运维"）。
            question: 提问内容。
            fallback_peer_id: 无候选响应时的兜底 peer_id（事件归属）。
            in_reply_to: 回复链。

        Returns:
            回答事件（realtime_answer / simulated_answer）。
        """
        # 1. 查询候选 peer 列表
        candidates = self._resolve_candidates_by_tag(tag)
        # 候选去重并保留顺序
        seen: set[str] = set()
        unique_candidates: list[str] = []
        for c in candidates:
            if c and c not in seen and c != self.member_id:
                seen.add(c)
                unique_candidates.append(c)

        if not unique_candidates:
            # 无候选 → 直接走影子联络（基于 fallback_peer_id）
            logger.debug("tag=%r 无匹配候选，转影子联络 fallback=%r", tag, fallback_peer_id)
            return self._fallback_to_shadow(
                fallback_peer_id or f"tag:{tag}",
                question,
                in_reply_to=in_reply_to,
                reason="no_tag_candidate",
            )

        # 2. 广播轻量 probe
        probe_responses = self._broadcast_probe(unique_candidates, question)

        if probe_responses:
            # 3a. 有响应 → 取第一个响应者定向追问
            first_responder = next(iter(probe_responses))
            logger.debug(
                "tag=%r probe 命中 %r，定向追问",
                tag,
                first_responder,
            )
            return self._targeted_ask(first_responder, question, in_reply_to=in_reply_to)

        # 3b. 无响应 → 转影子联络（fallback_peer_id）
        logger.debug(
            "tag=%r probe 无响应，转影子联络 fallback=%r",
            tag,
            fallback_peer_id,
        )
        return self._fallback_to_shadow(
            fallback_peer_id or f"tag:{tag}",
            question,
            in_reply_to=in_reply_to,
            reason="no_probe_response",
        )

    def _resolve_candidates_by_tag(self, tag: str) -> list[str]:
        """按 tag 查询匹配候选 peer_id 列表。

        Task 25 SubTask 25.2 / 25.6：
        - central 模式：调用 transport.fetch_team_members() 实时查 DB（不缓存）
        - P2P 模式：从 transport.discover_peers() 返回的 PeerInfo.capabilities 匹配
          （capabilities 由 tags_sync 刷新，未收到时为静态配置值）
        - tag 为空 → 返回空列表
        - fetch_team_members 不存在或抛异常 → 降级到 discover_peers

        Args:
            tag: 标签字符串。

        Returns:
            匹配 tag 的 peer_id 列表（顺序由 transport 决定）。
        """
        if not tag:
            return []

        # 优先调用 transport.fetch_team_members（central 模式权威源）
        fetch_fn = getattr(self.transport, "fetch_team_members", None)
        if callable(fetch_fn):
            try:
                members: list[PeerInfo] = fetch_fn()
            except AttributeError:
                # P2P 模式无此方法 → 降级到 discover_peers
                members = None
            except Exception as exc:  # noqa: BLE001
                logger.warning("fetch_team_members 失败: %s", exc)
                members = None

            if members is not None:
                return [
                    m.peer_id
                    for m in members
                    if tag in (m.capabilities or []) and m.peer_id
                ]

        # 降级：discover_peers + capabilities 匹配（P2P / hybrid）
        try:
            peers = self.transport.discover_peers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("discover_peers 失败: %s", exc)
            return []
        return [
            p.peer_id
            for p in peers
            if tag in (p.capabilities or []) and p.peer_id
        ]

    def _broadcast_probe(
        self,
        candidates: list[str],
        question: str,
        *,
        timeout_seconds: float = DEFAULT_PROBE_TIMEOUT_SECONDS,
    ) -> dict[str, str]:
        """向候选 peer 广播轻量 probe 消息，收集响应。

        Task 25 SubTask 25.7：
        - msg_type = "probe"（轻量探测，不带完整 question payload）
        - 仅向可达 peer 投递（_is_peer_reachable 过滤）
        - 收集 fetch() 中 in_reply_to 匹配 probe.event_id 的响应

        Args:
            candidates: 候选 peer_id 列表。
            question: 提问内容（仅用于探针 metadata，不要求 peer 处理）。
            timeout_seconds: 单 peer 等待响应超时（语义占位，实际由 transport.fetch 决定）。

        Returns:
            {peer_id: answer_text} 响应映射（顺序按 candidates 出现顺序）。
        """
        responses: dict[str, str] = {}
        for peer_id in candidates:
            if not self._is_peer_reachable(peer_id):
                continue
            # 构造 probe 消息
            probe_event_id = str(uuid.uuid4())
            probe_msg = self._build_message(
                peer_id,
                MSG_TYPE_PROBE,
                {"question": question, "probe": True},
                event_id=probe_event_id,
            )
            try:
                self.transport.deliver(peer_id, [probe_msg])
            except Exception as exc:  # noqa: BLE001
                logger.debug("probe deliver 失败 peer=%r: %s", peer_id, exc)
                continue
            # 拉取响应（fetch 返回的 in_reply_to 匹配 probe_event_id）
            try:
                msgs = self.transport.fetch(peer_id)
            except Exception as exc:  # noqa: BLE001
                logger.debug("probe fetch 失败 peer=%r: %s", peer_id, exc)
                continue
            for msg in msgs:
                if msg.in_reply_to == probe_event_id:
                    answer_text = str(msg.payload.get("answer", ""))
                    responses[peer_id] = answer_text
                    break  # 取第一个匹配响应
        return responses

    def _targeted_ask(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """定向追问：复用现有 ask_peer 实时路径（probe 命中后的追问）。

        Args:
            peer_id: probe 响应的 peer_id。
            question: 完整问题。
            in_reply_to: 上游回复链。

        Returns:
            realtime_answer 事件。
        """
        ask_event = self._create_ask_event(peer_id, question, in_reply_to=in_reply_to)
        self.conversation_log.append(ask_event)
        # 走在线实时路径（probe 已确认可达，缓存命中）
        return self._realtime_ask(ask_event, peer_id, question, in_reply_to=in_reply_to)

    def _fallback_to_shadow(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
        reason: str = "",
    ) -> ConversationEvent:
        """无候选 / 无响应时转影子联络。

        Task 25 SubTask 25.7 兜底路径：
        - 创建 ask 事件写入 ConversationLog 与 outbox
        - 委托 shadow_comm.ask_peer()；shadow_comm 为 None 时返回
          pending 状态的 realtime_answer（携带 reason 标记）

        Args:
            peer_id: 兜底归属 peer_id（可能为 "tag:<tag>" 占位）。
            question: 提问内容。
            in_reply_to: 上游回复链。
            reason: 降级原因（no_tag_candidate / no_probe_response）。

        Returns:
            simulated_answer 或 pending realtime_answer 事件。
        """
        ask_event = self._create_ask_event(peer_id, question, in_reply_to=in_reply_to)
        ask_event.payload["tag_routing"] = reason
        self.conversation_log.append(ask_event)
        self.mailbox.append_outbox(ask_event)

        if self.shadow_comm is None:
            # 无 shadow_comm：返回 pending 状态的 realtime_answer（携带 reason）
            answer_event = self._create_realtime_answer_event(
                peer_id,
                "",
                in_reply_to=ask_event.event_id,
            )
            answer_event.payload["status"] = "pending"
            answer_event.payload["tag_routing"] = reason
            self.conversation_log.append(answer_event)
            return answer_event

        return self.shadow_comm.ask_peer(
            peer_id,
            question,
            in_reply_to=ask_event.event_id,
        )

    # ------------------------------------------------------------------
    # 内部：可达性缓存
    # ------------------------------------------------------------------

    def _is_peer_reachable(self, peer_id: str) -> bool:
        """探测 peer 可达性（带缓存）。

        缓存有效期内的结果直接返回，过期则重新探测。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            可达返回 True，否则 False。
        """
        now = time.time()
        cached = self._reachability_cache.get(peer_id)
        if cached is not None:
            is_reachable, cached_at = cached
            if now - cached_at < self._network_check_interval_seconds:
                return is_reachable

        # 缓存过期或不存在，重新探测
        is_reachable = self.transport.is_peer_reachable(peer_id)
        self._reachability_cache[peer_id] = (is_reachable, now)
        return is_reachable

    # ------------------------------------------------------------------
    # 内部：事件与消息构造
    # ------------------------------------------------------------------

    def _create_ask_event(
        self,
        peer_id: str,
        question: str,
        *,
        in_reply_to: str = "",
    ) -> ConversationEvent:
        """创建 ask 事件。

        递增本地 vector_clock counter，事件携带递增后的版本向量副本。
        """
        # ask 时递增本地 counter
        self._local_vector_clock.increment(self.member_id)
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_ASK,
            peer_id=peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=VectorClock.from_dict(self._local_vector_clock.to_dict()),
            payload={"question": question},
            in_reply_to=in_reply_to,
            degraded=False,
            realtime=False,
        )

    def _create_realtime_answer_event(
        self,
        peer_id: str,
        answer: str,
        *,
        in_reply_to: str = "",
        vector_clock: VectorClock | None = None,
    ) -> ConversationEvent:
        """创建 realtime_answer 事件（realtime=True）。

        Args:
            peer_id: 对方 peer_id。
            answer: 回答内容。
            in_reply_to: 关联的 ask 事件 ID。
            vector_clock: 自定义版本向量（None 时使用本地当前向量副本）。
        """
        vc = (
            vector_clock
            if vector_clock is not None
            else VectorClock.from_dict(self._local_vector_clock.to_dict())
        )
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type=EVENT_REALTIME_ANSWER,
            peer_id=peer_id,
            timestamp=self._utcnow_iso(),
            vector_clock=vc,
            payload={"answer": answer},
            in_reply_to=in_reply_to,
            degraded=False,
            realtime=True,
        )

    def _build_message(
        self,
        peer_id: str,
        msg_type: str,
        payload: dict[str, Any],
        *,
        event_id: str = "",
        in_reply_to: str = "",
    ) -> Message:
        """构建 transport 层 Message。

        Args:
            peer_id: 接收方 peer_id。
            msg_type: 消息类型（ask / answer / share_asset / sync / heartbeat）。
            payload: 消息内容。
            event_id: 关联的 ConversationEvent ID（用于幂等去重与回复链匹配）。
            in_reply_to: 回复链。

        Returns:
            Message 实例。
        """
        return Message(
            message_id=str(uuid.uuid4()),
            event_id=event_id,
            sender_id=self.member_id,
            recipient_id=peer_id,
            msg_type=msg_type,
            payload=payload,
            timestamp=self._utcnow_iso(),
            in_reply_to=in_reply_to,
        )

    def _utcnow_iso(self) -> str:
        """当前 UTC ISO 时间戳。"""
        return datetime.now(timezone.utc).isoformat()


__all__ = [
    "PeerComm",
    "ShadowCommProtocol",
]
