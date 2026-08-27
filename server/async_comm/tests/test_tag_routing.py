"""Task 25 测试：按 tag 路由 + 缓存同步。

覆盖：
- central 模式按 tag 路由（StubTransport + fetch_team_members mock）
- P2P 模式按 tag 路由（discover_peers + capabilities 匹配）
- 多候选混合策略（广播 probe → 定向追问 → 命中实时路径）
- 无候选响应超时转影子联络（fallback_to_shadow）
- P2P tags_sync 广播与接收刷新（admin 广播 → 非 admin 刷新 _peer_registry）
- 降级路径（未收到 tags_sync 时使用 ClientConfig.peers[].tags）
- 幂等性（tags_sync 多次接收不产生副作用）
- 边界：tag 为空 / 无候选 / 无 shadow_comm

测试隔离：用 tmp_path fixture 与 StubTransport 注入，不依赖真实网络。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.async_comm.constants import (
    EVENT_ASK,
    EVENT_REALTIME_ANSWER,
    MSG_TYPE_PROBE,
    MSG_TYPE_TAGS_SYNC,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_comm import PeerComm
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.types import ConversationEvent
from server.client.config import ClientConfig, load_client_config, save_client_config
from server.transport.p2p_transport import P2PSyncTransport
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - ``reachable_peers``：可达 peer 集合
    - ``team_members``：fetch_team_members 返回值（central 模式权威源）
    - ``discovered_peers``：discover_peers 返回值（P2P 模式）
    - ``auto_answer``：非空时 fetch 自动为已 deliver 的消息生成回答
    - ``probe_answers``：{peer_id: answer_text}，probe 模式 fetch 返回此映射
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        team_members: list[PeerInfo] | None = None,
        discovered_peers: list[PeerInfo] | None = None,
        auto_answer: str = "",
        probe_answers: dict[str, str] | None = None,
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.team_members = team_members or []
        self.discovered_peers = discovered_peers or []
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.reachability_checks: list[str] = []
        self.auto_answer = auto_answer
        self.probe_answers = probe_answers or {}
        # 记录 fetch 调用次数（用于断言 probe 是否触发）
        self.fetch_calls: list[str] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(success=True, delivered_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        self.fetch_calls.append(peer_id)
        # probe 模式：返回预置的 probe answer（in_reply_to 匹配 probe.event_id）
        # 通过检查最近一条 deliver 的 probe 消息匹配 in_reply_to
        responses: list[Message] = []
        # 找最近一次给该 peer 的 deliver（通常是 probe）
        for delivered_peer, messages in reversed(self.delivered_messages):
            if delivered_peer != peer_id:
                continue
            for msg in messages:
                if msg.msg_type == MSG_TYPE_PROBE:
                    # 用 probe_answers 中预置的答案
                    if peer_id in self.probe_answers:
                        responses.append(
                            Message(
                                message_id=str(uuid.uuid4()),
                                event_id=str(uuid.uuid4()),
                                sender_id=peer_id,
                                recipient_id=msg.sender_id,
                                msg_type="answer",
                                payload={"answer": self.probe_answers[peer_id]},
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                in_reply_to=msg.event_id,
                            )
                        )
                    break  # 找到最近一次 probe 即可
            break  # 只看最近一次 deliver

        if responses:
            return responses

        # 自动回答模式：为 ask 类型的 deliver 生成回答
        if self.auto_answer:
            for delivered_peer, messages in self.delivered_messages:
                if delivered_peer != peer_id:
                    continue
                for msg in messages:
                    if msg.msg_type == "ask":
                        responses.append(
                            Message(
                                message_id=str(uuid.uuid4()),
                                event_id=str(uuid.uuid4()),
                                sender_id=peer_id,
                                recipient_id=msg.sender_id,
                                msg_type="answer",
                                payload={"answer": self.auto_answer},
                                timestamp=datetime.now(timezone.utc).isoformat(),
                                in_reply_to=msg.event_id,
                            )
                        )
            return responses
        return []

    def is_peer_reachable(self, peer_id: str) -> bool:
        self.reachability_checks.append(peer_id)
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        return list(self.discovered_peers)

    def fetch_team_members(self) -> list[PeerInfo]:
        return list(self.team_members)


class StubShadowComm:
    """Stub 实现 ShadowCommProtocol。"""

    def __init__(self) -> None:
        self.ask_calls: list[tuple[str, str, str]] = []

    def ask_peer(
        self, peer_id: str, question: str, *, in_reply_to: str = ""
    ) -> ConversationEvent:
        self.ask_calls.append((peer_id, question, in_reply_to))
        return ConversationEvent(
            event_id=str(uuid.uuid4()),
            event_type="simulated_answer",
            peer_id=peer_id,
            timestamp=datetime.now(timezone.utc).isoformat(),
            degraded=True,
            realtime=False,
        )


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_comm(
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
    shadow_comm: StubShadowComm | None = None,
    member_id: str = "alice",
) -> tuple[PeerComm, StubTransport, StubShadowComm]:
    """构造 PeerComm 测试实例。"""
    transport = transport or StubTransport()
    shadow_comm = shadow_comm or StubShadowComm()
    comm = PeerComm(
        transport=transport,
        mailbox=Mailbox(tmp_path / "mb", member_id),
        conversation_log=ConversationLog(tmp_path / "conversation.jsonl"),
        peer_snapshot_manager=PeerSnapshotManager(tmp_path / "snapshots"),
        member_id=member_id,
        shadow_comm=shadow_comm,
    )
    return comm, transport, shadow_comm


# ---------------------------------------------------------------------------
# TestCentralTagRouting
# ---------------------------------------------------------------------------


class TestCentralTagRouting:
    """central 模式按 tag 路由（fetch_team_members 权威源）。"""

    def test_tag_routes_to_matching_member(self, tmp_path: Path):
        """tag="运维" 命中运维成员，走 probe → 定向追问路径。"""
        members = [
            PeerInfo(peer_id="bob", capabilities=["运维", "后端"]),
            PeerInfo(peer_id="carol", capabilities=["前端"]),
            PeerInfo(peer_id="dave", capabilities=["运维"]),
        ]
        transport = StubTransport(
            reachable_peers={"bob", "dave"},
            team_members=members,
            probe_answers={"bob": "ready"},
        )
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("fallback", "如何处理 X？", tag="运维")

        # 应该定向追问 bob（probe 第 1 个响应者）
        assert event.event_type == EVENT_REALTIME_ANSWER
        assert event.realtime is True
        # 询问应投递给 bob（probe 之后的 targeted ask）
        # 检查 ask 类型的 deliver 中包含 bob
        ask_delivers = [
            (pid, msgs)
            for pid, msgs in transport.delivered_messages
            if any(m.msg_type == "ask" for m in msgs)
        ]
        assert any(pid == "bob" for pid, _ in ask_delivers)

    def test_tag_no_matching_member_falls_back_to_shadow(self, tmp_path: Path):
        """tag 无匹配候选 → 转影子联络（fallback_peer_id）。"""
        members = [
            PeerInfo(peer_id="bob", capabilities=["前端"]),
        ]
        transport = StubTransport(
            reachable_peers={"bob"},
            team_members=members,
        )
        shadow = StubShadowComm()
        comm, transport, shadow = _make_comm(
            tmp_path, transport=transport, shadow_comm=shadow
        )

        event = comm.ask_peer("fallback_peer", "问题？", tag="运维")

        # 应回转到影子联络（simulated_answer）
        assert event.event_type == "simulated_answer"
        assert event.degraded is True
        # shadow_comm 被调用，peer_id 应是 fallback_peer
        assert len(shadow.ask_calls) == 1
        peer_id, _, _ = shadow.ask_calls[0]
        assert peer_id == "fallback_peer"

    def test_tag_no_matching_member_no_shadow_returns_pending(self, tmp_path: Path):
        """tag 无候选且 shadow_comm=None → 返回 pending realtime_answer。"""
        members = []
        transport = StubTransport(reachable_peers=set(), team_members=members)
        comm = PeerComm(
            transport=transport,
            mailbox=Mailbox(tmp_path / "mb", "alice"),
            conversation_log=ConversationLog(tmp_path / "conversation.jsonl"),
            peer_snapshot_manager=PeerSnapshotManager(tmp_path / "snapshots"),
            member_id="alice",
            shadow_comm=None,
        )

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        assert event.event_type == EVENT_REALTIME_ANSWER
        assert event.payload.get("status") == "pending"
        assert event.payload.get("tag_routing") == "no_tag_candidate"
        # outbox 应有 ask 事件
        outbox = comm.mailbox.load_outbox()
        assert len(outbox) == 1
        assert outbox[0].payload.get("tag_routing") == "no_tag_candidate"

    def test_tag_skips_self_member(self, tmp_path: Path):
        """候选中包含自己（member_id）时应被跳过。"""
        members = [
            PeerInfo(peer_id="alice", capabilities=["运维"]),  # 自己
            PeerInfo(peer_id="bob", capabilities=["运维"]),
        ]
        transport = StubTransport(
            reachable_peers={"alice", "bob"},
            team_members=members,
            probe_answers={"bob": "ready"},
        )
        comm, _, _ = _make_comm(tmp_path, transport=transport, member_id="alice")

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        # 应只 probe bob（alice 被跳过）
        probe_delivers = [
            (pid, msgs)
            for pid, msgs in transport.delivered_messages
            if any(m.msg_type == MSG_TYPE_PROBE for m in msgs)
        ]
        assert all(pid != "alice" for pid, _ in probe_delivers)
        assert any(pid == "bob" for pid, _ in probe_delivers)


# ---------------------------------------------------------------------------
# TestP2PTagRouting
# ---------------------------------------------------------------------------


class TestP2PTagRouting:
    """P2P 模式按 tag 路由（discover_peers + capabilities）。"""

    def test_p2p_tag_routes_by_discover_peers_capabilities(self, tmp_path: Path):
        """无 fetch_team_members 时退回 discover_peers + capabilities 匹配。"""

        class P2PStubTransport(StubTransport):
            """覆盖 fetch_team_members 抛 AttributeError 模拟 P2P 模式。"""

            def fetch_team_members(self):  # type: ignore[override]
                raise AttributeError("P2P 模式无此方法")

        peers = [
            PeerInfo(peer_id="bob", capabilities=["运维"]),
            PeerInfo(peer_id="carol", capabilities=["前端"]),
        ]
        transport = P2PStubTransport(
            reachable_peers={"bob"},
            discovered_peers=peers,
            probe_answers={"bob": "ready"},
        )
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        assert event.event_type == EVENT_REALTIME_ANSWER
        assert event.realtime is True

    def test_p2p_tag_no_matching_peer_returns_shadow(self, tmp_path: Path):
        """P2P 模式 discover_peers 无匹配 tag → 影子联络。"""

        class P2PStubTransport(StubTransport):
            def fetch_team_members(self):  # type: ignore[override]
                raise AttributeError("no method")

        peers = [PeerInfo(peer_id="bob", capabilities=["前端"])]
        transport = P2PStubTransport(
            reachable_peers={"bob"},
            discovered_peers=peers,
        )
        shadow = StubShadowComm()
        comm, _, shadow = _make_comm(tmp_path, transport=transport, shadow_comm=shadow)

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        assert event.event_type == "simulated_answer"
        assert event.degraded is True
        assert len(shadow.ask_calls) == 1


# ---------------------------------------------------------------------------
# TestProbeTargetedAsk
# ---------------------------------------------------------------------------


class TestProbeTargetedAsk:
    """多候选混合策略：probe → 定向追问。"""

    def test_probe_targets_first_responder(self, tmp_path: Path):
        """多候选中只有 bob 响应 probe → 定向追问 bob。"""
        members = [
            PeerInfo(peer_id="bob", capabilities=["运维"]),
            PeerInfo(peer_id="carol", capabilities=["运维"]),
        ]
        transport = StubTransport(
            reachable_peers={"bob", "carol"},
            team_members=members,
            probe_answers={"bob": "ready"},  # 只有 bob 响应
        )
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        # 应只对 bob 发起 ask（targeted）
        ask_delivers = [
            (pid, msgs)
            for pid, msgs in transport.delivered_messages
            if any(m.msg_type == "ask" for m in msgs)
        ]
        assert any(pid == "bob" for pid, _ in ask_delivers)
        assert not any(pid == "carol" for pid, _ in ask_delivers)
        # 返回 realtime_answer
        assert event.realtime is True

    def test_probe_no_response_falls_back_to_shadow(self, tmp_path: Path):
        """候选都可达但 probe 无响应 → 转影子联络。"""
        members = [
            PeerInfo(peer_id="bob", capabilities=["运维"]),
        ]
        transport = StubTransport(
            reachable_peers={"bob"},
            team_members=members,
            probe_answers={},  # 无任何响应
        )
        shadow = StubShadowComm()
        comm, _, shadow = _make_comm(tmp_path, transport=transport, shadow_comm=shadow)

        event = comm.ask_peer("fallback_peer", "问题？", tag="运维")

        assert event.event_type == "simulated_answer"
        assert event.degraded is True
        # shadow_comm 应被调用
        assert len(shadow.ask_calls) == 1
        peer_id, _, _ = shadow.ask_calls[0]
        assert peer_id == "fallback_peer"

    def test_probe_skips_unreachable_candidates(self, tmp_path: Path):
        """候选不可达时跳过 probe（节省开销）。"""
        members = [
            PeerInfo(peer_id="bob", capabilities=["运维"]),
            PeerInfo(peer_id="carol", capabilities=["运维"]),
        ]
        # 只有 bob 可达
        transport = StubTransport(
            reachable_peers={"bob"},
            team_members=members,
            probe_answers={"bob": "ready"},
        )
        comm, transport, _ = _make_comm(tmp_path, transport=transport)

        comm.ask_peer("fallback", "问题？", tag="运维")

        # probe 应只发给 bob（carol 不可达被跳过）
        probe_delivers = [
            (pid, msgs)
            for pid, msgs in transport.delivered_messages
            if any(m.msg_type == MSG_TYPE_PROBE for m in msgs)
        ]
        probe_targets = {pid for pid, _ in probe_delivers}
        assert "bob" in probe_targets
        assert "carol" not in probe_targets


# ---------------------------------------------------------------------------
# TestTagsSyncBroadcast
# ---------------------------------------------------------------------------


class TestTagsSyncBroadcast:
    """P2P 模式 tags_sync 广播与接收刷新。"""

    def test_admin_broadcast_tags_sync_to_all_peers(self):
        """admin 调用 broadcast_tags_sync 给所有已知 peer 投递 tags_sync 消息。"""
        transport = P2PSyncTransport(peers=["bob", "carol", "dave"])
        tags_map = {
            "bob": ["运维"],
            "carol": ["前端", "后端"],
            "dave": ["测试"],
        }

        result = transport.broadcast_tags_sync(tags_map)

        # 无活跃连接 → 所有消息落入 outbox（pending）
        # success=True 因为 pending_count > 0
        assert result.pending_count == 3
        # outbox 中每个 peer 都应有 1 条 tags_sync 消息
        for pid in ("bob", "carol", "dave"):
            msgs = transport.outbox.get(pid, [])
            assert any(m.msg_type == MSG_TYPE_TAGS_SYNC for m in msgs)
            # payload 中应包含完整的 tags_map
            tags_msg = next(m for m in msgs if m.msg_type == MSG_TYPE_TAGS_SYNC)
            assert "tags_map" in tags_msg.payload
            assert tags_msg.payload["tags_map"]["bob"] == ["运维"]

    def test_non_admin_handle_tags_sync_refreshes_registry(self):
        """非 admin peer 接收 tags_sync 后刷新 _peer_registry.capabilities。"""
        transport = P2PSyncTransport(peers=["bob", "carol"])
        # 初始 capabilities 为空
        assert transport.peer_registry["bob"].capabilities == []

        payload = {
            "tags_map": {
                "bob": ["运维", "后端"],
                "carol": ["前端"],
            }
        }
        transport.handle_tags_sync(payload)

        assert transport.peer_registry["bob"].capabilities == ["运维", "后端"]
        assert transport.peer_registry["carol"].capabilities == ["前端"]

    def test_handle_tags_sync_idempotent(self):
        """多次接收 tags_sync 覆盖式更新（幂等，不追加）。"""
        transport = P2PSyncTransport(peers=["bob"])
        # 第一次：bob tags=["运维"]
        transport.handle_tags_sync({"tags_map": {"bob": ["运维"]}})
        assert transport.peer_registry["bob"].capabilities == ["运维"]
        # 第二次：bob tags=["前端"]（覆盖，不追加）
        transport.handle_tags_sync({"tags_map": {"bob": ["前端"]}})
        assert transport.peer_registry["bob"].capabilities == ["前端"]
        # 第三次：重复相同内容（幂等）
        transport.handle_tags_sync({"tags_map": {"bob": ["前端"]}})
        assert transport.peer_registry["bob"].capabilities == ["前端"]

    def test_handle_tags_sync_registers_new_peer(self):
        """tags_sync 中包含未注册的 peer_id → 自动注册到 _peer_registry。"""
        transport = P2PSyncTransport(peers=["bob"])

        transport.handle_tags_sync({
            "tags_map": {
                "bob": ["运维"],
                "new_peer": ["前端"],  # 未注册
            }
        })

        assert "new_peer" in transport.peer_registry
        assert transport.peer_registry["new_peer"].capabilities == ["前端"]

    def test_handle_tags_sync_invalid_payload_noop(self):
        """无效 payload（无 tags_map / 非 dict）应安全忽略。"""
        transport = P2PSyncTransport(peers=["bob"])
        transport.peer_registry["bob"].capabilities = ["运维"]

        # payload 不是 dict
        transport.handle_tags_sync(None)  # type: ignore[arg-type]
        # payload 缺 tags_map
        transport.handle_tags_sync({"other": "field"})
        # tags_map 不是 dict
        transport.handle_tags_sync({"tags_map": "not_a_dict"})

        # capabilities 应保持不变
        assert transport.peer_registry["bob"].capabilities == ["运维"]

    def test_broadcast_empty_tags_map_is_noop(self):
        """空 tags_map 广播应返回 success=True 但不投递任何消息。"""
        transport = P2PSyncTransport(peers=["bob"])
        result = transport.broadcast_tags_sync({})
        assert result.success is True
        assert result.delivered_count == 0
        # outbox 应为空
        assert transport.outbox.get("bob", []) == []

    def test_broadcast_no_peers_registered(self):
        """无已知 peer 时广播返回 success=True, delivered_count=0。"""
        transport = P2PSyncTransport()
        result = transport.broadcast_tags_sync({"bob": ["运维"]})
        assert result.success is True
        assert result.delivered_count == 0


# ---------------------------------------------------------------------------
# TestClientConfigPeerTags
# ---------------------------------------------------------------------------


class TestClientConfigPeerTags:
    """ClientConfig.peers 静态 tags 配置（降级路径）。"""

    def test_resolve_peer_tags_from_dict_form(self):
        """dict 形式 peers 解析出 peer_id → tags 映射。"""
        cfg = ClientConfig(
            peers=[
                "host1:1234",  # 字符串形式，无 tags
                {"peer_id": "bob", "tags": ["运维", "后端"]},
                {"peer_id": "carol", "tags": ["前端"]},
                {"endpoint": "host2:5678", "tags": "测试,运维"},  # 字符串形式 tags
            ]
        )

        mapping = cfg.resolve_peer_tags()

        assert mapping["bob"] == ["运维", "后端"]
        assert mapping["carol"] == ["前端"]
        # 字符串形式 tags 用 endpoint 作为 key
        assert mapping["host2:5678"] == ["测试", "运维"]
        # 字符串形式 peer 不在映射中
        assert "host1:1234" not in mapping

    def test_resolve_peer_tags_empty(self):
        """无 dict 形式 peer 时返回空映射。"""
        cfg = ClientConfig(peers=["host1:1234", "host2:5678"])
        assert cfg.resolve_peer_tags() == {}

    def test_resolve_peer_tags_missing_peer_id_skipped(self):
        """dict 形式但缺 peer_id 与 endpoint → 跳过。"""
        cfg = ClientConfig(
            peers=[
                {"tags": ["运维"]},  # 缺 peer_id 与 endpoint
                {"peer_id": "", "tags": ["前端"]},  # 空 peer_id
            ]
        )
        assert cfg.resolve_peer_tags() == {}

    def test_is_admin_field_default_false(self):
        """is_admin 默认 False。"""
        cfg = ClientConfig()
        assert cfg.is_admin is False

    def test_load_client_config_with_dict_peers(self, tmp_path: Path):
        """配置文件中 peers 支持 dict 形式。"""
        config_yaml = """
