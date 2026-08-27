"""async_comm 领域包：异步交流模块（Task 11 / Task 13 / Task 15 / Task 16 / Task 17 / Task 18）。

对应技术方案「异步交流」需求，提供 peer 间异步通信的核心数据结构与服务：
- VectorClock：版本向量，用于因果排序与冲突检测
- ConversationEvent：交流事件（ConversationLog 中的单条记录）
- PeerSnapshot：peer 的 harness 本地快照
- ConversationLog：peer 间交流事件的 append-only JSONL 日志（Task 15）
- Mailbox：信箱，管理 peer 间异步消息收发与状态流转（Task 13）
- PeerComm：通信核心入口，自动选择在线实时 / 离线影子路径（Task 16）
- ShadowComm：影子联络，peer 离线时基于本地快照模拟交流（Task 17）
- SyncProtocol：上线同步协议，peer 可达时触发同步与对账（Task 18）

设计原则：基于版本向量实现因果一致性，支持离线/影子联络场景下的
异步交流与冲突检测。
"""

from server.async_comm.conflict_resolver import (
    ConflictResolver,
    ResolutionResult,
    SimilarityFunc,
    default_similarity,
)
from server.async_comm.constants import (
    DEFAULT_AUTO_CONFIRM_THRESHOLD,
    DEFAULT_CONFLICT_THRESHOLD,
    DEFAULT_SNAPSHOT_POLICY,
    DEFAULT_SNAPSHOT_TTL_DAYS,
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
from server.async_comm.peer_comm import PeerComm, ShadowCommProtocol
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.shadow_comm import AnswerGenerator, ShadowComm
from server.async_comm.sync_protocol import (
    ConflictResolverProtocol,
    SyncProtocol,
    SyncProtocolResult,
)
from server.async_comm.types import ConversationEvent, PeerSnapshot, VectorClock

__all__ = [
    "AnswerGenerator",
    "ConflictResolver",
    "ConflictResolverProtocol",
    "ConversationEvent",
    "ConversationLog",
    "DEFAULT_AUTO_CONFIRM_THRESHOLD",
    "DEFAULT_CONFLICT_THRESHOLD",
    "DEFAULT_SNAPSHOT_POLICY",
    "DEFAULT_SNAPSHOT_TTL_DAYS",
    "EVENT_ASK",
    "EVENT_CONFIRMED",
    "EVENT_NEEDS_HUMAN_REVIEW",
    "EVENT_REALTIME_ANSWER",
    "EVENT_REVISED",
    "EVENT_SIMULATED_ANSWER",
    "Mailbox",
    "PeerComm",
    "PeerSnapshot",
    "PeerSnapshotManager",
    "ResolutionResult",
    "STATUS_CONFIRMED",
    "STATUS_DELIVERED",
    "STATUS_NEEDS_HUMAN_REVIEW",
    "STATUS_PENDING_DELIVERY",
    "STATUS_REVISED",
    "ShadowComm",
    "ShadowCommProtocol",
    "SimilarityFunc",
    "SyncProtocol",
    "SyncProtocolResult",
    "VectorClock",
    "default_similarity",
]
