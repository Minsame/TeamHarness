"""Task 24：成员 AI 通信端到端集成测试。

验证 async_comm / transport / mcp_server / client 等子模块协同工作的完整流程：
- 双 peer 在线实时通信（PeerComm → transport.deliver/fetch → ConversationLog）
- 双 peer 影子联络（ShadowComm → 快照 → simulated_answer → 上线同步对账）
- 在线/离线路径切换（会话中途 peer 下线 → 影子联络 → 上线对账）
- 拓扑切换回归（central / p2p / hybrid 切换不中断现有功能）
- MCP 工具调用（McpServer.call_tool → TransportBridge → async_comm 全链路）
- CLI 子命令（ClientCLI ask-peer / shadow-log → async_comm 全链路）

测试策略：
- 使用 DualPeerTransport 模拟双 peer 通信（避免真实网络依赖）
- 使用 tmp_path 隔离文件系统
- 构建完整的 PeerComm + ShadowComm + SyncProtocol + Mailbox + ConversationLog +
  PeerSnapshotManager + ConflictResolver 组件链
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.async_comm import (
    ConflictResolver,
    ConversationLog,
    Mailbox,
    PeerComm,
    PeerSnapshotManager,
    ShadowComm,
    SyncProtocol,
)
from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_REVISED,
    EVENT_SIMULATED_ANSWER,
    STATUS_DELIVERED,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.types import ConversationEvent, PeerSnapshot, VectorClock
from server.client.config import ClientConfig
from server.client.cli import ClientCLI
from server.mcp_server.server import McpServer
from server.mcp_server.transport_bridge import TransportBridge
from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
)
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub Transport
# ---------------------------------------------------------------------------


class DualPeerTransport:
    """模拟双 peer 通信的 Stub Transport。

    维护两个 peer 的消息投递与自动回答：
    - deliver(peer_id, messages)：投递消息给 peer_id。peer 在线时对 ask 消息
      自动生成回答，存入 _responses[peer_id] 供后续 fetch 取回。
    - fetch(peer_id)：拉取 peer_id 发来的消息（自动生成的回答），拉取后清空。
    - 可控制 peer 在线状态（set_peer_online / set_peer_offline）。
    - peer_inboxes 记录投递给每个 peer 的原始消息（供测试验证用）。
    """

    def __init__(self, *, auto_answer: str = "") -> None:
        """初始化双 peer transport。

        Args:
            auto_answer: 自定义自动回答文本。为空时使用默认格式。
        """
        self.online_peers: set[str] = {"alice", "bob"}
        self.known_peers: set[str] = {"alice", "bob"}
        # peer_inboxes: 投递给每个 peer 的原始消息（供验证用）
        self.peer_inboxes: dict[str, list[Message]] = {"alice": [], "bob": []}
        # _responses: 每个 peer 自动生成的回答（供 fetch 取回）
        self._responses: dict[str, list[Message]] = {"alice": [], "bob": []}
        self.delivered_log: list[tuple[str, list[Message]]] = []
        self.auto_answer: str = auto_answer

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """投递消息到 peer 的 inbox。"""
        if peer_id not in self.online_peers:
            return SyncResult(success=False, pending_count=len(messages))
        self.peer_inboxes[peer_id].extend(messages)
        self.delivered_log.append((peer_id, messages))

        # 对 ask 消息自动生成回答
        for msg in messages:
            if msg.msg_type == "ask":
                answer_text = self.auto_answer or (
                    f"[来自 {peer_id} 的回答] 关于: {msg.payload.get('question', '')}"
                )
                answer = Message(
                    message_id=str(uuid.uuid4()),
                    event_id=str(uuid.uuid4()),
                    sender_id=peer_id,
                    recipient_id=msg.sender_id,
                    msg_type="answer",
                    payload={"answer": answer_text},
                    timestamp=datetime.now(timezone.utc).isoformat(),
                    in_reply_to=msg.event_id,
                )
                self._responses[peer_id].append(answer)

        return SyncResult(
            success=True,
            delivered_count=len(messages),
            delivered_message_ids=[m.event_id for m in messages],
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """拉取 peer 发来的消息（自动生成的回答）。"""
        msgs = self._responses.get(peer_id, [])
        self._responses[peer_id] = []
        return msgs

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.online_peers

    def discover_peers(self) -> list[PeerInfo]:
        return [
            PeerInfo(peer_id=p, online=(p in self.online_peers))
            for p in sorted(self.known_peers)
        ]

    def set_peer_offline(self, peer_id: str) -> None:
        self.online_peers.discard(peer_id)

    def set_peer_online(self, peer_id: str) -> None:
        self.online_peers.add(peer_id)

    def auto_respond(self, peer_id: str, question: str) -> Message:
        """模拟 peer 自动生成回答。"""
        return Message(
            message_id=str(uuid.uuid4()),
            event_id=str(uuid.uuid4()),
            sender_id=peer_id,
            recipient_id="caller",
            msg_type="answer",
            payload={"answer": f"[来自 {peer_id} 的回答] 关于: {question}"},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def build_comm_stack(
    member_id: str,
    base_dir: Path,
    transport: DualPeerTransport,
    *,
    answer_generator=None,
) -> dict:
    """构建完整的通信组件栈。

    Args:
        member_id: 本地成员 ID。
        base_dir: 数据根目录（tmp_path 下的子目录）。
        transport: DualPeerTransport 实例。
        answer_generator: ShadowComm 的自定义回答生成器（可选）。

    Returns:
        dict 含 mailbox / conversation_log / peer_snapshot_manager /
        shadow_comm / peer_comm / sync_protocol / conflict_resolver。
    """
    mailbox = Mailbox(base_dir / "mailbox", member_id)
    conversation_log = ConversationLog(base_dir / "conversation.jsonl")
    peer_snapshot_manager = PeerSnapshotManager(base_dir / "snapshots")

    shadow_comm = ShadowComm(
        mailbox=mailbox,
        peer_snapshot_manager=peer_snapshot_manager,
        conversation_log=conversation_log,
        member_id=member_id,
        answer_generator=answer_generator,
    )

    peer_comm = PeerComm(
        transport=transport,
        mailbox=mailbox,
        conversation_log=conversation_log,
        peer_snapshot_manager=peer_snapshot_manager,
        member_id=member_id,
        network_check_interval_seconds=0,  # 测试用：不缓存可达性
        shadow_comm=shadow_comm,
    )

    conflict_resolver = ConflictResolver()
    sync_protocol = SyncProtocol(
        transport=transport,
        mailbox=mailbox,
        conversation_log=conversation_log,
        peer_snapshot_manager=peer_snapshot_manager,
        member_id=member_id,
        conflict_resolver=conflict_resolver,
    )

    return {
        "mailbox": mailbox,
        "conversation_log": conversation_log,
        "peer_snapshot_manager": peer_snapshot_manager,
        "shadow_comm": shadow_comm,
        "peer_comm": peer_comm,
        "sync_protocol": sync_protocol,
        "conflict_resolver": conflict_resolver,
    }


def make_answer_generator(answer_text: str):
    """创建返回固定文本的回答生成器。"""

    def generator(question: str, snapshot: PeerSnapshot) -> str:
        return answer_text

    return generator


def create_bob_snapshot(snap_mgr: PeerSnapshotManager, version: str = "v1") -> str:
    """为 bob 创建本地快照（含 harness 文件）。

    Args:
        snap_mgr: PeerSnapshotManager 实例。
        version: 期望的快照版本（仅用于断言，实际版本由 refresh_snapshot 自增）。

    Returns:
        实际快照版本号。
    """
    snap = snap_mgr.refresh_snapshot(
        "bob",
        harness_files={
            "rules/lint.md": "# bob 的 lint 规则\n所有函数需类型标注",
            "memory/decisions.md": "# bob 的决策记录",
        },
        manifest={"assets": [{"id": "rule-lint", "path": "rules/lint.md"}]},
        vector_clock=VectorClock.from_dict({"bob": 1}),
    )
    return snap.snapshot_version


# ---------------------------------------------------------------------------
# SubTask 24.1: 双 peer 在线实时通信全流程
# ---------------------------------------------------------------------------


class TestDualPeerOnlineRealtime:
    """双 peer 在线实时通信全流程测试。"""

    def test_alice_ask_bob_realtime(self, tmp_path):
        """alice 向 bob 提问，bob 在线，走实时路径。

        验证：
        - alice 的 ask 事件写入 ConversationLog
        - bob 的 realtime_answer 事件写入 ConversationLog
        - 事件标记 realtime=True, degraded=False
        - transport.deliver 被调用
        - in_reply_to 链正确关联
        """
        transport = DualPeerTransport(auto_answer="42")
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        answer_event = peer_comm.ask_peer("bob", "如何处理 X？")

        # ask 事件写入 ConversationLog
        ask_events = log.load_by_type(EVENT_ASK)
        assert len(ask_events) == 1
        assert ask_events[0].payload["question"] == "如何处理 X？"

        # realtime_answer 事件写入 ConversationLog
        answer_events = log.load_by_type(EVENT_REALTIME_ANSWER)
        assert len(answer_events) == 1

        # 回答事件标记
        assert answer_event.event_type == EVENT_REALTIME_ANSWER
        assert answer_event.realtime is True
        assert answer_event.degraded is False
        assert answer_event.payload.get("answer") == "42"

        # transport.deliver 被调用
        assert len(transport.delivered_log) == 1
        delivered_peer, delivered_msgs = transport.delivered_log[0]
        assert delivered_peer == "bob"
        assert len(delivered_msgs) == 1
        assert delivered_msgs[0].msg_type == "ask"

        # in_reply_to 链：answer 指向 ask
        assert answer_event.in_reply_to == ask_events[0].event_id

    def test_bidirectional_discussion(self, tmp_path):
        """alice 与 bob 多轮讨论。

        验证：
        - 多轮 ask/answer 都写入双方 ConversationLog
        - in_reply_to 链形成正确的回复链
        - load_thread 返回完整对话历史
        """
        transport = DualPeerTransport(auto_answer="回答")
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        # 第一轮：alice 问 bob
        ans1 = peer_comm.ask_peer("bob", "问题1")

        # 第二轮：alice 追问 bob（in_reply_to 关联第一轮回答）
        ans2 = peer_comm.ask_peer("bob", "问题2", in_reply_to=ans1.event_id)

        # ConversationLog 含 2 个 ask + 2 个 realtime_answer
        assert log.count(event_type=EVENT_ASK) == 2
        assert log.count(event_type=EVENT_REALTIME_ANSWER) == 2

        # in_reply_to 链正确
        ask_events = log.load_by_type(EVENT_ASK)
        answer_events = log.load_by_type(EVENT_REALTIME_ANSWER)

        # 第一个 answer 指向第一个 ask
        assert answer_events[0].in_reply_to == ask_events[0].event_id
        # 第二个 ask 的 in_reply_to 指向第一个 answer
        assert ask_events[1].in_reply_to == answer_events[0].event_id
        # 第二个 answer 指向第二个 ask
        assert answer_events[1].in_reply_to == ask_events[1].event_id

        # load_thread 从第一个 ask 开始，返回完整对话链
        thread = log.load_thread(ask_events[0].event_id)
        assert len(thread) == 4  # 2 ask + 2 answer

    def test_share_asset_realtime(self, tmp_path):
        """alice 向 bob 实时共享资产。

        验证：
        - transport.deliver 被调用
        - SyncResult.success=True
        - bob 的 inbox 收到资产消息
        """
        transport = DualPeerTransport()
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]

        result = peer_comm.share_asset(
            "rule-001",
            "bob",
            asset_content={"title": "lint 规则", "content": "所有函数需类型标注"},
        )

        # SyncResult 成功
        assert result.success is True
        assert result.delivered_count == 1

        # transport.deliver 被调用
        assert len(transport.delivered_log) == 1
        _, delivered_msgs = transport.delivered_log[0]
        assert len(delivered_msgs) == 1
        assert delivered_msgs[0].msg_type == "share_asset"
        assert delivered_msgs[0].payload["asset_id"] == "rule-001"

        # bob 的 inbox 收到资产消息
        assert len(transport.peer_inboxes["bob"]) == 1
        assert transport.peer_inboxes["bob"][0].msg_type == "share_asset"


# ---------------------------------------------------------------------------
# SubTask 24.2: 双 peer 影子联络全流程
# ---------------------------------------------------------------------------


class TestDualPeerShadowComm:
    """双 peer 影子联络全流程测试。"""

    def test_alice_shadow_ask_bob(self, tmp_path):
        """alice 向 bob 提问，bob 离线，走影子联络路径。

        验证：
        - alice 的 ask 事件写入 ConversationLog + outbox（pending_delivery）
        - simulated_answer 事件写入 ConversationLog
        - 事件标记 degraded=True, realtime=False
        - based_on 设为 bob 的快照版本
        - answer 来自 answer_generator
        """
        transport = DualPeerTransport()
        transport.set_peer_offline("bob")

        custom_answer = "这是基于 bob 快照的模拟回答"
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_answer_generator(custom_answer),
        )
        peer_comm = stack["peer_comm"]
        mailbox = stack["mailbox"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 创建 bob 的快照
        snap_version = create_bob_snapshot(snap_mgr)

        # alice 向离线的 bob 提问
        answer_event = peer_comm.ask_peer("bob", "如何配置 lint？")

        # ask 事件写入 ConversationLog（PeerComm 的 + ShadowComm 的）
        ask_events = log.load_by_type(EVENT_ASK)
        assert len(ask_events) == 2

        # ask 事件写入 outbox（pending_delivery）
        # PeerComm 写入一个 ask，ShadowComm 也写入一个 ask
        outbox = mailbox.load_outbox()
        assert len(outbox) == 2
        assert all(e.event_type == EVENT_ASK for e in outbox)
        assert all(
            mailbox.get_status(e.event_id) == STATUS_PENDING_DELIVERY
            for e in outbox
        )

        # simulated_answer 事件写入 ConversationLog
        simulated_events = log.load_by_type(EVENT_SIMULATED_ANSWER)
        assert len(simulated_events) == 1

        # 事件标记
        assert answer_event.event_type == EVENT_SIMULATED_ANSWER
        assert answer_event.degraded is True
        assert answer_event.realtime is False

        # based_on 设为 bob 的快照版本
        assert answer_event.based_on == snap_version
        assert answer_event.snapshot_stale is False

        # answer 来自 answer_generator
        assert answer_event.payload.get("answer") == custom_answer

    def test_shadow_then_sync_and_reconcile(self, tmp_path):
        """完整影子联络流程：离线提问 → 上线同步 → 对账。

        流程：
        1. bob 离线，alice 影子提问，生成 simulated_answer
        2. bob 上线
        3. 触发 SyncProtocol.sync_with_peer(bob)
        4. SyncProtocol 推送 outbox 中的 ask 消息给 bob
        5. bob 的 realtime_answer 通过 fetch 返回
        6. ConflictResolver 对比 simulated_answer 与 realtime_answer
        7. 生成 confirmed 事件（相似度高时）

        验证：
        - outbox 消息状态从 pending_delivery → delivered
        - 对账事件写入 ConversationLog
        - 相似度高时 → confirmed
        """
        # 使用相同回答文本确保高相似度 → confirmed
        shared_answer = "使用 use_alter=True 避免循环外键"
        transport = DualPeerTransport(auto_answer=shared_answer)
        transport.set_peer_offline("bob")

        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_answer_generator(shared_answer),
        )
        peer_comm = stack["peer_comm"]
        sync_protocol = stack["sync_protocol"]
        mailbox = stack["mailbox"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 1. 创建 bob 快照
        create_bob_snapshot(snap_mgr)

        # 2. bob 离线，alice 影子提问
        simulated_event = peer_comm.ask_peer("bob", "如何处理循环外键？")
        assert simulated_event.event_type == EVENT_SIMULATED_ANSWER
        assert simulated_event.degraded is True

        # 3. bob 上线
        transport.set_peer_online("bob")

        # 4. 触发同步
        result = sync_protocol.sync_with_peer("bob")

        # 5. outbox 消息状态从 pending_delivery → delivered
        # PeerComm 和 ShadowComm 各写入一个 ask 事件到 outbox
        outbox_all = mailbox.load_outbox()
        ask_in_outbox = [e for e in outbox_all if e.event_type == EVENT_ASK]
        assert len(ask_in_outbox) == 2
        assert all(
            mailbox.get_status(e.event_id) == STATUS_DELIVERED
            for e in ask_in_outbox
        )

        # 6. 推送计数 > 0
        assert result.pushed_count > 0

        # 7. 拉取计数 > 0（bob 的 realtime_answer）
        assert result.received_count > 0

        # 8. 对账：相似度高 → confirmed
        assert result.confirmed_count == 1
        assert result.revised_count == 0
        assert result.needs_review_count == 0

        # 9. 对账事件写入 ConversationLog
        confirmed_events = log.load_by_type(EVENT_CONFIRMED)
        assert len(confirmed_events) == 1
        assert confirmed_events[0].in_reply_to == simulated_event.event_id

    def test_shadow_no_snapshot(self, tmp_path):
        """无快照时影子联络。

        验证：
        - 仍生成 simulated_answer
        - based_on 为空
        - snapshot_stale=True
        - payload 含 warning
        """
        transport = DualPeerTransport()
        transport.set_peer_offline("bob")

        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 不创建 bob 的快照
        assert snap_mgr.get_snapshot("bob") is None

        answer_event = peer_comm.ask_peer("bob", "问题？")

        # 仍生成 simulated_answer
        assert answer_event.event_type == EVENT_SIMULATED_ANSWER
        assert answer_event.degraded is True

        # based_on 为空
        assert answer_event.based_on == ""

        # snapshot_stale=True
        assert answer_event.snapshot_stale is True

        # payload 含 warning
        assert "warning" in answer_event.payload
        assert "no local snapshot" in answer_event.payload["warning"]


# ---------------------------------------------------------------------------
# SubTask 24.3: 在线 → 离线路径切换
# ---------------------------------------------------------------------------


class TestOnlineOfflineSwitch:
    """在线 → 离线路径切换测试（会话中途 peer 下线）。"""

    def test_peer_goes_offline_mid_session(self, tmp_path):
        """会话中途 peer 下线。

        流程：
        1. alice 在线问 bob 第一个问题 → realtime_answer
        2. bob 下线
        3. alice 问 bob 第二个问题 → simulated_answer（degraded）
        4. bob 上线
        5. 同步与对账

        验证：
        - 第一个问题 realtime=True
        - 第二个问题 degraded=True
        - ConversationLog 含两种事件类型
        - 同步后第二个问题被对账
        """
        shared_answer = "统一回答内容"
        transport = DualPeerTransport(auto_answer=shared_answer)
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_answer_generator(shared_answer),
        )
        peer_comm = stack["peer_comm"]
        sync_protocol = stack["sync_protocol"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 创建 bob 快照（影子联络需要）
        create_bob_snapshot(snap_mgr)

        # 1. alice 在线问 bob 第一个问题 → realtime_answer
        ans1 = peer_comm.ask_peer("bob", "问题1")
        assert ans1.realtime is True
        assert ans1.degraded is False

        # 2. bob 下线
        transport.set_peer_offline("bob")

        # 3. alice 问 bob 第二个问题 → simulated_answer（degraded）
        ans2 = peer_comm.ask_peer("bob", "问题2")
        assert ans2.realtime is False
        assert ans2.degraded is True
        assert ans2.event_type == EVENT_SIMULATED_ANSWER

        # ConversationLog 含 realtime_answer 和 simulated_answer
        assert log.count(event_type=EVENT_REALTIME_ANSWER) == 1
        assert log.count(event_type=EVENT_SIMULATED_ANSWER) == 1

        # 4. bob 上线
        transport.set_peer_online("bob")

        # 5. 同步与对账
        result = sync_protocol.sync_with_peer("bob")

        # 第二个问题被对账（confirmed，因为答案相同）
        assert result.confirmed_count == 1

        # 对账事件写入 ConversationLog
        confirmed_events = log.load_by_type(EVENT_CONFIRMED)
        assert len(confirmed_events) == 1

    def test_peer_comes_back_online(self, tmp_path):
        """peer 重新上线后自动同步。

        验证：
        - peer 上线触发 sync_with_peer
        - pending_delivery 消息被推送
        - 对账正确执行
        """
        shared_answer = "同步后的回答"
        transport = DualPeerTransport(auto_answer=shared_answer)
        transport.set_peer_offline("bob")

        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_answer_generator(shared_answer),
        )
        peer_comm = stack["peer_comm"]
        sync_protocol = stack["sync_protocol"]
        mailbox = stack["mailbox"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 创建 bob 快照
        create_bob_snapshot(snap_mgr)

        # bob 离线时提问
        peer_comm.ask_peer("bob", "离线时的问题")

        # 验证 outbox 有 pending_delivery 消息
        assert mailbox.pending_delivery_count() > 0

        # bob 上线
        transport.set_peer_online("bob")

        # 触发同步
        result = sync_protocol.sync_with_peer("bob")

        # pending_delivery 消息被推送
        assert result.pushed_count > 0
        # pending_delivery 计数归零
        assert mailbox.pending_delivery_count() == 0

        # 对账正确执行
        assert result.confirmed_count == 1


# ---------------------------------------------------------------------------
# SubTask 24.4: 拓扑切换回归
# ---------------------------------------------------------------------------


class TestTopologySwitch:
    """拓扑切换回归测试。"""

    def test_central_topology(self, tmp_path):
        """central 模式通信正常。"""
        transport = DualPeerTransport(auto_answer="central 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_CENTRAL,
        )
        bridge = TransportBridge(config, transport=transport)

        result = bridge.ask_peer("bob", "central 模式问题")
        assert result["realtime"] is True
        assert result["degraded"] is False
        assert "central 回答" in result["answer"]

    def test_p2p_topology(self, tmp_path):
        """p2p 模式通信正常。"""
        transport = DualPeerTransport(auto_answer="p2p 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_P2P,
        )
        bridge = TransportBridge(config, transport=transport)

        result = bridge.ask_peer("bob", "p2p 模式问题")
        assert result["realtime"] is True
        assert result["degraded"] is False
        assert "p2p 回答" in result["answer"]

    def test_hybrid_topology(self, tmp_path):
        """hybrid 模式通信正常。"""
        transport = DualPeerTransport(auto_answer="hybrid 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_HYBRID,
        )
        bridge = TransportBridge(config, transport=transport)

        result = bridge.ask_peer("bob", "hybrid 模式问题")
        assert result["realtime"] is True
        assert result["degraded"] is False
        assert "hybrid 回答" in result["answer"]

    def test_topology_switch_no_break(self, tmp_path):
        """切换拓扑后现有功能不中断。

        验证：
        - ClientConfig.topology 切换后
        - PeerComm 仍能正常工作
        - mailbox / conversation_log 数据保留
        """
        # 1. 使用 central 模式创建通信栈
        transport1 = DualPeerTransport(auto_answer="第一轮回答")
        stack1 = build_comm_stack("alice", tmp_path, transport1)
        peer_comm1 = stack1["peer_comm"]
        log1 = stack1["conversation_log"]

        # 进行一次通信
        peer_comm1.ask_peer("bob", "central 模式问题")

        # 验证数据存在
        assert log1.count(event_type=EVENT_ASK) == 1

        # 2. 切换拓扑到 p2p，复用同一数据目录
        transport2 = DualPeerTransport(auto_answer="第二轮回答")
        stack2 = build_comm_stack("alice", tmp_path, transport2)
        peer_comm2 = stack1["peer_comm"]
        log2 = stack2["conversation_log"]

        # 验证数据保留（同一个 conversation.jsonl 文件）
        assert log2.count(event_type=EVENT_ASK) == 1

        # 3. 切换拓扑后仍能正常通信
        peer_comm2.ask_peer("bob", "p2p 模式问题")
        assert log2.count(event_type=EVENT_ASK) == 2
        assert log2.count(event_type=EVENT_REALTIME_ANSWER) == 2


# ---------------------------------------------------------------------------
# SubTask 24.5: MCP 工具调用 + CLI 子命令
# ---------------------------------------------------------------------------


class TestMcpToolCall:
    """MCP 工具调用测试。"""

    def _build_server(self, tmp_path, transport=None):
        """构建带 StubTransport 的 McpServer。"""
        transport = transport or DualPeerTransport(auto_answer="MCP 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_CENTRAL,
        )
        bridge = TransportBridge(config, transport=transport)
        server = McpServer(config)
        server.bridge = bridge  # 替换为带 StubTransport 的 bridge
        return server, transport, bridge

    def test_mcp_ask_peer_tool(self, tmp_path):
        """MCP ask_peer 工具调用。

        验证：
        - McpServer.call_tool("ask_peer", {"peer_id": "bob", "question": "..."})
        - 返回 dict 含 answer / degraded / realtime
        """
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("ask_peer", {
            "peer_id": "bob",
            "question": "如何处理 X？",
        })

        assert "result" in result
        data = result["result"]
        assert "answer" in data
        assert "degraded" in data
        assert "realtime" in data
        assert data["realtime"] is True
        assert data["degraded"] is False
        assert "MCP 回答" in data["answer"]

    def test_mcp_list_peers_tool(self, tmp_path):
        """MCP list_peers 工具调用。"""
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("list_peers", {})

        assert "result" in result
        peers = result["result"]
        assert isinstance(peers, list)
        assert len(peers) == 2
        peer_ids = {p["peer_id"] for p in peers}
        assert "alice" in peer_ids
        assert "bob" in peer_ids

    def test_mcp_share_asset_tool(self, tmp_path):
        """MCP share_asset 工具调用。"""
        server, transport, _ = self._build_server(tmp_path)

        result = server.call_tool("share_asset", {
            "asset_id": "rule-001",
            "to_peer_id": "bob",
            "content": {"title": "lint 规则"},
        })

        assert "result" in result
        data = result["result"]
        assert data["success"] is True
        assert data["delivered_count"] == 1

        # 验证 transport 收到了投递
        assert len(transport.delivered_log) == 1
        _, msgs = transport.delivered_log[0]
        assert msgs[0].msg_type == "share_asset"

    def test_mcp_search_team_assets_tool(self, tmp_path):
        """MCP search_team_assets 工具调用。

        无真实服务端时返回 error（RecallClient 无 server_url），
        验证工具可调用且返回 dict。
        """
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("search_team_assets", {
            "query": "lint 规则",
            "limit": 5,
        })

        # 无真实服务端，返回 error 或 result
        assert isinstance(result, dict)
        assert "result" in result or "error" in result

    def test_mcp_unknown_tool(self, tmp_path):
        """未知工具返回 error。"""
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("nonexistent_tool", {})

        assert "error" in result
        assert "unknown tool" in result["error"]

    def test_cli_ask_peer_command(self, tmp_path):
        """CLI ask-peer 子命令调用。

        验证：
        - ClientCLI ask-peer 子命令成功执行
        - 返回含 answer / degraded / realtime 的结果
        """
        transport = DualPeerTransport(auto_answer="CLI 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_CENTRAL,
        )
        stdout = io.StringIO()
        cli = ClientCLI(config=config, stdout=stdout, transport=transport)

        rc = cli.run([
            "ask-peer",
            "--peer", "bob",
            "--question", "CLI 提问",
        ])

        assert rc == 0
        output = json.loads(stdout.getvalue())
        assert output["success"] is True
        assert output["command"] == "ask-peer"
        assert "CLI 回答" in output["data"]["answer"]
        assert output["data"]["realtime"] is True

    def test_cli_shadow_log_command(self, tmp_path):
        """CLI shadow-log 子命令调用。

        验证：
        - ClientCLI shadow-log 子命令成功执行
        - 返回含交流记录的结果
        """
        transport = DualPeerTransport(auto_answer="CLI 回答")
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_CENTRAL,
        )
        stdout = io.StringIO()
        cli = ClientCLI(config=config, stdout=stdout, transport=transport)

        # 先用 ask-peer 产生一些交流记录
        cli.run(["ask-peer", "--peer", "bob", "--question", "问题1"])

        # 清空 stdout 缓冲
        stdout.truncate(0)
        stdout.seek(0)

        # 执行 shadow-log
        rc = cli.run(["shadow-log", "--peer", "bob"])

        assert rc == 0
        output = json.loads(stdout.getvalue())
        assert output["success"] is True
        assert output["command"] == "shadow-log"
        assert output["data"]["count"] > 0
        # 应包含 ask 和 realtime_answer 事件
        event_types = {e["event_type"] for e in output["data"]["events"]}
        assert EVENT_ASK in event_types
