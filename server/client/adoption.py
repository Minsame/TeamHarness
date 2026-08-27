"""采纳率上报（本地缓存 + 联网时批量 flush）。

对应 SubTask 6.10 + 技术方案 8.6「采纳率反馈」：
- 客户端记录每次召回/采纳事件到本地缓存（.teamharness/adoption-cache.json）
- 联网时由守护进程批量 flush 到服务端 POST /v1/metrics
- 失败时保留在本地缓存，下次重试（防丢数据）

事件类型：
- recall：召回被触发（recall_list / recall_read）
- view：用户查看资产详情
- adopt：用户采纳资产（复制/应用到当前会话）
- modify：用户修改资产后回写
- reject：用户显式拒绝资产

事件结构：
    {
      "event_id": "uuid",          # 客户端生成，用于幂等去重
      "event_type": "recall",
      "asset_id": "rule-backend-lint",
      "agent_id": "agent-1",
      "member_id": "alice",
      "module_path": "modules/backend",
      "timestamp": "2026-08-07T10:00:00Z",
      "metadata": { ... }           # 额外字段（如召回 score、修改前后 hash）
    }

丢数据风险（重点风险 🟡）：
- 本地缓存用 append-only JSONL + 定期压缩为 JSON 快照
- flush 失败时事件保留在本地，不删除
- 守护进程定期 flush，单次最多 100 条（避免单请求过大）
- 服务端基于 event_id 幂等去重（重试不会重复计数）
"""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from server.client.config import TEAMHARNESS_DIR
from server.client.placeholders import MetricsBatchAck, mock_metrics_batch

ADOPTION_CACHE_DIRNAME = TEAMHARNESS_DIR
ADOPTION_CACHE_FILENAME = "adoption-cache.json"
ADOPTION_EVENTS_LOG_FILENAME = "adoption-events.jsonl"
ADOPTION_STATE_FILENAME = "adoption-state.json"

# 单次 flush 最多事件数
DEFAULT_FLUSH_BATCH_SIZE = 100
# 本地缓存最大保留事件数（超过则告警，但不主动丢弃）
DEFAULT_MAX_CACHE_SIZE = 10000
# 事件类型枚举
EVENT_TYPES: tuple[str, ...] = ("recall", "view", "adopt", "modify", "reject")


