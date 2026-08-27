"""中央服务中转传输（实现 SyncTransport）。

对应 Task 6 SubTask 6.1：基于 httpx 同步客户端调用中央服务的通信 API：
- POST /v1/comm/deliver  → 投递消息给指定 peer
- POST /v1/comm/fetch    → 拉取指定 peer 的增量消息
- GET  /v1/comm/peer/{peer_id}/status → peer 在线状态（中央服务统一感知）
- GET  /v1/comm/peers    → 发现可用 peer 列表

设计要点：
- 复用 httpx.Client（同步），与 server/client/recall_client.py 保持一致风格
- 响应体状态标志聚合：SyncResult.success 汇总服务端返回 + 网络层 + HTTP 状态等多路信号，
  不在单一返回点硬编码 False
- 空消息列表 deliver：HTTP 200 + delivered_count=0 → success=True（语义上无失败）
- 错误降级：网络异常 / 非 2xx → 返回失败 SyncResult（携带 error），不抛异常给上层
"""

from __future__ import annotations

from typing import Any

import httpx

from server.transport.types import Message, PeerInfo, SyncResult


class CentralSyncTransport:
    """中央服务中转传输：所有消息经中央服务转发。

    构造参数：
        server_url: 中央服务基址（如 https://th.example.com）
        api_key:    成员 API Key（鉴权 Bearer）
        timeout:    HTTP 请求超时（秒）
    """

    def __init__(
        self,
        server_url: str,
        api_key: str,
        timeout: int = 15,
        *,
        http_client: httpx.BaseClient | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._http_client = http_client

    # ------------------------------------------------------------------
    # SyncTransport 接口实现
    # ------------------------------------------------------------------

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        """POST /v1/comm/deliver 投递消息给 peer。

        空消息列表：HTTP 200 + delivered_count=0 → success=True（无失败信号）。
        网络异常 / 非 2xx → 失败 SyncResult，不抛异常。
        """
        if not messages:
            # 空消息：聚合状态 = 无失败信号 = success
            return SyncResult(success=True, delivered_count=0)

        payload: dict[str, Any] = {
            "peer_id": peer_id,
            "messages": [self._message_to_dict(m) for m in messages],
        }
        try:
            resp = self._client().post(
                f"{self.server_url}/v1/comm/deliver",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except (httpx.HTTPError, OSError) as exc:
            return SyncResult(
                success=False,
                failed_count=len(messages),
                error=f"network error: {exc}",
            )

        if resp.status_code >= 400:
            return SyncResult(
                success=False,
                failed_count=len(messages),
                error=f"HTTP {resp.status_code}: {self._safe_text(resp)}",
            )

        data = self._safe_json(resp)
        # 响应体状态标志聚合：汇总 HTTP 状态、服务端 accepted/delivered_count 多路信号
        delivered = int(data.get("delivered_count", 0))
        failed = int(data.get("failed_count", 0))
        pending = int(data.get("pending_count", 0))
        delivered_ids = list(data.get("delivered_message_ids") or [])
        server_ok = bool(data.get("accepted", True))
        # 聚合：服务端 accepted + HTTP 200 + 无失败计数 = success
        success = server_ok and resp.status_code < 400 and failed == 0
        return SyncResult(
            success=success,
            delivered_count=delivered,
            failed_count=failed,
            pending_count=pending,
            delivered_message_ids=delivered_ids,
            error=str(data.get("error", "")) if not success else "",
        )

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        """POST /v1/comm/fetch 拉取 peer 的增量消息。

        since_vector_clock 非空时一并提交，由服务端做增量过滤。
        """
        payload: dict[str, Any] = {"peer_id": peer_id}
        if since_vector_clock:
            payload["since_vector_clock"] = since_vector_clock
        try:
            resp = self._client().post(
                f"{self.server_url}/v1/comm/fetch",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except (httpx.HTTPError, OSError):
            return []
        if resp.status_code >= 400:
            return []
        data = self._safe_json(resp)
        items = list(data.get("messages") or [])
        return [self._message_from_dict(it) for it in items]

    def is_peer_reachable(self, peer_id: str) -> bool:
        """GET /v1/comm/peer/{peer_id}/status 查询 peer 在线状态。"""
        try:
            resp = self._client().get(
                f"{self.server_url}/v1/comm/peer/{peer_id}/status",
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except (httpx.HTTPError, OSError):
            return False
        if resp.status_code >= 400:
            return False
        data = self._safe_json(resp)
        return bool(data.get("online", False))

    def discover_peers(self) -> list[PeerInfo]:
        """GET /v1/comm/peers 发现可用 peer 列表。"""
        try:
            resp = self._client().get(
                f"{self.server_url}/v1/comm/peers",
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except (httpx.HTTPError, OSError):
            return []
        if resp.status_code >= 400:
            return []
        data = self._safe_json(resp)
        items = list(data.get("peers") or [])
        return [self._peer_from_dict(it) for it in items]

    # ------------------------------------------------------------------
    # Task 25：按 tag 路由辅助（central 模式专用）
    # ------------------------------------------------------------------

    def fetch_team_members(self) -> list[PeerInfo]:
        """GET /v1/team/members 查询全部团队成员，从 Member.tags 构建 PeerInfo。

        Task 25 SubTask 25.2 / 25.3：
        - central 模式按 tag 路由的权威数据源（实时查 DB，不缓存）
        - PeerInfo.capabilities 从 Member.tags 直接读取
        - 网络异常 / 非 2xx → 返回空列表（不抛异常）

        Returns:
            PeerInfo 列表，每个 PeerInfo 的 capabilities = Member.tags。
        """
        try:
            resp = self._client().get(
                f"{self.server_url}/v1/team/members",
                headers=self._auth_headers(),
                timeout=self.timeout,
            )
        except (httpx.HTTPError, OSError):
            return []
        if resp.status_code >= 400:
            return []
        data = self._safe_json(resp)
        # GET /v1/team/members 返回 list[MemberInfo]
        items = data if isinstance(data, list) else list(data.get("members") or [])
        return [self._peer_from_member_info(it) for it in items]

    @staticmethod
    def _peer_from_member_info(d: dict[str, Any]) -> PeerInfo:
        """从 MemberInfo dict 构建 PeerInfo（capabilities = tags）。

        MemberInfo 字段：member_id / display_name / role / status / tags / created_by / created_at
        PeerInfo.capabilities 直接复用 Member.tags（任务要求）。
        """
        raw_tags = d.get("tags") or []
        if isinstance(raw_tags, list):
            tags = [str(t).strip() for t in raw_tags if str(t).strip()]
        elif isinstance(raw_tags, str):
            # 容错：JSON 字符串形式
            try:
                import json
                parsed = json.loads(raw_tags)
                tags = (
                    [str(t).strip() for t in parsed if str(t).strip()]
                    if isinstance(parsed, list)
                    else []
                )
            except (ValueError, TypeError):
                tags = []
        else:
            tags = []
        return PeerInfo(
            peer_id=str(d.get("member_id", "")),
            agent_id="",
            endpoint="",
            online=str(d.get("status", "active")) == "active",
            last_seen="",
            capabilities=tags,
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _client(self) -> httpx.BaseClient:
        if self._http_client is None:
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    @staticmethod
    def _safe_json(resp: httpx.Response) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}

    @staticmethod
    def _safe_text(resp: httpx.Response) -> str:
        try:
            return resp.text[:200]
        except Exception:  # noqa: BLE001
            return ""

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

    @staticmethod
    def _peer_from_dict(d: dict[str, Any]) -> PeerInfo:
        return PeerInfo(
            peer_id=str(d.get("peer_id", "")),
            agent_id=str(d.get("agent_id", "")),
            endpoint=str(d.get("endpoint", "")),
            online=bool(d.get("online", False)),
            last_seen=str(d.get("last_seen", "")),
            capabilities=list(d.get("capabilities") or []),
        )


__all__ = ["CentralSyncTransport"]
