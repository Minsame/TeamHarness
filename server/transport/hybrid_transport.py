"""混合拓扑传输（实现 SyncTransport）。

对应 Task 6 SubTask 6.3：组合 CentralSyncTransport + P2PSyncTransport。
- deliver: P2P 可达时直连投递；不可达时降级中央中转
- fetch: P2P 可达时直连拉取；不可达时降级中央
- is_peer_reachable: P2P 可达或中央报告在线均可
- discover_peers: 合并两个来源（按 peer_id 去重）

设计要点：
- "P2P 优先"以 reachability 为判据：P2P 可达即用 P2P 路径，
  不可达才走中央；不在 P2P 内部失败时立即降级（避免双投递副作用）
- 响应体状态标志聚合：SyncResult.success 汇总实际执行路径返回的信号，
  不在单一返回点硬编码 False
- discover_peers 合并去重：相同 peer_id 以 P2P（本地视图）优先保留
"""

from __future__ import annotations

from server.transport.central_transport import CentralSyncTransport
from server.transport.p2p_transport import P2PSyncTransport
from server.transport.types import Message, PeerInfo, SyncResult


class HybridSyncTransport:
    """混合拓扑传输：P2P 优先 + 中央降级。

    构造参数：
        central: CentralSyncTransport 实例（降级通道）
        p2p:     P2PSyncTransport 实例（优先通道）
    """

    def __init__(self, central: CentralSyncTransport, p2p: P2PSyncTransport) -> None:
        self.central = central
        self.p2p = p2p

    # ------------------------------------------------------------------
    # SyncTransport 接口实现
    # ------------------------------------------------------------------

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """P2P 可达时直连投递；不可达时降级中央中转。

        空消息列表：聚合无失败信号 → success=True（与子传输语义一致）。
        """
        if not messages:
            return SyncResult(success=True, delivered_count=0)

        # P2P 优先：可达即用 P2P 路径
        if self.p2p.is_peer_reachable(peer_id):
            return self.p2p.deliver(peer_id, messages)

        # P2P 不可达 → 降级中央中转
        return self.central.deliver(peer_id, messages)

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """P2P 可达时直连拉取；不可达时降级中央。"""
        if self.p2p.is_peer_reachable(peer_id):
            return self.p2p.fetch(peer_id, since_vector_clock)
        return self.central.fetch(peer_id, since_vector_clock)

    def is_peer_reachable(self, peer_id: str) -> bool:
        """P2P 可达或中央报告在线均可。"""
        if self.p2p.is_peer_reachable(peer_id):
            return True
        return self.central.is_peer_reachable(peer_id)

    def discover_peers(self) -> list[PeerInfo]:
        """合并 P2P 与中央两来源的 peer 列表，按 peer_id 去重。

        相同 peer_id 以 P2P（本地视图）优先保留。
        """
        merged: dict[str, PeerInfo] = {}
        # 先放 P2P（本地视图优先）
        for p in self.p2p.discover_peers():
            merged[p.peer_id] = p
        # 中央来源补充：P2P 未发现的 peer 才追加
        for p in self.central.discover_peers():
            if p.peer_id not in merged:
                merged[p.peer_id] = p
        return list(merged.values())


__all__ = ["HybridSyncTransport"]
