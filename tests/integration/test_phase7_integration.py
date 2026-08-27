"""Task 29：阶段七端到端集成测试。

验证阶段七（Task 25-28）各功能模块的协同工作：
- SubTask 29.1：按 tag 路由 + 多候选混合策略端到端（central / P2P / 影子兜底）
- SubTask 29.2：对话持久化与恢复（在线讨论 → peer 下线 paused → 上线 resumed）
- SubTask 29.3：对话超时断开 + 自动恢复（daemon 超时检测 → timeout_disconnect → resumed）
- SubTask 29.4：梦境提炼集成（ConversationLog → Light/REM/Deep → tags 回灌）
- SubTask 29.5：MCP ask_peer 按 tag 路由 + resume_conversation 工具测试

测试策略：
- 使用 MultiPeerStubTransport 模拟多 peer 通信（支持 tag 路由 / probe / 在线切换）
- 使用 StubTagsFeedbackClient 验证 tags 回灌
- 使用 tmp_path 隔离文件系统
- 时间 mock：超时测试用旧时间戳 + 短 timeout，不实际等待
- 不重复 test_tag_routing / test_conversation_persistence / test_conversation_distill /
  test_mcp_server 已覆盖的单元测试，只补端到端集成场景
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

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
    CONV_STATE_ACTIVE,
    CONV_STATE_PAUSED,
    CONV_STATE_RESUMED,
    CONV_STATE_TIMEOUT_DISCONNECT,
    EVENT_ASK,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_SIMULATED_ANSWER,
    MSG_TYPE_PROBE,
    STATUS_PENDING_DELIVERY,
)
from server.async_comm.types import ConversationEvent, PeerSnapshot, VectorClock
from server.client.config import ClientConfig
from server.client.daemon import ClientDaemon
from server.distill_personal.budget import BudgetManager, PendingCandidateStore
from server.distill_personal.metrics import SignalReporter
from server.distill_personal.personal_distill import PersonalDistill
from server.mcp_server.server import McpServer
from server.mcp_server.transport_bridge import TransportBridge
from server.transport.protocol import TOPOLOGY_CENTRAL, TOPOLOGY_P2P
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub Transport（多 peer，支持 tag 路由 / probe / 在线切换）
# ---------------------------------------------------------------------------


class MultiPeerStubTransport:
    """多 peer Stub 传输层。

    支持：
    - 多 peer 在线/离线切换（set_peer_online / set_peer_offline）
    - fetch_team_members（central 模式 tag 路由权威源；为 None 时触发降级）
    - discover_peers 带 capabilities（P2P 模式 tag 路由）
    - probe 消息自动响应（probe_answers 预置）
    - ask 消息自动回答（auto_answers 预置）
    - fetch 后清空响应队列（避免重复返回）

    member_id 不参与 discover_peers 返回（本成员不是候选）。
    """

    def __init__(
        self,
        *,
        member_id: str = "alice",
        online_peers: set[str] | None = None,
        team_members: list[PeerInfo] | None = None,
        discovered_peers: list[PeerInfo] | None = None,
        probe_answers: dict[str, str] | None = None,
        auto_answers: dict[str, str] | None = None,
    ) -> None:
        self.member_id = member_id
        self.online_peers = online_peers or set()
        # team_members 为 None 表示 P2P 模式（无中心 API），触发降级到 discover_peers
        self._team_members = team_members
        self._discovered_peers = discovered_peers or []
        self._probe_answers = probe_answers or {}
        self._auto_answers = auto_answers or {}
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.fetch_calls: list[str] = []
        self._responses: dict[str, list[Message]] = {}

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """投递消息给 peer。peer 在线时为 ask/probe 自动生成响应。"""
        self.delivered_messages.append((peer_id, messages))
        if peer_id not in self.online_peers:
            return SyncResult(success=False, pending_count=len(messages))
        for msg in messages:
            if msg.msg_type == MSG_TYPE_PROBE:
                answer_text = self._probe_answers.get(peer_id, "")
                if answer_text:
                    self._responses.setdefault(peer_id, []).append(
                        self._make_answer(peer_id, msg, answer_text)
                    )
            elif msg.msg_type == "ask":
                answer_text = self._auto_answers.get(peer_id, "")
                if answer_text:
                    self._responses.setdefault(peer_id, []).append(
                        self._make_answer(peer_id, msg, answer_text)
                    )
        return SyncResult(
            success=True,
            delivered_count=len(messages),
            delivered_message_ids=[m.event_id for m in messages],
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """拉取 peer 的响应消息（取后清空）。"""
        self.fetch_calls.append(peer_id)
        msgs = self._responses.get(peer_id, [])
        self._responses[peer_id] = []
        return msgs

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.online_peers

    def discover_peers(self) -> list[PeerInfo]:
        return list(self._discovered_peers)

    def fetch_team_members(self) -> list[PeerInfo] | None:
        """返回 team_members（None 触发 P2P 降级到 discover_peers）。"""
        if self._team_members is None:
            return None
        return list(self._team_members)

    def set_peer_online(self, peer_id: str) -> None:
        self.online_peers.add(peer_id)

    def set_peer_offline(self, peer_id: str) -> None:
        self.online_peers.discard(peer_id)

    @staticmethod
    def _make_answer(peer_id: str, orig_msg: Message, answer_text: str) -> Message:
        return Message(
            message_id=str(uuid.uuid4()),
            event_id=str(uuid.uuid4()),
            sender_id=peer_id,
            recipient_id=orig_msg.sender_id,
            msg_type="answer",
            payload={"answer": answer_text},
            timestamp=datetime.now(timezone.utc).isoformat(),
            in_reply_to=orig_msg.event_id,
        )


# ---------------------------------------------------------------------------
# Stub Tags 回灌客户端
# ---------------------------------------------------------------------------


class StubTagsFeedbackClient:
    """Tags 回灌客户端 Stub，记录调用供测试验证。"""

    def __init__(self, *, return_value: bool = True) -> None:
        self.feedback_calls: list[tuple[str, list[str]]] = []
        self.return_value = return_value

    def feedback_tags(self, member_id: str, new_tags: list[str]) -> bool:
        self.feedback_calls.append((member_id, list(new_tags)))
        return self.return_value


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def build_comm_stack(
    member_id: str,
    base_dir: Path,
    transport: MultiPeerStubTransport,
    *,
    answer_generator=None,
) -> dict[str, Any]:
    """构建完整的通信组件栈（PeerComm + ShadowComm + SyncProtocol）。"""
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
        network_check_interval_seconds=0,
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


def make_fixed_answer_generator(answer_text: str):
    """创建返回固定文本的回答生成器（ShadowComm 用）。"""

    def generator(question: str, snapshot: PeerSnapshot) -> str:
        return answer_text

    return generator


def make_event(
    *,
    event_id: str | None = None,
    event_type: str = EVENT_ASK,
    peer_id: str = "bob",
    timestamp: str | None = None,
    payload: dict | None = None,
    in_reply_to: str = "",
    degraded: bool = False,
    realtime: bool = False,
    conversation_state: str = CONV_STATE_ACTIVE,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        vector_clock=VectorClock(),
        payload=payload if payload is not None else {},
        in_reply_to=in_reply_to,
        degraded=degraded,
        realtime=realtime,
        conversation_state=conversation_state,
    )


def make_llm_returning_asset(tags: list[str] | None = None):
    """返回产出资产的 LLM stub（含指定 tags）。"""

    asset_tags = tags if tags is not None else ["运维专家", "账号管理"]

    class _StubLLM:
        def chat(self, messages, *, schema=None, **kw):
            return {
                "content": json.dumps({
                    "skip": False,
                    "asset": {
                        "title": "提炼规则",
                        "content": "提交前必须跑 lint，禁止跳过",
                        "tags": asset_tags,
                        "rationale": "用户反复强调",
                    },
                    "confidence": 0.9,
                }),
                "usage": {"total_tokens": 200},
            }

    return _StubLLM()


def make_daemon_config(tmp_path: Path, *, member_id: str = "alice") -> ClientConfig:
    """构造测试用 ClientConfig。"""
    return ClientConfig(
        repo_root=str(tmp_path),
        member_id=member_id,
        network_check_interval_seconds=60,
    )


def inject_daemon_components(
    daemon: ClientDaemon,
    tmp_path: Path,
    *,
    transport: MultiPeerStubTransport | None = None,
    member_id: str = "alice",
) -> dict[str, Any]:
    """为 daemon 注入 Stub peer 通信组件。"""
    base_dir = tmp_path / "async_comm"
    transport = transport or MultiPeerStubTransport(member_id=member_id)
    mailbox = Mailbox(base_dir, member_id)
    conversation_log = ConversationLog(base_dir / "conversation.jsonl")
    peer_snapshot_manager = PeerSnapshotManager(base_dir)
    sync_protocol = SyncProtocol(
        transport=transport,
        mailbox=mailbox,
        conversation_log=conversation_log,
        peer_snapshot_manager=peer_snapshot_manager,
        member_id=member_id,
    )
    components = {
        "transport": transport,
        "mailbox": mailbox,
        "conversation_log": conversation_log,
        "peer_snapshot_manager": peer_snapshot_manager,
        "sync_protocol": sync_protocol,
    }
    daemon._peer_comm_components = components
    return components


# ---------------------------------------------------------------------------
# SubTask 29.1：按 tag 路由 + 多候选混合策略端到端
# ---------------------------------------------------------------------------


class TestTagRoutingEndToEnd:
    """按 tag 路由 + 多候选混合策略端到端集成测试。

    场景：后端开发 alice 的 AI 调用 ask_peer(tag="运维", ...)，
    系统中有两个运维 charlie 和 dave。
    """

    def test_central_tag_routing_probe_then_targeted_ask(self, tmp_path):
        """central 模式：广播 probe → charlie 响应 → 定向追问 → realtime_answer。

        验证：
        - fetch_team_members 返回 charlie/dave（含"运维" tag）
        - probe 消息投递给 charlie 和 dave
        - charlie 响应 probe → 定向追问 → realtime_answer
        - 最终返回 realtime_answer（realtime=True）
        """
        charlie = PeerInfo(peer_id="charlie", online=True, capabilities=["运维", "后端"])
        dave = PeerInfo(peer_id="dave", online=True, capabilities=["运维"])
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"charlie", "dave"},
            team_members=[charlie, dave],
            probe_answers={"charlie": "我能帮你看运维问题"},
            auto_answers={"charlie": "测试环境登不上请检查 VPN"},
        )
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        answer_event = peer_comm.ask_peer(
            peer_id="", tag="运维", question="为啥我登不上测试环境",
        )

        # 最终返回 realtime_answer
        assert answer_event.event_type == EVENT_REALTIME_ANSWER
        assert answer_event.realtime is True
        assert answer_event.degraded is False
        assert "测试环境登不上请检查 VPN" in answer_event.payload.get("answer", "")

        # probe 消息投递给了 charlie 和 dave
        probe_delivers = [
            (pid, msgs) for pid, msgs in transport.delivered_messages
            if any(m.msg_type == MSG_TYPE_PROBE for m in msgs)
        ]
        probe_peers = {pid for pid, _ in probe_delivers}
        assert "charlie" in probe_peers
        assert "dave" in probe_peers

        # 定向追问（ask 消息）只投递给 charlie（第一个响应者）
        ask_delivers = [
            (pid, msgs) for pid, msgs in transport.delivered_messages
            if any(m.msg_type == "ask" for m in msgs)
        ]
        ask_peers = {pid for pid, _ in ask_delivers}
        assert ask_peers == {"charlie"}

        # ConversationLog 含 ask 事件
        ask_events = log.load_by_type(EVENT_ASK)
        assert len(ask_events) >= 1
        assert ask_events[-1].peer_id == "charlie"

    def test_no_probe_response_fallback_to_shadow(self, tmp_path):
        """无候选响应 probe → 转影子联络（degraded=True, tag_routing 标记）。

        验证：
        - charlie/dave 在线但不响应 probe（probe_answers 为空）
        - _fallback_to_shadow 被调用
        - 返回 simulated_answer（degraded=True）
        - ask 事件 payload 含 tag_routing="no_probe_response"
        """
        charlie = PeerInfo(peer_id="charlie", online=True, capabilities=["运维"])
        dave = PeerInfo(peer_id="dave", online=True, capabilities=["运维"])
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"charlie", "dave"},
            team_members=[charlie, dave],
            probe_answers={},  # 无 probe 响应
        )
        # 需要 shadow_comm 兜底（无快照时仍生成 simulated_answer，标记 snapshot_stale）
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_fixed_answer_generator("影子联络模拟回答"),
        )
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        # 不创建快照：_fallback_to_shadow 用 "tag:运维" 作为 peer_id，
        # ShadowComm 无快照时仍生成 simulated_answer（snapshot_stale=True）
        answer_event = peer_comm.ask_peer(
            peer_id="", tag="运维", question="为啥我登不上测试环境",
        )

        # 返回 simulated_answer（degraded=True）
        assert answer_event.event_type == EVENT_SIMULATED_ANSWER
        assert answer_event.degraded is True
        assert answer_event.realtime is False

        # ask 事件 payload 含 tag_routing 标记
        ask_events = log.load_by_type(EVENT_ASK)
        tag_routing_asks = [
            e for e in ask_events if e.payload.get("tag_routing") == "no_probe_response"
        ]
        assert len(tag_routing_asks) >= 1

    def test_p2p_tag_routing_via_capabilities(self, tmp_path):
        """P2P 模式：discover_peers + capabilities 匹配 → probe → 定向追问。

        验证：
        - transport 无 fetch_team_members（返回 None）→ 降级到 discover_peers
        - capabilities 匹配 "运维" 的 peer 被选为候选
        - probe → 响应 → 定向追问 → realtime_answer
        """
        charlie = PeerInfo(peer_id="charlie", online=True, capabilities=["运维"])
        dave = PeerInfo(peer_id="dave", online=True, capabilities=["运维"])
        eve = PeerInfo(peer_id="eve", online=True, capabilities=["前端"])  # 不匹配
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"charlie", "dave", "eve"},
            team_members=None,  # P2P 模式：触发降级
            discovered_peers=[charlie, dave, eve],
            probe_answers={"dave": "我可以帮你"},
            auto_answers={"dave": "P2P 模式运维回答"},
        )
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]

        answer_event = peer_comm.ask_peer(
            peer_id="", tag="运维", question="P2P 模式问题",
        )

        # 最终返回 realtime_answer
        assert answer_event.event_type == EVENT_REALTIME_ANSWER
        assert answer_event.realtime is True
        assert "P2P 模式运维回答" in answer_event.payload.get("answer", "")

        # eve 不参与 probe（capabilities 不匹配）
        probe_delivers = [
            (pid, msgs) for pid, msgs in transport.delivered_messages
            if any(m.msg_type == MSG_TYPE_PROBE for m in msgs)
        ]
        probe_peers = {pid for pid, _ in probe_delivers}
        assert "eve" not in probe_peers
        assert "charlie" in probe_peers or "dave" in probe_peers

    def test_no_tag_candidate_fallback(self, tmp_path):
        """无匹配候选 → 直接转影子联络（tag_routing="no_tag_candidate"）。

        验证：
        - fetch_team_members 返回的成员中无匹配 tag
        - _fallback_to_shadow 被调用（reason="no_tag_candidate"）
        - 返回 simulated_answer（degraded=True）
        """
        charlie = PeerInfo(peer_id="charlie", online=True, capabilities=["后端"])
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"charlie"},
            team_members=[charlie],  # 无"运维" tag
        )
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_fixed_answer_generator("无候选兜底回答"),
        )
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        # 不创建快照：ShadowComm 无快照时仍生成 simulated_answer（snapshot_stale=True）
        answer_event = peer_comm.ask_peer(
            peer_id="", tag="运维", question="无候选场景",
        )

        # 返回 simulated_answer（degraded=True）
        assert answer_event.event_type == EVENT_SIMULATED_ANSWER
        assert answer_event.degraded is True

        # ask 事件 payload 含 tag_routing="no_tag_candidate"
        ask_events = log.load_by_type(EVENT_ASK)
        tag_routing_asks = [
            e for e in ask_events
            if e.payload.get("tag_routing") == "no_tag_candidate"
        ]
        assert len(tag_routing_asks) >= 1


# ---------------------------------------------------------------------------
# SubTask 29.2：对话持久化与恢复端到端
# ---------------------------------------------------------------------------


class TestConversationPersistenceEndToEnd:
    """对话持久化与恢复端到端测试。

    场景：alice 与 bob 在线实时讨论 API 设计，bob 突然下线，
    对话标记 paused；bob 回来后自动恢复对话。
    """

    def test_realtime_discussion_then_peer_offline_paused(self, tmp_path):
        """在线实时讨论 → bob 下线 → alice 提问 → 对话标记 paused。

        验证：
        - 双方在线时多条 ask/answer 正常（realtime=True）
        - bob 下线后 alice 提问 → peer 不可达 → 对话标记 paused
        - simulated_answer（degraded=True）写入 ConversationLog
        """
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"bob"},
            auto_answers={"bob": "API 设计回答"},
        )
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_fixed_answer_generator("bob 离线时的模拟回答"),
        )
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        # 为 bob 创建快照（影子联络需要）
        snap_mgr.refresh_snapshot(
            "bob",
            harness_files={"rules/api.md": "# API 规则"},
            manifest={"assets": []},
            vector_clock=VectorClock.from_dict({"bob": 1}),
        )

        # 1. 在线实时讨论（多轮）
        ans1 = peer_comm.ask_peer("bob", "API 用 REST 还是 RPC？")
        ans2 = peer_comm.ask_peer("bob", "那版本怎么管理？", in_reply_to=ans1.event_id)

        assert ans1.realtime is True
        assert ans2.realtime is True
        assert log.count(event_type=EVENT_REALTIME_ANSWER) == 2

        # 对话状态为 active
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_ACTIVE

        # 2. bob 下线
        transport.set_peer_offline("bob")

        # 3. alice 提问 → peer 不可达 → 对话标记 paused
        ans3 = peer_comm.ask_peer("bob", "你还在吗？")
        assert ans3.event_type == EVENT_SIMULATED_ANSWER
        assert ans3.degraded is True

        # 对话状态变为 paused
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_PAUSED
        assert state["reason"] == "peer_offline"

    def test_peer_online_resume_conversation(self, tmp_path):
        """bob 上线 → resume_conversation → 加载历史上下文 → 标记 resumed。

        验证：
        - resume_conversation 返回历史事件列表
        - 对话状态从 paused → resumed
        - 历史事件含之前的 ask/answer
        """
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"bob"},
            auto_answers={"bob": "在线回答"},
        )
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_fixed_answer_generator("模拟回答"),
        )
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        snap_mgr.refresh_snapshot(
            "bob",
            harness_files={"rules/api.md": "# API 规则"},
            manifest={"assets": []},
            vector_clock=VectorClock.from_dict({"bob": 1}),
        )

        # 1. 在线讨论
        ans1 = peer_comm.ask_peer("bob", "问题1")
        assert ans1.realtime is True

        # 2. bob 下线 → paused
        transport.set_peer_offline("bob")
        ans2 = peer_comm.ask_peer("bob", "问题2")
        assert ans2.degraded is True

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_PAUSED

        # 3. bob 上线 → resume_conversation
        transport.set_peer_online("bob")
        resumed_events = peer_comm.resume_conversation("bob")

        # 返回历史事件列表
        assert len(resumed_events) > 0
        # 含之前的 ask 事件
        event_types = {e.event_type for e in resumed_events}
        assert EVENT_ASK in event_types

        # 对话状态变为 resumed
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_RESUMED

    def test_resume_continues_in_reply_to_chain(self, tmp_path):
        """恢复后新消息接续 in_reply_to 链。

        验证：
        - resume 后 alice 继续提问
        - 新 ask 的 in_reply_to 可关联到恢复前的最后一条 answer
        - load_thread 返回完整对话链（含恢复前 + 恢复后）
        """
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"bob"},
            auto_answers={"bob": "链式回答"},
        )
        stack = build_comm_stack(
            "alice", tmp_path, transport,
            answer_generator=make_fixed_answer_generator("离线回答"),
        )
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]
        snap_mgr = stack["peer_snapshot_manager"]

        snap_mgr.refresh_snapshot(
            "bob",
            harness_files={"rules/api.md": "# API 规则"},
            manifest={"assets": []},
            vector_clock=VectorClock.from_dict({"bob": 1}),
        )

        # 1. 在线讨论
        ans1 = peer_comm.ask_peer("bob", "第一轮问题")

        # 2. bob 下线 → paused（第二轮关联第一轮回答，保持回复链）
        transport.set_peer_offline("bob")
        ans2 = peer_comm.ask_peer("bob", "第二轮问题（离线）", in_reply_to=ans1.event_id)
        assert ans2.degraded is True

        # 3. bob 上线 → resume
        transport.set_peer_online("bob")
        resumed_events = peer_comm.resume_conversation("bob")
        assert len(resumed_events) > 0

        # 4. 恢复后继续提问（in_reply_to 关联 ans2）
        ans3 = peer_comm.ask_peer("bob", "第三轮问题（恢复后）", in_reply_to=ans2.event_id)
        assert ans3.realtime is True

        # load_thread 从第一个 ask 开始，返回完整对话链
        ask_events = log.load_by_type(EVENT_ASK)
        first_ask = ask_events[0]
        thread = log.load_thread(first_ask.event_id)
        # 含 3 个 ask + 3 个 answer（realtime/simulated/realtime）
        assert len(thread) >= 6


# ---------------------------------------------------------------------------
# SubTask 29.3：对话超时断开 + 自动恢复
# ---------------------------------------------------------------------------


class TestConversationTimeoutAndRecover:
    """对话超时断开 + 自动恢复测试。

    场景：alice 与 bob 对话超过 realtime_session_timeout 无新消息 →
    标记 timeout_disconnect；bob 发新消息时自动恢复。
    """

    def test_daemon_detects_timeout_disconnect(self, tmp_path):
        """daemon 扫描检测到超时 → 标记 timeout_disconnect。

        验证：
        - 对话 active 状态 + 旧时间戳事件
        - daemon._run_session_timeout_check 检测超时
        - 状态变为 timeout_disconnect
        """
        daemon = ClientDaemon(make_daemon_config(tmp_path))
        components = inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 创建旧时间戳事件（超过 60s）
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=120)).isoformat()
        event = make_event(
            event_id="evt-timeout-1",
            peer_id="bob",
            timestamp=old_time,
            payload={"question": "旧问题"},
        )
        log.append(event)
        log.set_conversation_state("bob", CONV_STATE_ACTIVE, last_event_id="evt-timeout-1")

        # daemon 扫描（短 timeout=60s）
        daemon._run_session_timeout_check(60)

        # 状态变为 timeout_disconnect
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_TIMEOUT_DISCONNECT
        assert "session_timeout" in state["reason"]

    def test_resume_after_timeout(self, tmp_path):
        """超时后 resume_conversation → 标记 resumed。

        验证：
        - timeout_disconnect 状态下 resume_conversation 返回历史事件
        - 状态变为 resumed
        - 恢复后可继续对话
        """
        transport = MultiPeerStubTransport(
            member_id="alice",
            online_peers={"bob"},
            auto_answers={"bob": "恢复后回答"},
        )
        stack = build_comm_stack("alice", tmp_path, transport)
        peer_comm = stack["peer_comm"]
        log = stack["conversation_log"]

        # 构造旧时间戳事件 + active 状态
        old_time = (datetime.now(timezone.utc) - timedelta(seconds=300)).isoformat()
        event = make_event(
            event_id="evt-timeout-2",
            peer_id="bob",
            timestamp=old_time,
            payload={"question": "超时前的问题"},
        )
        log.append(event)
        log.set_conversation_state("bob", CONV_STATE_ACTIVE, last_event_id="evt-timeout-2")

        # 手动标记 timeout_disconnect（模拟 daemon 检测到）
        log.set_conversation_state(
            "bob", CONV_STATE_TIMEOUT_DISCONNECT,
            last_event_id="evt-timeout-2",
            reason="session_timeout_300s",
        )

        # resume_conversation
        resumed_events = peer_comm.resume_conversation("bob")
        assert len(resumed_events) > 0

        # 状态变为 resumed
        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_RESUMED

        # 恢复后可继续对话
        ans = peer_comm.ask_peer("bob", "恢复后的新问题")
        assert ans.realtime is True

    def test_recent_active_not_timeout(self, tmp_path):
        """active 对话在 timeout 内 → 不标记超时。

        验证：
        - 近期事件（在 timeout 内）→ daemon 不标记 timeout_disconnect
        - 状态保持 active
        """
        daemon = ClientDaemon(make_daemon_config(tmp_path))
        components = inject_daemon_components(daemon, tmp_path)
        log: ConversationLog = components["conversation_log"]

        # 近期事件（5s 前，在 60s timeout 内）
        recent_time = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
        event = make_event(
            event_id="evt-recent",
            peer_id="bob",
            timestamp=recent_time,
            payload={"question": "近期问题"},
        )
        log.append(event)
        log.set_conversation_state("bob", CONV_STATE_ACTIVE, last_event_id="evt-recent")

        daemon._run_session_timeout_check(60)

        state = log.get_conversation_state("bob")
        assert state is not None
        assert state["state"] == CONV_STATE_ACTIVE


# ---------------------------------------------------------------------------
# SubTask 29.4：梦境提炼集成测试
# ---------------------------------------------------------------------------


class TestDreamDistillIntegration:
    """梦境提炼集成测试。

    场景：alice 与 bob/charlie 有多次跨职能对话（含 needs_human_review 事件）→
    触发梦境提炼 → 产出资产 → tags 回灌。
    """

    def test_conversation_log_to_dream_with_needs_human_review(self, tmp_path):
        """ConversationLog 含 needs_human_review 事件 → 三阶段提炼 → 产出资产。

        验证：
        - ConversationLog 中有 ask/answer/needs_human_review 事件
        - run_with_conversation_log 执行三阶段提炼
        - Light 阶段对 needs_human_review 事件加权（confidence 更高）
        - Deep 阶段产出资产
        - conversation_session_count > 0
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")

        # 构造 alice 与 bob 的对话（含 needs_human_review）
        ask1 = make_event(
            event_type=EVENT_ASK, peer_id="bob",
            payload={"question": "如何配置 lint？必须的类型标注规则"},
        )
        log.append(ask1)
        log.append(make_event(
            event_type=EVENT_REALTIME_ANSWER, peer_id="bob",
            payload={"answer": "用 ruff 配置 pyproject.toml，必须标注类型"},
            in_reply_to=ask1.event_id,
        ))
        log.append(make_event(
            event_type=EVENT_NEEDS_HUMAN_REVIEW, peer_id="bob",
            payload={"answer": "此规则需人工审核：是否强制 mypy strict 模式"},
            in_reply_to=ask1.event_id,
        ))
        # 标记为 resumed（is_completed → True）
        log.set_conversation_state("bob", CONV_STATE_RESUMED, last_event_id=ask1.event_id)

        # 构造 PersonalDistill 实例
        budget_mgr = BudgetManager(default_daily_budget=100_000)
        pending_store = PendingCandidateStore(repo_root=tmp_path)
        signal_reporter = SignalReporter(member_id="alice")
        distill = PersonalDistill(
            llm=make_llm_returning_asset(tags=["lint专家", "类型标注"]),
            budget_mgr=budget_mgr,
            pending_store=pending_store,
            signal_reporter=signal_reporter,
            owner="alice",
            module_path="modules/backend",
            member_id="alice",
            repo_root=tmp_path,
        )

        result = distill.run_with_conversation_log(
            log, member_id="alice", enable_tags_feedback=False,
        )

        # 三阶段提炼执行
        assert result.error is None
        assert result.conversation_session_count > 0

        # Light 阶段产出信号
        assert result.light is not None
        assert result.light.signal_count > 0

        # needs_human_review 事件被加权（confidence > 0.5）
        needs_review_signals = [
            s for s in result.light.signals
            if s.metadata.get("needs_human_review") is True
        ]
        assert len(needs_review_signals) >= 1
        for signal in needs_review_signals:
            assert signal.confidence > 0.5
            assert signal.candidate_type == "rule"

        # Deep 阶段产出资产
        assert result.deep is not None
        assert result.produced_count > 0

    def test_tags_feedback_called_after_distill(self, tmp_path):
        """提炼产出资产后 → tags 回灌调用。

        验证：
        - enable_tags_feedback=True 时调用 StubTagsFeedbackClient
        - tags_feedback_applied=True
        - feedback_calls 记录正确的 member_id 和 tags
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")

        ask1 = make_event(
            event_type=EVENT_ASK, peer_id="bob",
            payload={"question": "必须配置 lint 规则"},
        )
        log.append(ask1)
        log.append(make_event(
            event_type=EVENT_REALTIME_ANSWER, peer_id="bob",
            payload={"answer": "用 ruff 配置"},
            in_reply_to=ask1.event_id,
        ))
        log.set_conversation_state("bob", CONV_STATE_RESUMED, last_event_id=ask1.event_id)

        budget_mgr = BudgetManager(default_daily_budget=100_000)
        pending_store = PendingCandidateStore(repo_root=tmp_path)
        signal_reporter = SignalReporter(member_id="alice")
        distill = PersonalDistill(
            llm=make_llm_returning_asset(tags=["lint专家", "类型标注"]),
            budget_mgr=budget_mgr,
            pending_store=pending_store,
            signal_reporter=signal_reporter,
            owner="alice",
            module_path="modules/backend",
            member_id="alice",
            repo_root=tmp_path,
        )

        stub_feedback = StubTagsFeedbackClient(return_value=True)
        result = distill.run_with_conversation_log(
            log,
            member_id="alice",
            enable_tags_feedback=True,
            tags_feedback_client=stub_feedback,
        )

        # tags 回灌被调用
        assert result.tags_feedback_applied is True
        assert result.tags_feedback_error is None
        assert len(stub_feedback.feedback_calls) == 1
        called_member_id, called_tags = stub_feedback.feedback_calls[0]
        assert called_member_id == "alice"
        assert "lint专家" in called_tags
        assert "类型标注" in called_tags

    def test_tags_feedback_failure_does_not_block(self, tmp_path):
        """tags 回灌失败 → 不阻断提炼。

        验证：
        - StubTagsFeedbackClient 返回 False
        - tags_feedback_applied=False
        - 提炼仍正常完成（produced_count > 0）
        - error 为 None
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")

        ask1 = make_event(
            event_type=EVENT_ASK, peer_id="bob",
            payload={"question": "必须配置 lint 规则"},
        )
        log.append(ask1)
        log.append(make_event(
            event_type=EVENT_REALTIME_ANSWER, peer_id="bob",
            payload={"answer": "用 ruff 配置"},
            in_reply_to=ask1.event_id,
        ))
        log.set_conversation_state("bob", CONV_STATE_RESUMED, last_event_id=ask1.event_id)

        budget_mgr = BudgetManager(default_daily_budget=100_000)
        pending_store = PendingCandidateStore(repo_root=tmp_path)
        signal_reporter = SignalReporter(member_id="alice")
        distill = PersonalDistill(
            llm=make_llm_returning_asset(tags=["lint专家"]),
            budget_mgr=budget_mgr,
            pending_store=pending_store,
            signal_reporter=signal_reporter,
            owner="alice",
            module_path="modules/backend",
            member_id="alice",
            repo_root=tmp_path,
        )

        stub_feedback = StubTagsFeedbackClient(return_value=False)
        result = distill.run_with_conversation_log(
            log,
            member_id="alice",
            enable_tags_feedback=True,
            tags_feedback_client=stub_feedback,
        )

        # tags 回灌失败但不阻断提炼
        assert result.tags_feedback_applied is False
        assert result.error is None
        assert result.produced_count > 0

    def test_no_completed_sessions_no_distill(self, tmp_path):
        """无已完成会话 → 不执行提炼（返回空结果）。

        验证：
        - 所有对话状态为 active（未完成）
        - run_with_conversation_log 返回空结果
        - conversation_session_count=0
        - error 为 None
        """
        log = ConversationLog(tmp_path / "conversation.jsonl")

        ask1 = make_event(
            event_type=EVENT_ASK, peer_id="bob",
            payload={"question": "进行中的问题"},
        )
        log.append(ask1)
        log.set_conversation_state("bob", CONV_STATE_ACTIVE, last_event_id=ask1.event_id)

        budget_mgr = BudgetManager(default_daily_budget=100_000)
        pending_store = PendingCandidateStore(repo_root=tmp_path)
        signal_reporter = SignalReporter(member_id="alice")
        distill = PersonalDistill(
            llm=make_llm_returning_asset(),
            budget_mgr=budget_mgr,
            pending_store=pending_store,
            signal_reporter=signal_reporter,
            owner="alice",
            module_path="modules/backend",
            member_id="alice",
            repo_root=tmp_path,
        )

        result = distill.run_with_conversation_log(
            log, member_id="alice", enable_tags_feedback=False,
        )

        # 无已完成会话
        assert result.conversation_session_count == 0
        assert result.error is None
        assert result.light is None
        assert result.deep is None


