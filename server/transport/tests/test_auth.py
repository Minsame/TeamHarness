"""Task 8 测试：Peer 身份互验（签名 / 验签 / 握手）。"""

from __future__ import annotations

import hashlib

from server.transport.auth import PeerAuthenticator
from server.transport.types import Message


# ----------------------------------------------------------------------
# 签名与验签正常流程
# ----------------------------------------------------------------------


class TestSignAndVerify:
    """签名与验签正常流程。"""

    def test_sign_fills_fields(self):
        """sign 填充 sender_key_hash 和 signature。"""
        auth = PeerAuthenticator(api_key="th_abc123", agent_id="agent-1")
        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        signed = auth.sign(msg)

        assert signed.sender_key_hash != ""
        assert signed.signature != ""
        # sender_key_hash = sha256(api_key)
        assert signed.sender_key_hash == hashlib.sha256(b"th_abc123").hexdigest()

    def test_sign_returns_same_message(self):
        """sign 返回同一消息对象（原地填充）。"""
        auth = PeerAuthenticator(api_key="th_key", agent_id="agent-1")
        msg = Message(message_id="msg-1", timestamp="2026-08-12T10:00:00Z")
        signed = auth.sign(msg)
        assert signed is msg

    def test_verify_with_peer_key(self):
        """已知对方 api_key 时验签通过。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")

        # Bob 预先知道 Alice 的 key_hash 和 api_key
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            sender_id="alice",
            timestamp="2026-08-12T10:00:00Z",
            payload={"question": "lint?"},
        )
        alice.sign(msg)

        assert bob.verify(msg) is True

    def test_verify_with_expected_key_hash_only(self):
        """无对方 api_key，但有 expected_key_hash → 通过。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        # Bob 不知道 Alice 的 api_key，但知道预期的 key_hash
        assert bob.verify(msg, expected_key_hash=alice._key_hash) is True

    def test_round_trip_self_sign_verify(self):
        """自己签名自己验证（key_hash 在已知列表中）。"""
        auth = PeerAuthenticator(api_key="th_secret", agent_id="agent-1")
        auth.add_peer_key(auth._key_hash, "th_secret")

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"k": "v"},
        )
        auth.sign(msg)

        assert auth.verify(msg) is True

    def test_sign_content_includes_payload(self):
        """签名内容包含 payload（不同 payload 产生不同签名）。"""
        auth = PeerAuthenticator(api_key="th_key", agent_id="agent-1")

        msg1 = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        msg2 = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "world"},
        )
        auth.sign(msg1)
        auth.sign(msg2)

        assert msg1.signature != msg2.signature


# ----------------------------------------------------------------------
# 篡改检测
# ----------------------------------------------------------------------


class TestTamperDetection:
    """篡改消息验签失败。"""

    def test_tamper_payload_fails(self):
        """篡改 payload → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"question": "original"},
        )
        alice.sign(msg)

        # 篡改 payload
        msg.payload = {"question": "tampered"}

        assert bob.verify(msg) is False

    def test_tamper_timestamp_fails(self):
        """篡改 timestamp → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        msg.timestamp = "2026-08-12T11:00:00Z"

        assert bob.verify(msg) is False

    def test_tamper_message_id_fails(self):
        """篡改 message_id → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        msg.message_id = "msg-tampered"

        assert bob.verify(msg) is False

    def test_tamper_signature_fails(self):
        """篡改 signature → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        # 篡改签名（翻转一个字符）
        msg.signature = "0" + msg.signature[1:]

        assert bob.verify(msg) is False


# ----------------------------------------------------------------------
# 空签名拒绝
# ----------------------------------------------------------------------


