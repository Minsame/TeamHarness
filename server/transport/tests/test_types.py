"""Task 5 测试：transport 数据结构（PeerInfo / Message / SyncResult）。"""

from __future__ import annotations

from server.transport.types import Message, PeerInfo, SyncResult


class TestPeerInfo:
    """PeerInfo 构造与默认值。"""

    def test_required_peer_id(self):
        """peer_id 为必填字段。"""
        info = PeerInfo(peer_id="alice")
        assert info.peer_id == "alice"

    def test_defaults(self):
        """未提供字段使用默认值。"""
        info = PeerInfo(peer_id="alice")
        assert info.agent_id == ""
        assert info.endpoint == ""
        assert info.online is False
        assert info.last_seen == ""
        assert info.capabilities == []

    def test_full_construction(self):
        """完整构造。"""
        info = PeerInfo(
            peer_id="bob",
            agent_id="agent-2",
            endpoint="192.168.1.10:8080",
            online=True,
            last_seen="2026-08-12T10:00:00Z",
            capabilities=["ask", "share_asset"],
        )
        assert info.peer_id == "bob"
        assert info.agent_id == "agent-2"
        assert info.endpoint == "192.168.1.10:8080"
        assert info.online is True
        assert info.last_seen == "2026-08-12T10:00:00Z"
        assert info.capabilities == ["ask", "share_asset"]

    def test_capabilities_independent(self):
        """capabilities 默认值不共享（mutable default 隔离）。"""
        a = PeerInfo(peer_id="a")
        b = PeerInfo(peer_id="b")
        a.capabilities.append("ask")
        assert b.capabilities == []


class TestMessage:
    """Message 构造与字段赋值。"""

    def test_required_message_id(self):
        """message_id 为必填字段。"""
        msg = Message(message_id="msg-1")
        assert msg.message_id == "msg-1"

    def test_defaults(self):
        """未提供字段使用默认值。"""
        msg = Message(message_id="msg-1")
        assert msg.event_id == ""
        assert msg.sender_id == ""
        assert msg.recipient_id == ""
        assert msg.msg_type == ""
        assert msg.payload == {}
        assert msg.timestamp == ""
        assert msg.in_reply_to == ""
        assert msg.sender_key_hash == ""
        assert msg.signature == ""

    def test_field_assignment(self):
        """字段赋值（含 msg_type、回复链、签名）。"""
        msg = Message(
            message_id="msg-2",
            event_id="evt-2",
            sender_id="alice",
            recipient_id="bob",
            msg_type="ask",
            payload={"question": "lint 规则?"},
            timestamp="2026-08-12T10:00:00Z",
            in_reply_to="msg-1",
            sender_key_hash="abc123",
            signature="sig-xyz",
        )
        assert msg.event_id == "evt-2"
        assert msg.sender_id == "alice"
        assert msg.recipient_id == "bob"
        assert msg.msg_type == "ask"
        assert msg.payload == {"question": "lint 规则?"}
        assert msg.timestamp == "2026-08-12T10:00:00Z"
        assert msg.in_reply_to == "msg-1"
        assert msg.sender_key_hash == "abc123"
        assert msg.signature == "sig-xyz"

    def test_msg_types(self):
        """msg_type 支持各类消息类型。"""
        for mtype in ("ask", "answer", "share_asset", "sync", "heartbeat"):
            msg = Message(message_id=f"msg-{mtype}", msg_type=mtype)
            assert msg.msg_type == mtype

    def test_payload_independent(self):
        """payload 默认值不共享（mutable default 隔离）。"""
        a = Message(message_id="a")
        b = Message(message_id="b")
        a.payload["k"] = "v"
        assert b.payload == {}

    def test_broadcast_recipient_empty(self):
        """recipient_id 为空表示广播。"""
        msg = Message(message_id="msg-broadcast", recipient_id="")
        assert msg.recipient_id == ""


class TestSyncResult:
    """SyncResult 计数逻辑。"""

    def test_defaults(self):
        """默认值为失败空结果。"""
        result = SyncResult()
        assert result.success is False
        assert result.delivered_count == 0
        assert result.failed_count == 0
        assert result.pending_count == 0
        assert result.error == ""
        assert result.delivered_message_ids == []

    def test_success_with_delivered(self):
        """成功投递：delivered_count 与 delivered_message_ids 一致。"""
        result = SyncResult(
            success=True,
            delivered_count=2,
            delivered_message_ids=["msg-1", "msg-2"],
        )
        assert result.success is True
        assert result.delivered_count == 2
        assert len(result.delivered_message_ids) == 2
        assert result.failed_count == 0
        assert result.pending_count == 0

    def test_partial_failure(self):
        """部分失败：delivered + failed + pending 互不影响。"""
        result = SyncResult(
            success=False,
            delivered_count=3,
            failed_count=1,
            pending_count=2,
            error="peer bob unreachable",
        )
        assert result.delivered_count == 3
        assert result.failed_count == 1
        assert result.pending_count == 2
        assert result.error == "peer bob unreachable"

    def test_all_pending(self):
        """peer 离线：全部 pending。"""
        result = SyncResult(
            success=False,
            pending_count=5,
            error="all peers offline",
        )
        assert result.delivered_count == 0
        assert result.failed_count == 0
        assert result.pending_count == 5

    def test_delivered_message_ids_independent(self):
        """delivered_message_ids 默认值不共享。"""
        a = SyncResult()
        b = SyncResult()
        a.delivered_message_ids.append("msg-1")
        assert b.delivered_message_ids == []
