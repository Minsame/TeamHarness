"""通信数据结构（拓扑无关）。

定义 PeerInfo / Message / SyncResult 三个核心 DTO：
- PeerInfo：peer 节点元信息（成员 ID、网络地址、在线状态、能力）
- Message：拓扑无关的通信消息（含幂等去重 ID、回复链、签名）
- SyncResult：同步操作结果（投递计数与失败原因）

对应 Task 5 SubTask 5.2。
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PeerInfo:
    """Peer 节点信息。"""

    peer_id: str  # 成员 ID（如 alice/bob）
    agent_id: str = ""  # Agent ID
    endpoint: str = ""  # 网络地址（host:port 或 URL）
    online: bool = False  # 是否在线
    last_seen: str = ""  # 最后在线时间（ISO）
    capabilities: list[str] = field(default_factory=list)  # 能力列表


@dataclass
class Message:
    """通信消息（拓扑无关）。"""

    message_id: str  # UUID
    event_id: str = ""  # 幂等去重 ID
    sender_id: str = ""  # 发送方 peer_id
    recipient_id: str = ""  # 接收方 peer_id（空=广播）
    msg_type: str = ""  # ask / answer / share_asset / sync / heartbeat
    payload: dict = field(default_factory=dict)  # 消息内容
    timestamp: str = ""  # ISO 时间戳
    in_reply_to: str = ""  # 回复链
    sender_key_hash: str = ""  # 发送方 API Key 哈希
    signature: str = ""  # 消息签名


@dataclass
class SyncResult:
    """同步操作结果。"""

    success: bool = False
    delivered_count: int = 0  # 成功投递消息数
    failed_count: int = 0  # 失败消息数
    pending_count: int = 0  # 待投递（peer 离线）消息数
    error: str = ""
    delivered_message_ids: list[str] = field(default_factory=list)


__all__ = [
    "Message",
    "PeerInfo",
    "SyncResult",
]
