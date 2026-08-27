"""同步传输抽象（拓扑无关接口）。

定义 SyncTransport Protocol，三种实现（Task 6 提供）：
- CentralSyncTransport：中央服务中转（复用现有 httpx）
- P2PSyncTransport：去中心化直连（WebSocket）
- HybridSyncTransport：混合（P2P 优先，降级中央）

业务层通过此接口通信，不感知底层拓扑。

对应 Task 5 SubTask 5.1。
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from server.transport.types import Message, PeerInfo, SyncResult

# 拓扑类型常量
TOPOLOGY_CENTRAL = "central"
TOPOLOGY_P2P = "p2p"
TOPOLOGY_HYBRID = "hybrid"
VALID_TOPOLOGIES = {TOPOLOGY_CENTRAL, TOPOLOGY_P2P, TOPOLOGY_HYBRID}


@runtime_checkable
class SyncTransport(Protocol):
    """同步传输抽象 — 拓扑无关接口。

    三种实现：CentralSyncTransport / P2PSyncTransport / HybridSyncTransport
    业务层通过此接口通信，不感知底层拓扑。
    """

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """投递消息给 peer（实时或暂存）。

        peer 可达时实时投递；不可达时存入本地 outbox 等待下次同步。
        """
        ...

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """拉取 peer 的消息（增量）。

        since_vector_clock 非空时只返回该版本向量之后的消息。
        """
        ...

    def is_peer_reachable(self, peer_id: str) -> bool:
        """peer 是否当前可达。"""
        ...

    def discover_peers(self) -> list[PeerInfo]:
        """发现可用 peer 列表。"""
        ...


__all__ = [
    "SyncTransport",
    "TOPOLOGY_CENTRAL",
    "TOPOLOGY_HYBRID",
    "TOPOLOGY_P2P",
    "VALID_TOPOLOGIES",
]