# ---------------------------------------------------------------------------
# SubTask 29.5：MCP ask_peer 按 tag 路由 + resume_conversation 工具测试
# ---------------------------------------------------------------------------


class TestMcpTagRoutingAndResume:
    """MCP ask_peer 按 tag 路由 + resume_conversation 工具测试。"""

    def _build_server(
        self,
        tmp_path: Path,
        transport: MultiPeerStubTransport | None = None,
    ) -> tuple[McpServer, MultiPeerStubTransport, TransportBridge]:
        """构建带 StubTransport 的 McpServer。"""
        charlie = PeerInfo(peer_id="charlie", online=True, capabilities=["运维"])
        dave = PeerInfo(peer_id="dave", online=True, capabilities=["运维"])
        transport = transport or MultiPeerStubTransport(
            member_id="alice",
            online_peers={"bob", "charlie", "dave"},
            team_members=[charlie, dave],
            probe_answers={"charlie": "我能帮你"},
            auto_answers={
                "bob": "MCP 回答",
                "charlie": "MCP tag 路由回答",
            },
        )
        config = ClientConfig(
            member_id="alice",
            repo_root=str(tmp_path),
            topology=TOPOLOGY_CENTRAL,
            network_check_interval_seconds=0,  # 测试用：不缓存可达性
        )
        bridge = TransportBridge(config, transport=transport)
        server = McpServer(config)
        server.bridge = bridge
        return server, transport, bridge

    def test_mcp_ask_peer_by_tag(self, tmp_path):
        """MCP ask_peer 按 tag 路由。

        验证：
        - execute_tool("ask_peer", {"tag": "运维", "question": "..."}) 正确路由
        - 返回 result 含 answer / realtime / tag 字段
        """
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("ask_peer", {
            "tag": "运维",
            "question": "MCP tag 路由提问",
        })

        assert "result" in result
        data = result["result"]
        assert "answer" in data
        assert "realtime" in data
        assert data["realtime"] is True
        assert data["degraded"] is False
        assert data.get("tag") == "运维"
        assert "MCP tag 路由回答" in data["answer"]

    def test_mcp_ask_peer_missing_peer_and_tag_returns_error(self, tmp_path):
        """MCP ask_peer 缺少 peer_id 和 tag → 返回错误。

        验证：
        - execute_tool("ask_peer", {"peer_id": "", "tag": "", "question": "..."})
        - 返回 error "必须提供 peer_id 或 tag"
        """
        server, _, _ = self._build_server(tmp_path)

        result = server.call_tool("ask_peer", {
            "peer_id": "",
            "tag": "",
            "question": "缺少路由参数",
        })

        assert "error" in result
        assert "必须提供 peer_id 或 tag" in result["error"]

    def test_mcp_resume_conversation(self, tmp_path):
        """MCP resume_conversation 恢复对话。

        验证：
        - 先构造 paused 对话
        - execute_tool("resume_conversation", {"peer_id": "bob"}) 恢复
        - 返回 result 含 resumed / events / event_count
        """
        server, transport, bridge = self._build_server(tmp_path)
        peer_comm = bridge.peer_comm
        log = bridge.conversation_log
        snap_mgr = bridge.peer_snapshot_manager

        # 为 bob 创建快照
        snap_mgr.refresh_snapshot(
            "bob",
            harness_files={"rules/api.md": "# API 规则"},
            manifest={"assets": []},
            vector_clock=VectorClock.from_dict({"bob": 1}),
        )

        # 构造在线对话 → bob 下线 → paused
        peer_comm.ask_peer("bob", "在线问题1")
        transport.set_peer_offline("bob")
        peer_comm.ask_peer("bob", "离线问题2")

        state = log.get_conversation_state("bob")
        assert state["state"] == CONV_STATE_PAUSED

        # bob 上线 → resume
        transport.set_peer_online("bob")

        result = server.call_tool("resume_conversation", {
            "peer_id": "bob",
        })

        assert "result" in result
        data = result["result"]
        assert data["peer_id"] == "bob"
        assert data["resumed"] is True
        assert data["event_count"] > 0
        assert isinstance(data["events"], list)

    def test_transport_bridge_ask_peer_supports_tag(self, tmp_path):
        """TransportBridge.ask_peer 支持 tag 参数。

        验证：
        - bridge.ask_peer(peer_id="", tag="运维", question="...") 正确路由
        - 返回 dict 含 tag 字段
        """
        server, _, bridge = self._build_server(tmp_path)

        result = bridge.ask_peer(
            peer_id="", question="直接调用 bridge", tag="运维",
        )

        assert result["realtime"] is True
        assert result.get("tag") == "运维"
        assert "MCP tag 路由回答" in result["answer"]

    def test_transport_bridge_resume_conversation_structure(self, tmp_path):
        """TransportBridge.resume_conversation 返回正确结构。

        验证：
        - 返回 dict 含 peer_id / resumed / events / event_count
        - 无暂停对话时 resumed=False
        """
        server, _, bridge = self._build_server(tmp_path)

        # 无暂停对话 → resumed=False
        result = bridge.resume_conversation("bob")
        assert result["peer_id"] == "bob"
        assert result["resumed"] is False
        assert result["event_count"] == 0
        assert isinstance(result["events"], list)