server_url: https://example.com
topology: p2p
is_admin: true
peers:
  - host1:1234
  - peer_id: bob
    tags:
      - 运维
      - 后端
  - peer_id: carol
    tags: [前端]
"""
        config_dir = tmp_path / ".teamharness"
        config_dir.mkdir()
        (config_dir / "config.yaml").write_text(config_yaml, encoding="utf-8")

        cfg = load_client_config(repo_root=tmp_path)

        assert cfg.is_admin is True
        assert cfg.topology == "p2p"
        assert len(cfg.peers) == 3
        # dict 形式应保留
        dict_peers = [p for p in cfg.peers if isinstance(p, dict)]
        assert len(dict_peers) == 2
        # resolve_peer_tags 应工作
        mapping = cfg.resolve_peer_tags()
        assert mapping["bob"] == ["运维", "后端"]
        assert mapping["carol"] == ["前端"]

    def test_save_client_config_preserves_dict_peers(self, tmp_path: Path):
        """save_client_config 写回时应保留 dict 形式。"""
        cfg = ClientConfig(
            server_url="https://example.com",
            topology="p2p",
            is_admin=True,
            peers=[
                "host1:1234",
                {"peer_id": "bob", "tags": ["运维"]},
            ],
        )
        cfg.repo_root = str(tmp_path)

        save_client_config(cfg)
        # 重新加载验证
        cfg2 = load_client_config(repo_root=tmp_path)
        assert cfg2.is_admin is True
        assert cfg2.topology == "p2p"
        # 字符串形式 + dict 形式都保留
        assert any(isinstance(p, str) and p == "host1:1234" for p in cfg2.peers)
        assert any(
            isinstance(p, dict) and p.get("peer_id") == "bob"
            for p in cfg2.peers
        )
        # resolve_peer_tags 工作
        assert cfg2.resolve_peer_tags()["bob"] == ["运维"]

    def test_load_client_config_is_admin_env_var(self, tmp_path: Path, monkeypatch):
        """env TEAMHARNESS_IS_ADMIN 应被识别。"""
        monkeypatch.setenv("TEAMHARNESS_IS_ADMIN", "true")
        cfg = load_client_config(repo_root=tmp_path)
        assert cfg.is_admin is True

        monkeypatch.setenv("TEAMHARNESS_IS_ADMIN", "false")
        cfg = load_client_config(repo_root=tmp_path)
        assert cfg.is_admin is False


# ---------------------------------------------------------------------------
# TestP2PTagRoutingFallback
# ---------------------------------------------------------------------------


class TestP2PTagRoutingFallback:
    """P2P 模式未收到 tags_sync 时使用 ClientConfig.peers[].tags 静态配置。"""

    def test_static_tags_used_when_no_tags_sync_received(self, tmp_path: Path):
        """未收到 tags_sync → _peer_registry.capabilities 来自静态 peers[].tags。

        模拟流程：
        1. P2PSyncTransport 初始化时不带 peers（_peer_registry 为空）
        2. 上层从 ClientConfig.peers 静态配置构造 tags_map
        3. 模拟 admin 广播的 tags_sync 被接收后，本地 _peer_registry 有 capabilities
        4. ask_peer(tag=...) 能匹配到候选
        """
        # 步骤 1: 构造静态 peers 配置
        cfg = ClientConfig(
            topology="p2p",
            peers=[
                {"peer_id": "bob", "tags": ["运维"]},
                {"peer_id": "carol", "tags": ["前端"]},
            ],
        )
        static_tags_map = cfg.resolve_peer_tags()
        assert static_tags_map == {"bob": ["运维"], "carol": ["前端"]}

        # 步骤 2: P2PSyncTransport 初始化（不注册任何 peer）
        transport = P2PSyncTransport()

        # 步骤 3: 模拟 admin 广播 tags_sync 被本地接收
        # （在真实场景中由 deliver → fetch → handle_tags_sync 触发）
        transport.handle_tags_sync({"tags_map": static_tags_map})

        # 此时 _peer_registry 应有 bob 和 carol，且 capabilities 来自静态配置
        assert transport.peer_registry["bob"].capabilities == ["运维"]
        assert transport.peer_registry["carol"].capabilities == ["前端"]

    def test_no_static_tags_no_candidates(self, tmp_path: Path):
        """无静态 tags 配置 + 未收到 tags_sync → _resolve_candidates_by_tag 返回空。"""
        # PeerComm 的 transport.discover_peers 返回的 PeerInfo.capabilities 为空
        # （P2P 模式下未收到 tags_sync 时）

        class P2PNoFetchTransport(StubTransport):
            """模拟 P2P 模式：无 fetch_team_members 方法。"""

            def fetch_team_members(self):  # type: ignore[override]
                raise AttributeError("P2P 模式无此方法")

        transport = P2PNoFetchTransport(
            discovered_peers=[
                PeerInfo(peer_id="bob", capabilities=[]),  # 空 capabilities
            ],
            reachable_peers=set(),  # 不可达，不会触发 probe
        )
        comm, _, _ = _make_comm(tmp_path, transport=transport)
        shadow = StubShadowComm()
        comm.shadow_comm = shadow

        event = comm.ask_peer("fallback", "问题？", tag="运维")

        # 应转影子联络
        assert event.event_type == "simulated_answer"
        assert len(shadow.ask_calls) == 1
