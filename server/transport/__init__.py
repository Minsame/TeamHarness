"""transport 领域包：通信拓扑抽象（Task 5）。

对应技术方案「通信拓扑可切换」需求，对外提供拓扑无关的传输接口：
- SyncTransport Protocol：deliver / fetch / is_peer_reachable / discover_peers
- 三种实现：CentralSyncTransport / P2PSyncTransport / HybridSyncTransport（Task 6 提供）
- 数据结构：PeerInfo / Message / SyncResult

设计原则：业务层通过 SyncTransport 接口通信，不感知底层拓扑
（central / p2p / hybrid），在线与离线是同一接口的两种执行路径。
"""

from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
    VALID_TOPOLOGIES,
    SyncTransport,
)
from server.transport.types import Message, PeerInfo, SyncResult

__all__ = [
    "Message",
    "PeerInfo",
    "SyncResult",
    "SyncTransport",
    "TOPOLOGY_CENTRAL",
    "TOPOLOGY_HYBRID",
    "TOPOLOGY_P2P",
    "VALID_TOPOLOGIES",
]