@dataclass
class AdoptionEvent:
    """采纳率事件。"""

    event_id: str = ""
    event_type: str = "recall"  # recall / view / adopt / modify / reject
    asset_id: str = ""
    agent_id: str = ""
    member_id: str = ""
    module_path: str = ""
    timestamp: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.event_id:
            self.event_id = str(uuid.uuid4())
        if not self.timestamp:
            self.timestamp = _utcnow_iso()
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"非法事件类型: {self.event_type}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "AdoptionEvent":
        return cls(
            event_id=str(data.get("event_id", "")),
            event_type=str(data.get("event_type", "recall")),
            asset_id=str(data.get("asset_id", "")),
            agent_id=str(data.get("agent_id", "")),
            member_id=str(data.get("member_id", "")),
            module_path=str(data.get("module_path", "")),
            timestamp=str(data.get("timestamp", "")),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class FlushResult:
    """flush 操作结果。"""

    flushed: int = 0
    rejected: int = 0
    retained: int = 0  # 仍保留在本地的事件数（flush 失败的）
    error: str | None = None
    ack: MetricsBatchAck | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.rejected == 0


# ---------------------------------------------------------------------------
# AdoptionReporter
# ---------------------------------------------------------------------------


class AdoptionReporter:
    """采纳率上报器。

    使用：
        reporter = AdoptionReporter(config)
        reporter.record(AdoptionEvent(event_type='recall', asset_id='rule-1', agent_id='a-1'))
        result = reporter.flush()  # 联网时批量上传
    """

    def __init__(
        self,
        config: Any,  # ClientConfig
        *,
        http_client: httpx.BaseClient | None = None,
        flush_batch_size: int = DEFAULT_FLUSH_BATCH_SIZE,
        max_cache_size: int = DEFAULT_MAX_CACHE_SIZE,
    ) -> None:
        self.config = config
        self._http_client = http_client
        self.flush_batch_size = flush_batch_size
        self.max_cache_size = max_cache_size
        self.cache_dir = config.resolve_teamharness_dir()
        self.events_log_path = self.cache_dir / ADOPTION_EVENTS_LOG_FILENAME
        self.state_path = self.cache_dir / ADOPTION_STATE_FILENAME

    # ------------------------------------------------------------------
    # 记录事件
    # ------------------------------------------------------------------

    def record(self, event: AdoptionEvent) -> str:
        """记录事件到本地 JSONL（append-only）。

        返回 event_id。父目录自动创建。
        """
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with self.events_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        return event.event_id

    def record_recall(
        self,
        *,
        asset_id: str,
        agent_id: str | None = None,
        module_path: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """便捷：记录召回事件。"""
        return self.record(
            AdoptionEvent(
                event_type="recall",
                asset_id=asset_id,
                agent_id=agent_id or self.config.agent_id,
                member_id=self.config.member_id,
                module_path=module_path,
                metadata=metadata or {},
            )
        )

    def record_view(self, *, asset_id: str, agent_id: str | None = None, **kwargs: Any) -> str:
        return self.record(
            AdoptionEvent(
                event_type="view",
                asset_id=asset_id,
                agent_id=agent_id or self.config.agent_id,
                member_id=self.config.member_id,
                module_path=kwargs.pop("module_path", ""),
                metadata=kwargs,
            )
        )

    def record_adopt(self, *, asset_id: str, agent_id: str | None = None, **kwargs: Any) -> str:
        return self.record(
            AdoptionEvent(
                event_type="adopt",
                asset_id=asset_id,
                agent_id=agent_id or self.config.agent_id,
                member_id=self.config.member_id,
                module_path=kwargs.pop("module_path", ""),
                metadata=kwargs,
            )
        )

    def record_modify(
        self,
        *,
        asset_id: str,
        agent_id: str | None = None,
        old_hash: str = "",
        new_hash: str = "",
        **kwargs: Any,
    ) -> str:
        meta = {"old_hash": old_hash, "new_hash": new_hash, **kwargs}
        return self.record(
            AdoptionEvent(
                event_type="modify",
                asset_id=asset_id,
                agent_id=agent_id or self.config.agent_id,
                member_id=self.config.member_id,
                module_path=kwargs.pop("module_path", ""),
                metadata=meta,
            )
        )

    def record_reject(self, *, asset_id: str, agent_id: str | None = None, **kwargs: Any) -> str:
        return self.record(
            AdoptionEvent(
                event_type="reject",
                asset_id=asset_id,
                agent_id=agent_id or self.config.agent_id,
                member_id=self.config.member_id,
                module_path=kwargs.pop("module_path", ""),
                metadata=kwargs,
            )
        )

    # ------------------------------------------------------------------
    # 读取本地事件
    # ------------------------------------------------------------------

    def load_pending_events(self, *, limit: int | None = None) -> list[AdoptionEvent]:
        """加载本地未 flush 事件（按时间升序）。"""
        if not self.events_log_path.is_file():
            return []
        events: list[AdoptionEvent] = []
        max_count = limit or self.max_cache_size
        with self.events_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(AdoptionEvent.from_dict(data))
                except (json.JSONDecodeError, ValueError):
                    continue
                if len(events) >= max_count:
                    break
        return events

    def pending_count(self) -> int:
        """待 flush 事件数。"""
        if not self.events_log_path.is_file():
            return 0
        count = 0
        with self.events_log_path.open("r", encoding="utf-8") as f:
            for _ in f:
                count += 1
        return count

    # ------------------------------------------------------------------
    # flush
    # ------------------------------------------------------------------

    def flush(
        self,
        *,
        online: bool | None = None,
        max_batches: int = 10,
    ) -> FlushResult:
        """联网时批量 flush 事件到服务端。

        流程：
        1. 检测网络（online=None 时探测）
        2. 加载本地事件（最多 flush_batch_size * max_batches 条）
        3. 分批 POST /v1/metrics
        4. 服务端 ack 后从本地缓存删除已确认事件
        5. 失败的事件保留，下次重试

        返回 FlushResult（含 flushed / retained / error）。
        """
        if online is None:
            online = self._is_online()
        if not online:
            return FlushResult(
                retained=self.pending_count(),
                error="offline mode",
            )

        events = self.load_pending_events(limit=self.flush_batch_size * max_batches)
        if not events:
            return FlushResult()

        total_flushed = 0
        total_rejected = 0
        last_error: str | None = None
        last_ack: MetricsBatchAck | None = None
        acked_event_ids: set[str] = set()

        for batch_start in range(0, len(events), self.flush_batch_size):
            batch = events[batch_start : batch_start + self.flush_batch_size]
            batch_payload = [e.to_dict() for e in batch]
            ack = self._post_metrics_batch(batch_payload)
            last_ack = ack
            if ack.error:
                last_error = ack.error
                # 整批失败，停止后续 batch（避免雪崩）
                break
            total_flushed += ack.accepted
            total_rejected += ack.rejected
            # 服务端 ack 的事件从本地删除
            for e in batch:
                acked_event_ids.add(e.event_id)

        # 重写本地缓存（删除已 ack 的事件）
        if acked_event_ids:
            self._rewrite_events_log(exclude_ids=acked_event_ids)

        retained = self.pending_count()
        return FlushResult(
            flushed=total_flushed,
            rejected=total_rejected,
            retained=retained,
            error=last_error,
            ack=last_ack,
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _is_online(self) -> bool:
        """检查网络可达性（复用 RecallClient 的轻量探测）。"""
        if not self.config.server_url:
            return False
        try:
            client = self._get_http_client()
            resp = client.get(
                f"{self.config.server_url}/v1/sync/status",
                timeout=self.config.request_timeout_seconds,
            )
            return resp.status_code < 400
        except Exception:  # noqa: BLE001
            return False

    def _post_metrics_batch(self, events: list[dict[str, Any]]) -> MetricsBatchAck:
        """POST /v1/metrics 批量上报。Agent 9 未就绪时降级到 mock。"""
        if not self.config.server_url:
            return mock_metrics_batch(events)
        try:
            client = self._get_http_client()
            resp = client.post(
                f"{self.config.server_url}/v1/metrics",
                json={"events": events, "agent_id": self.config.agent_id},
                headers=self._auth_headers(),
                timeout=self.config.request_timeout_seconds,
            )
            if resp.status_code >= 400:
                return MetricsBatchAck(
                    accepted=0,
                    rejected=len(events),
                    error=f"HTTP {resp.status_code}: {resp.text[:200]}",
                )
            data = resp.json()
            return MetricsBatchAck(
                accepted=int(data.get("accepted", 0)),
                rejected=int(data.get("rejected", 0)),
                error=data.get("error"),
            )
        except (httpx.HTTPError, OSError, ValueError) as exc:
            # Agent 9 未就绪 / 网络错误 → 标记 rejected 并保留本地缓存重试
            return MetricsBatchAck(
                accepted=0,
                rejected=len(events),
                error=str(exc),
            )

    def _rewrite_events_log(self, *, exclude_ids: set[str]) -> None:
        """重写事件日志，排除已 ack 的事件。"""
        if not self.events_log_path.is_file():
            return
        # 读取全部事件
        all_events: list[dict[str, Any]] = []
        with self.events_log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if data.get("event_id") in exclude_ids:
                        continue
                    all_events.append(data)
                except json.JSONDecodeError:
                    continue
        # 原子写：先写临时文件再替换
        tmp_path = self.events_log_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for data in all_events:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        # Windows: os.replace 支持原子替换
        os.replace(tmp_path, self.events_log_path)

    def _get_http_client(self) -> httpx.BaseClient:
        if self._http_client is None:
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "ADOPTION_CACHE_DIRNAME",
    "ADOPTION_CACHE_FILENAME",
    "ADOPTION_EVENTS_LOG_FILENAME",
    "ADOPTION_STATE_FILENAME",
    "DEFAULT_FLUSH_BATCH_SIZE",
    "DEFAULT_MAX_CACHE_SIZE",
    "EVENT_TYPES",
    "AdoptionEvent",
    "AdoptionReporter",
    "FlushResult",
]
