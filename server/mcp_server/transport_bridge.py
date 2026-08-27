"""Transport Bridge：桥接 async_comm + transport 层。

为 MCP Server 和 CLI 提供统一的底层调用入口。根据 ClientConfig.topology
选择 transport 实现（central / p2p / hybrid），并初始化 mailbox /
conversation_log / peer_snapshot_manager / peer_comm / shadow_comm /
sync_protocol 等组件。

对应 Task 20。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.async_comm.conflict_resolver import ConflictResolver
from server.async_comm.conversation_log import ConversationLog, event_to_dict
from server.async_comm.mailbox import Mailbox
from server.async_comm.peer_comm import PeerComm
from server.async_comm.peer_snapshot import PeerSnapshotManager
from server.async_comm.shadow_comm import ShadowComm
from server.async_comm.sync_protocol import SyncProtocol
from server.client.config import ClientConfig
from server.transport.central_transport import CentralSyncTransport
from server.transport.hybrid_transport import HybridSyncTransport
from server.transport.p2p_transport import P2PSyncTransport
from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
    SyncTransport,
)

logger = logging.getLogger(__name__)


@dataclass
class BridgeConfig:
    """Transport Bridge 配置。

    member_id: 本地成员 ID。
    base_dir: async_comm 数据目录（.teamharness/async_comm/）。
    """

    member_id: str = ""
    base_dir: str = ""


class TransportBridge:
    """桥接 async_comm + transport 层。

    初始化所有组件（transport / mailbox / conversation_log /
    peer_snapshot_manager / peer_comm / shadow_comm / sync_protocol），
    为 MCP Server 和 CLI 提供统一入口。

    使用：
        bridge = TransportBridge(config)
        result = bridge.ask_peer("bob", "如何处理 X？")
        peers = bridge.list_peers()
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        transport: SyncTransport | None = None,
    ) -> None:
        """从 ClientConfig 初始化所有组件。

        Args:
            config: 客户端配置（含 topology / server_url / api_key / member_id 等）。
            transport: 可选的预构建 transport（测试注入用）；None 时按
                config.topology 自动构建。遵循 RecallClient 的依赖注入模式。
        """
        self.config = config
        self.member_id = config.member_id or "default"

        # async_comm 数据目录：.teamharness/async_comm/
        base_dir = config.resolve_teamharness_dir() / "async_comm"
        base_dir.mkdir(parents=True, exist_ok=True)
        self.base_dir = base_dir

        # 1. 根据 config.topology 选择 transport（或使用注入的 transport）
        self.transport: SyncTransport = (
            transport if transport is not None else self._build_transport(config)
        )

        # 2. 初始化 mailbox / conversation_log / peer_snapshot_manager
        self.mailbox = Mailbox(base_dir, self.member_id)
        self.conversation_log = ConversationLog(base_dir / "conversation.jsonl")
        async_comm_cfg = config.resolve_async_comm_config()
        self.peer_snapshot_manager = PeerSnapshotManager(
            base_dir,
            ttl_days=int(async_comm_cfg.get("snapshot_ttl_days", 30)),
            snapshot_policy=str(async_comm_cfg.get("snapshot_policy", "on_demand")),
        )

        # 3. 初始化 shadow_comm / peer_comm / sync_protocol
        self.shadow_comm = ShadowComm(
            mailbox=self.mailbox,
            peer_snapshot_manager=self.peer_snapshot_manager,
            conversation_log=self.conversation_log,
            member_id=self.member_id,
        )

        self.peer_comm = PeerComm(
            transport=self.transport,
            mailbox=self.mailbox,
            conversation_log=self.conversation_log,
            peer_snapshot_manager=self.peer_snapshot_manager,
            member_id=self.member_id,
            network_check_interval_seconds=config.network_check_interval_seconds,
            shadow_comm=self.shadow_comm,
        )

        self.conflict_resolver = ConflictResolver(
            auto_confirm_threshold=float(
                async_comm_cfg.get("auto_confirm_threshold", 0.8)
            ),
            conflict_threshold=float(async_comm_cfg.get("conflict_threshold", 0.3)),
        )

        self.sync_protocol = SyncProtocol(
            transport=self.transport,
            mailbox=self.mailbox,
            conversation_log=self.conversation_log,
            peer_snapshot_manager=self.peer_snapshot_manager,
            member_id=self.member_id,
            conflict_resolver=self.conflict_resolver,
        )

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def ask_peer(
        self,
        peer_id: str,
        question: str,
        *,
        tag: str = "",
        in_reply_to: str = "",
    ) -> dict[str, Any]:
        """向 peer 提问。

        在线时走实时通信路径，离线时自动降级为影子联络。

        Task 26 扩展：
        - ``peer_id`` 与 ``tag`` 二选一：``peer_id`` 非空时走点对点通信（``tag`` 忽略）；
          ``peer_id`` 为空且 ``tag`` 非空时按标签路由到匹配候选 peer。
        - ``in_reply_to`` 透传到 ``peer_comm.ask_peer`` 用于回复链关联。

        Args:
            peer_id: 对方 peer ID（与 tag 二选一；同时提供时优先 peer_id）。
            question: 提问内容。
            tag: 按标签路由（如 "运维"），peer_id 为空时生效。
            in_reply_to: 关联的前驱事件 ID（回复链，可选）。

        Returns:
            dict 含回答内容、是否降级、事件 ID 等；tag 路由时额外携带 ``tag`` 字段。
        """
        # tag 模式：peer_id 为空且 tag 非空
        use_tag_routing = bool(tag) and not peer_id
        event = self.peer_comm.ask_peer(
            peer_id,
            question,
            tag=tag if use_tag_routing else "",
            in_reply_to=in_reply_to,
        )
        result: dict[str, Any] = {
            "event_id": event.event_id,
            "event_type": event.event_type,
            "peer_id": event.peer_id,
            "answer": str(event.payload.get("answer", "")),
            "degraded": event.degraded,
            "realtime": event.realtime,
            "timestamp": event.timestamp,
            "based_on": event.based_on,
            "snapshot_stale": event.snapshot_stale,
            "in_reply_to": event.in_reply_to,
        }
        if use_tag_routing:
            result["tag"] = tag
        return result

    def resume_conversation(self, peer_id: str) -> dict[str, Any]:
        """恢复与指定 peer 的暂停对话（Task 26 / Task 27）。

        委托 ``peer_comm.resume_conversation``：基于 in_reply_to 链重建上下文，
        返回历史事件列表。peer 无暂停对话时返回空事件列表。

        Args:
            peer_id: 对方 peer ID。

        Returns:
            dict 含 peer_id / resumed / events / event_count。
            resumed 为 True 表示有 paused/timeout_disconnect 对话被恢复。
        """
        events = self.peer_comm.resume_conversation(peer_id)
        event_dicts = [event_to_dict(e) for e in events]
        return {
            "peer_id": peer_id,
            "resumed": len(event_dicts) > 0,
            "events": event_dicts,
            "event_count": len(event_dicts),
        }

    def list_peers(self) -> list[dict[str, Any]]:
        """列出已知 peer。

        Returns:
            peer 信息字典列表（含 peer_id / online / endpoint 等）。
        """
        peers = self.transport.discover_peers()
        return [
            {
                "peer_id": p.peer_id,
                "agent_id": p.agent_id,
                "endpoint": p.endpoint,
                "online": p.online,
                "last_seen": p.last_seen,
                "capabilities": list(p.capabilities),
            }
            for p in peers
        ]

    def share_asset(
        self,
        asset_id: str,
        to_peer_id: str,
        content: dict | None = None,
    ) -> dict[str, Any]:
        """资产定向共享。

        peer 可达时实时推送，不可达时写入 outbox 等待上线同步。

        Args:
            asset_id: 资产 ID。
            to_peer_id: 接收方 peer ID。
            content: 资产内容（可选）。

        Returns:
            dict 含投递结果（success / delivered_count / pending_count 等）。
        """
        result = self.peer_comm.share_asset(
            asset_id,
            to_peer_id,
            asset_content=content,
        )
        return {
            "success": result.success,
            "delivered_count": result.delivered_count,
            "failed_count": result.failed_count,
            "pending_count": result.pending_count,
            "error": result.error,
            "delivered_message_ids": list(result.delivered_message_ids),
        }

    def search_team_assets(
        self,
        query: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        """搜索团队资产（复用 RecallClient）。

        Args:
            query: 搜索查询字符串。
            limit: 返回数量上限。

        Returns:
            资产信息字典列表（含 asset_id / title / relevance_score 等）。
        """
        from server.client.recall_client import RecallClient

        client = RecallClient(self.config, transport=self.transport)
        agent_id = self.config.agent_id or self.member_id
        result = client.recall_list(
            agent_id=agent_id,
            query=query,
            limit=limit,
        )
        return [
            {
                "asset_id": it.asset_id,
                "type": it.type,
                "title": it.title,
                "tags": list(it.tags),
                "relevance_score": it.relevance_score,
                "git_path": it.git_path,
                "module_path": it.module_path,
            }
            for it in result.items
        ]

    def get_conversation_log(
        self,
        peer_id: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """获取交流报告（含实时 + 影子）。

        Args:
            peer_id: 指定对方 peer ID；None 表示加载全部。
            limit: 返回数量上限。

        Returns:
            事件字典列表（按时间升序）。
        """
        if peer_id is not None:
            events = self.conversation_log.load_by_peer(peer_id, limit=limit)
        else:
            events = self.conversation_log.load_all(limit=limit)
        return [event_to_dict(e) for e in events]

    # ------------------------------------------------------------------
    # 内部：transport 选型
    # ------------------------------------------------------------------

    def _build_transport(self, config: ClientConfig) -> SyncTransport:
        """根据 config.topology 选择 transport 实现。

        central：CentralSyncTransport（基于 httpx 调中央服务 API）
        p2p：P2PSyncTransport（基于 WebSocket 直连）
        hybrid：HybridSyncTransport（P2P 优先 + 中央降级）

        Args:
            config: 客户端配置。

        Returns:
            SyncTransport 实例。
        """
        topology = config.topology
        if topology == TOPOLOGY_P2P:
            return P2PSyncTransport(peers=list(config.peers) or None)
        if topology == TOPOLOGY_HYBRID:
            central = CentralSyncTransport(
                server_url=config.server_url,
                api_key=config.api_key,
                timeout=config.request_timeout_seconds,
            )
            p2p = P2PSyncTransport(peers=list(config.peers) or None)
            return HybridSyncTransport(central, p2p)
        # 默认 central
        return CentralSyncTransport(
            server_url=config.server_url,
            api_key=config.api_key,
            timeout=config.request_timeout_seconds,
        )


__all__ = [
    "BridgeConfig",
    "TransportBridge",
]