class TestEmptySignature:
    """空签名拒绝。"""

    def test_empty_signature_rejected(self):
        """signature 为空 → 验签失败。"""
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        msg = Message(
            message_id="msg-1",
            sender_key_hash="some_hash",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        # signature 默认为空
        assert bob.verify(msg) is False

    def test_empty_key_hash_rejected(self):
        """sender_key_hash 为空 → 验签失败。"""
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        msg = Message(
            message_id="msg-1",
            signature="some_sig",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        # sender_key_hash 默认为空
        assert bob.verify(msg) is False

    def test_both_empty_rejected(self):
        """signature 和 sender_key_hash 都为空 → 验签失败。"""
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        msg = Message(message_id="msg-1", timestamp="2026-08-12T10:00:00Z")
        assert bob.verify(msg) is False

    def test_empty_signature_rejected_even_with_expected_key_hash(self):
        """signature 为空时，即使 expected_key_hash 提供 → 仍失败。"""
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        msg = Message(
            message_id="msg-1",
            sender_key_hash="some_hash",
            timestamp="2026-08-12T10:00:00Z",
        )
        assert bob.verify(msg, expected_key_hash="some_hash") is False


# ----------------------------------------------------------------------
# expected_key_hash 匹配逻辑
# ----------------------------------------------------------------------


class TestExpectedKeyHash:
    """expected_key_hash 匹配逻辑。"""

    def test_expected_key_hash_mismatch(self):
        """expected_key_hash 不匹配 → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        bob.add_peer_key(alice._key_hash, alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        # 传入错误的 expected_key_hash
        assert bob.verify(msg, expected_key_hash="wrong_hash") is False

    def test_no_peer_key_no_expected_hash_fails(self):
        """无对方 api_key 且无 expected_key_hash → 验签失败。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        # Bob 不知道 Alice 的 api_key

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        assert bob.verify(msg) is False

    def test_expected_key_hash_match_no_peer_key_passes(self):
        """expected_key_hash 匹配但无对方 api_key → 通过（key_hash 验证）。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")
        # Bob 不知道 Alice 的 api_key，但通过 handshake 知道 key_hash

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        assert bob.verify(msg, expected_key_hash=alice._key_hash) is True


# ----------------------------------------------------------------------
# handshake 握手协议
# ----------------------------------------------------------------------


class TestHandshake:
    """handshake 握手协议。"""

    def test_handshake_returns_correct_fields(self):
        """handshake 返回 agent_id / key_hash / key_prefix 三个字段。"""
        api_key = "th_a1b2c3d4e5f6g7h8"
        auth = PeerAuthenticator(api_key=api_key, agent_id="agent-1")
        result = auth.handshake("http://peer:8080")

        assert set(result.keys()) == {"agent_id", "key_hash", "key_prefix"}
        assert result["agent_id"] == "agent-1"
        assert result["key_hash"] == hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        assert result["key_prefix"] == api_key[:8]

    def test_handshake_key_hash_matches_sign(self):
        """handshake 返回的 key_hash 与 sign 填充的一致。"""
        auth = PeerAuthenticator(api_key="th_secret", agent_id="agent-1")
        msg = Message(message_id="msg-1", timestamp="2026-08-12T10:00:00Z")
        auth.sign(msg)

        handshake = auth.handshake("http://peer:8080")
        assert handshake["key_hash"] == msg.sender_key_hash

    def test_handshake_key_prefix_is_first_8_chars(self):
        """key_prefix 是 api_key 前 8 字符。"""
        api_key = "th_a1b2c3d4rest_of_key"
        auth = PeerAuthenticator(api_key=api_key, agent_id="agent-1")
        result = auth.handshake("http://peer:8080")
        assert result["key_prefix"] == api_key[:8]
        assert result["key_prefix"] == "th_a1b2c"

    def test_handshake_ignores_peer_endpoint(self):
        """handshake 对不同 peer_endpoint 返回相同身份信息。"""
        auth = PeerAuthenticator(api_key="th_secret", agent_id="agent-1")
        r1 = auth.handshake("http://peer1:8080")
        r2 = auth.handshake("http://peer2:8080")
        assert r1 == r2

    def test_handshake_used_for_verification(self):
        """握手后用 key_hash + 预共享 api_key 完成验签。"""
        alice_key = "th_alice_secret"
        alice = PeerAuthenticator(api_key=alice_key, agent_id="alice")
        bob = PeerAuthenticator(api_key="th_bob_secret", agent_id="bob")

        # 模拟握手：Alice 把身份信息发给 Bob
        alice_info = alice.handshake("http://bob:8080")
        # Bob 通过安全通道预共享得到 Alice 的 api_key，关联到 key_hash
        bob.add_peer_key(alice_info["key_hash"], alice_key)

        msg = Message(
            message_id="msg-1",
            timestamp="2026-08-12T10:00:00Z",
            payload={"q": "hello"},
        )
        alice.sign(msg)

        # Bob 用握手得到的 key_hash 作为 expected_key_hash 验证
        assert bob.verify(msg, expected_key_hash=alice_info["key_hash"]) is True
