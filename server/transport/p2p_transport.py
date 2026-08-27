"""P2P 直连传输（实现 SyncTransport）。

对应 Task 6 SubTask 6.2：基于 WebSocket 长连接的去中心化通信。
- deliver: 找到 peer 的活跃 WS 连接发送；无连接则存本地 outbox
- fetch: 通过 WS 请求增量消息；连接不可用时回退本地 incoming 缓存
- is_peer_reachable: 检查是否有活跃 WS 连接
- discover_peers: 从内置 PeerRegistry 获取已知 peer

websockets 库可选：未安装时降级为 Stub（参考 gotchas 规则）。
Stub 类须 `__init__(self, *args, **kwargs): pass` + 各方法 no-op 返回，
此时所有连接尝试返回 Stub（视为非活跃），deliver 永远落入 outbox，
is_peer_reachable 永远 False（除非通过 attach_connection 注入 mock 连接）。
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any

from server.transport.types import Message, PeerInfo, SyncResult

# 尝试导入 websockets；失败则降级为 Stub
try:
    import websockets  # noqa: F401
    _HAS_WEBSOCKETS = True
except ImportError:
    _HAS_WEBSOCKETS = False


class _StubWSConnection:
    """websockets 不可用时的连接 Stub：签名兼容，所有方法 no-op。

    满足工程规则：`__init__(self, *args, **kwargs): pass` + 各方法 no-op 返回。
    实例被 `_is_connection_alive` 判定为"非活跃"，所有 IO 操作无副作用。
    """

    def __init__(self, *args, **kwargs) -> None:  # noqa: D401
        pass

    def send(self, data: str) -> None:
        return None

    def recv(self) -> str:
        return ""

    def close(self) -> None:
        return None

    @property
    def closed(self) -> bool:
        return True  # Stub 永远视为已关闭


class P2PSyncTransport:
    """P2P 直连传输：基于 WebSocket 长连接。

    构造参数：
        listen_host: 监听地址（默认 0.0.0.0）
        listen_port: 监听端口（默认 7421）
        peers: 已知 peer ID 列表（初始化内置 PeerRegistry）
    """

    def __init__(
        self,
        listen_host: str = "0.0.0.0",
        listen_port: int = 7421,
        peers: list[str] | None = None,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = listen_port
        self._peer_registry: dict[str, PeerInfo] = {}
        self._connections: dict[str, Any] = {}  # peer_id -> connection
        self._outbox: dict[str, list[Message]] = {}
        self._incoming: dict[str, list[Message]] = {}  # peer_id -> 收到的消息（供 fetch）
        # 初始化 PeerRegistry
        if peers:
            for pid in peers:
                self._register_peer_internal(pid)

    @property
    def has_websockets(self) -> bool:
        """websockets 库是否可用。"""
        return _HAS_WEBSOCKETS

    # ------------------------------------------------------------------
    # SyncTransport 接口实现
    # ------------------------------------------------------------------

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """投递消息给 peer：有活跃 WS 连接则实时发送，否则存入本地 outbox。

        空消息列表：聚合无失败信号 → success=True，delivered_count=0
        （不保留"待定"语义）。
        """
        if not messages:
            return SyncResult(success=True, delivered_count=0)

        conn = self._connections.get(peer_id)
        if conn is not None and self._is_connection_alive(conn):
            try:
                payload = json.dumps(
                    {
                        "type": "deliver",
                        "messages": [self._message_to_dict(m) for m in messages],
                    }
                )
                self._send_via_ws(conn, payload)
                delivered_ids = [m.message_id for m in messages]
                # 聚合：发送成功 + 无失败 = success
                return SyncResult(
                    success=True,
                    delivered_count=len(messages),
                    delivered_message_ids=delivered_ids,
                )
            except Exception as exc:  # noqa: BLE001
                # 连接故障 → 暂存 outbox，标记 pending
                self._outbox.setdefault(peer_id, []).extend(messages)
                return SyncResult(
                    success=False,
                    pending_count=len(messages),
                    error=f"WS send failed, stored in outbox: {exc}",
                )
        # 无活跃连接 → 暂存 outbox，标记 pending
        self._outbox.setdefault(peer_id, []).extend(messages)
        return SyncResult(
            success=False,
            pending_count=len(messages),
            error="no active WS connection, stored in outbox",
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """通过 WS 拉取 peer 的增量消息；连接不可用则回退本地 incoming 缓存。"""
        conn = self._connections.get(peer_id)
        if conn is not None and self._is_connection_alive(conn):
            try:
                req = json.dumps(
                    {
                        "type": "fetch",
                        "since_vector_clock": since_vector_clock or {},
                    }
                )
                self._send_via_ws(conn, req)
                raw = self._recv_via_ws(conn)
                if raw:
                    data = json.loads(raw)
                    if isinstance(data, dict):
                        items = list(data.get("messages") or [])
                        return [self._message_from_dict(it) for it in items]
            except Exception:  # noqa: BLE001
                pass
        # 无连接或失败 → 返回本地 incoming 缓存
        return list(self._incoming.get(peer_id, []))

    def is_peer_reachable(self, peer_id: str) -> bool:
        """检查 peer 是否有活跃 WS 连接。"""
        conn = self._connections.get(peer_id)
        return conn is not None and self._is_connection_alive(conn)

    def discover_peers(self) -> list[PeerInfo]:
        """返回内置 PeerRegistry 中的所有已知 peer。"""
        return list(self._peer_registry.values())

    # ------------------------------------------------------------------
    # 连接管理（供测试与上层注入 mock 连接）
    # ------------------------------------------------------------------

    def register_peer(self, peer_id: str, endpoint: str = "", online: bool = False) -> None:
        """注册/更新 peer 信息。"""
        self._register_peer_internal(peer_id, endpoint=endpoint, online=online)

    def attach_connection(self, peer_id: str, conn: Any) -> None:
        """注入已有连接（测试与上层用，绕过 WS 建链）。

        注入的连接需提供 `send(data: str)` 方法（同步或返回 coroutine）。
        Stub 连接也可注入，但 `_is_connection_alive` 会判定为非活跃。
        """
        self._connections[peer_id] = conn
        if peer_id in self._peer_registry:
            self._peer_registry[peer_id].online = self._is_connection_alive(conn)

    def detach_connection(self, peer_id: str) -> None:
        """移除并关闭连接。"""
        conn = self._connections.pop(peer_id, None)
        if conn is not None:
            try:
                conn.close()
            except Exception:  # noqa: BLE001
                pass
        if peer_id in self._peer_registry:
            self._peer_registry[peer_id].online = False

    def open_connection(self, peer_id: str, host: str, port: int) -> Any:
        """尝试建立 WS 连接。

        websockets 不可用时返回 _StubWSConnection 实例（非活跃，不写入 _connections）。
        连接失败返回 None。
        """
        if not _HAS_WEBSOCKETS:
            return _StubWSConnection(host, port)
        try:
            from websockets.sync.client import connect as ws_sync_connect

            conn = ws_sync_connect(f"ws://{host}:{port}")
            self._connections[peer_id] = conn
            if peer_id in self._peer_registry:
                self._peer_registry[peer_id].online = True
            return conn
        except Exception:  # noqa: BLE001
            return None

    def push_incoming(self, peer_id: str, messages: list[Message]) -> None:
        """向 incoming 缓存推消息（测试与上层注入用）。"""
        self._incoming.setdefault(peer_id, []).extend(messages)

    @property
    def outbox(self) -> dict[str, list[Message]]:
        """暂存 outbox（测试断言用）。"""
        return self._outbox

    @property
    def incoming(self) -> dict[str, list[Message]]:
        """incoming 缓存（测试断言用）。"""
        return self._incoming

    @property
    def peer_registry(self) -> dict[str, PeerInfo]:
        """内置 PeerRegistry（测试断言用）。"""
        return self._peer_registry

    # ------------------------------------------------------------------
    # Task 25：P2P 模式 tags 同步（admin 权威源）
    # ------------------------------------------------------------------

    def broadcast_tags_sync(self, tags_map: dict[str, list[str]]) -> SyncResult:
        """admin 节点广播 tags_sync 消息给所有已知 peer。

        Task 25 SubTask 25.4：
        - msg_type = "tags_sync"
        - payload 含全部 peer_id → tags 映射（覆盖式更新，幂等）
        - 无活跃连接的 peer 消息暂存 outbox（待其上线时由 fetch 拉取）

        Args:
            tags_map: {peer_id: [tag1, tag2, ...]} 全量映射

        Returns:
            SyncResult：聚合各 peer 投递结果
        """
        if not tags_map:
            # 空映射：视为无操作（不广播）
            return SyncResult(success=True, delivered_count=0)

        # 构造 tags_sync 消息：广播（recipient_id 空表示广播）
        # 每个已知 peer 都投递一份
        all_peer_ids = list(self._peer_registry.keys())
        if not all_peer_ids:
            return SyncResult(success=True, delivered_count=0)

        # 构造单条广播消息（recipient_id="" 表示广播）
        broadcast_msg = Message(
            message_id=str(uuid.uuid4()),
            event_id=str(uuid.uuid4()),
            sender_id="",  # admin 节点 ID 由上层 PeerComm 填充（这里广播方匿名）
            recipient_id="",  # 空=广播
            msg_type="tags_sync",
            payload={"tags_map": {pid: list(tags) for pid, tags in tags_map.items()}},
            timestamp=datetime.now(timezone.utc).isoformat(),
        )

        # 逐 peer 投递（避免单连接广播失败影响所有 peer）
        total_delivered = 0
        total_pending = 0
        total_failed = 0
        delivered_ids: list[str] = []
        errors: list[str] = []
        for pid in all_peer_ids:
            result = self.deliver(pid, [broadcast_msg])
            total_delivered += result.delivered_count
            total_pending += result.pending_count
            total_failed += result.failed_count
            delivered_ids.extend(result.delivered_message_ids)
            if result.error:
                errors.append(f"{pid}: {result.error}")

        # 聚合：只要有任一 peer 投递成功（含 pending），整体视为可继续
        # 若全部失败 → success=False
        success = total_delivered > 0 or total_pending > 0
        return SyncResult(
            success=success,
            delivered_count=total_delivered,
            failed_count=total_failed,
            pending_count=total_pending,
            delivered_message_ids=delivered_ids,
            error="; ".join(errors) if errors else "",
        )

    def handle_tags_sync(self, payload: dict) -> None:
        """非 admin peer 接收 tags_sync 消息后刷新本地 _peer_registry.capabilities。

        Task 25 SubTask 25.5：
        - 幂等性：多次接收覆盖式更新（不追加，直接替换 capabilities）
        - 不在 tags_map 中的 peer 不被修改
        - 新增的 peer_id 自动注册到 _peer_registry

        Args:
            payload: tags_sync 消息的 payload 字段，含 "tags_map" 键。
        """
        if not isinstance(payload, dict):
            return
        tags_map = payload.get("tags_map")
        if not isinstance(tags_map, dict):
            return

        for peer_id, tags in tags_map.items():
            peer_id = str(peer_id)
            if not peer_id:
                continue
            # 规范化 tags 为 list[str]
            if isinstance(tags, str):
                # 兼容逗号分隔字符串
                tag_list = [t.strip() for t in tags.split(",") if t.strip()]
            elif isinstance(tags, list):
                tag_list = [str(t).strip() for t in tags if str(t).strip()]
            else:
                tag_list = []

            # 自动注册新 peer（不在 _peer_registry 中则补登记）
            if peer_id not in self._peer_registry:
                self._register_peer_internal(peer_id)
            # 覆盖式更新 capabilities
            self._peer_registry[peer_id].capabilities = tag_list

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _register_peer_internal(
        self,
        peer_id: str,
        *,
        endpoint: str = "",
        online: bool = False,
    ) -> None:
        self._peer_registry[peer_id] = PeerInfo(
            peer_id=peer_id,
            endpoint=endpoint or f"{self.listen_host}:{self.listen_port}",
            online=online,
        )
        self._outbox.setdefault(peer_id, [])
        self._incoming.setdefault(peer_id, [])

    def _is_connection_alive(self, conn: Any) -> bool:
        """连接是否活跃。Stub 与已关闭连接视为非活跃。"""
        if isinstance(conn, _StubWSConnection):
            return False
        closed = getattr(conn, "closed", None)
        if closed is True:
            return False
        # 未知连接类型，按有无 send 方法 + _closed 标志判定
        if not hasattr(conn, "send"):
            return False
        return not bool(getattr(conn, "_closed", False))

    def _send_via_ws(self, conn: Any, data: str) -> None:
        """通过 WS 发送：兼容同步 / 异步 send 接口。"""
        send_fn = getattr(conn, "send", None)
        if send_fn is None:
            raise RuntimeError("connection has no send method")
        result = send_fn(data)
        # 异步 send 返回 coroutine 时驱动完成
        if _is_coroutine(result):
            _run_coroutine_sync(result)

    def _recv_via_ws(self, conn: Any) -> str:
        """通过 WS 接收一条消息：兼容同步 / 异步 recv 接口。"""
        recv_fn = getattr(conn, "recv", None)
        if recv_fn is None:
            return ""
        try:
            result = recv_fn()
        except Exception:  # noqa: BLE001
            return ""
        if _is_coroutine(result):
            result = _run_coroutine_sync(result)
        return str(result) if result else ""

    @staticmethod
    def _message_to_dict(m: Message) -> dict[str, Any]:
        return {
            "message_id": m.message_id,
            "event_id": m.event_id,
            "sender_id": m.sender_id,
            "recipient_id": m.recipient_id,
            "msg_type": m.msg_type,
            "payload": dict(m.payload),
            "timestamp": m.timestamp,
            "in_reply_to": m.in_reply_to,
            "sender_key_hash": m.sender_key_hash,
            "signature": m.signature,
        }

    @staticmethod
    def _message_from_dict(d: dict[str, Any]) -> Message:
        return Message(
            message_id=str(d.get("message_id", "")),
            event_id=str(d.get("event_id", "")),
            sender_id=str(d.get("sender_id", "")),
            recipient_id=str(d.get("recipient_id", "")),
            msg_type=str(d.get("msg_type", "")),
            payload=dict(d.get("payload") or {}),
            timestamp=str(d.get("timestamp", "")),
            in_reply_to=str(d.get("in_reply_to", "")),
            sender_key_hash=str(d.get("sender_key_hash", "")),
            signature=str(d.get("signature", "")),
        )


# ---------------------------------------------------------------------------
# 异步 → 同步 桥接工具
# ---------------------------------------------------------------------------


def _is_coroutine(obj: Any) -> bool:
    import asyncio

    return asyncio.iscoroutine(obj) or asyncio.isfuture(obj)


def _run_coroutine_sync(coro: Any) -> Any:
    """在同步上下文中驱动 coroutine 完成。"""
    import asyncio

    try:
        loop = asyncio.get_event_loop()
        if loop.is_running():
            # 已有 loop 在跑（罕见），创建 task 后立即 yield
            # 注意：此处无法阻塞等待，返回 None
            task = loop.create_task(coro)
            # 立即让出一次控制权（best-effort）
            return None
        return loop.run_until_complete(coro)
    except RuntimeError:
        # 无事件循环 → 创建新的
        return asyncio.run(coro)


__all__ = ["P2PSyncTransport"]
