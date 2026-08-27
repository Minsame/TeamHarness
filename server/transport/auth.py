"""Peer 身份互验：消息签名与验签。

对应 Task 8：
- PeerAuthenticator：复用 AgentApiKeyService 的 API Key 体系
  - 签名：用发送方 api_key 对消息内容做 HMAC-SHA256
  - 验签：用 sender_key_hash 反查 api_key（或预共享 key），验证签名
  - 握手：交换 key_hash + agent_id + key_prefix

设计要点：
- sender_key_hash = sha256(api_key)（与 AgentApiKeyService._hash_key 一致）
- signature = hmac_sha256(api_key, message_id|timestamp|payload_json)
- 验签时需要对方的 api_key（预共享或通过安全通道交换）
- 无对方 api_key 时，只能验证 key_hash 是否匹配 expected_key_hash
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging

from server.transport.types import Message

logger = logging.getLogger(__name__)

# key_prefix 保留长度（与 AgentApiKeyService.KEY_PREFIX_LEN 一致）
KEY_PREFIX_LEN = 8


class PeerAuthenticator:
    """Peer 间消息签名与验签。

    复用 AgentApiKeyService 的 API Key 体系：
    - 签名：用发送方的 api_key 对消息内容做 HMAC-SHA256
    - 验签：用 sender_key_hash 反查 api_key（或预共享 key），验证签名
    """

    def __init__(self, api_key: str, agent_id: str) -> None:
        """初始化，持有本节点的 api_key 和 agent_id。"""
        self._api_key = api_key
        self._agent_id = agent_id
        self._key_hash = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        self._key_prefix = api_key[:KEY_PREFIX_LEN]
        # 已知 peer 的 api_key 映射（key_hash → api_key），用于验签
        self._known_peer_keys: dict[str, str] = {}

    def add_peer_key(self, key_hash: str, api_key: str) -> None:
        """添加已知 peer 的 api_key（用于验签）。

        通过 handshake 获取对方的 key_hash 后，将预共享的 api_key 关联起来。
        """
        self._known_peer_keys[key_hash] = api_key

    def sign(self, message: Message) -> Message:
        """对消息签名，填充 sender_key_hash 和 signature 字段。

        - sender_key_hash = sha256(api_key)
        - signature = hmac_sha256(api_key, message_id + timestamp + payload_json)
        """
        message.sender_key_hash = self._key_hash
        payload_json = json.dumps(message.payload, sort_keys=True, ensure_ascii=False)
        sign_content = f"{message.message_id}|{message.timestamp}|{payload_json}"
        signature = hmac.new(
            self._api_key.encode("utf-8"),
            sign_content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        message.signature = signature
        return message

    def verify(self, message: Message, expected_key_hash: str | None = None) -> bool:
        """验证消息签名。

        - 检查 sender_key_hash 和 signature 非空
        - 若 expected_key_hash 提供，检查是否匹配
        - 重新计算 HMAC 验证签名（需要知道对方的 api_key，或预共享 key）
        - 验签失败返回 False
        """
        # 检查 sender_key_hash 和 signature 非空
        if not message.sender_key_hash or not message.signature:
            return False
        # 若 expected_key_hash 提供，检查是否匹配
        if expected_key_hash is not None and message.sender_key_hash != expected_key_hash:
            return False
        # 尝试用已知 api_key 重新计算 HMAC 验证签名
        peer_api_key = self._known_peer_keys.get(message.sender_key_hash)
        if peer_api_key is None:
            # 无对方 api_key，只能依赖 key_hash 匹配
            # 若 expected_key_hash 提供，key_hash 已验证 → True
            # 否则无法验证签名 → False
            return expected_key_hash is not None
        # 重新计算 HMAC 验证
        payload_json = json.dumps(message.payload, sort_keys=True, ensure_ascii=False)
        sign_content = f"{message.message_id}|{message.timestamp}|{payload_json}"
        expected_sig = hmac.new(
            peer_api_key.encode("utf-8"),
            sign_content.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(message.signature, expected_sig)

    def handshake(self, peer_endpoint: str) -> dict:
        """P2P 握手协议：交换 key_hash + agent_id。

        返回 {"agent_id": ..., "key_hash": ..., "key_prefix": ...}
        供对方验证。

        peer_endpoint 为对方地址，实际网络交换由调用方实现；
        本方法仅返回本节点身份信息。
        """
        return {
            "agent_id": self._agent_id,
            "key_hash": self._key_hash,
            "key_prefix": self._key_prefix,
        }


__all__ = ["PeerAuthenticator"]
