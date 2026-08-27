"""信箱模块：管理 peer 间异步消息的收发与状态流转。

对应 Task 13。

复用 adoption.py 的 JSONL + event_id 幂等模式：
- append-only JSONL 文件存储消息
- event_id（UUID）幂等去重
- 原子重写（.tmp + os.replace）排除已处理事件

双箱结构：
- inbox.jsonl：收到的消息（初始状态 delivered）
- outbox.jsonl：待发送/已发送的消息（初始状态 pending_delivery）

状态机（5 种状态）：
- pending_delivery：待投递（outbox 初始状态，peer 离线时消息停留于此）
- delivered：已投递（peer 收到但未确认）
- confirmed：已确认（终态，对账一致）
- revised：已修订（终态，有差异但可自动修订）
- needs_human_review：需人工介入（终态，冲突严重）

合法流转：
    pending_delivery → delivered / confirmed / revised / needs_human_review
    delivered        → confirmed / revised / needs_human_review
    confirmed / revised / needs_human_review → 终态，不可再流转
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from server.async_comm.constants import (
    STATUS_CONFIRMED,
    STATUS_DELIVERED,
    STATUS_NEEDS_HUMAN_REVIEW,
    STATUS_PENDING_DELIVERY,
    STATUS_REVISED,
)
from server.async_comm.types import ConversationEvent, VectorClock

# 合法状态流转表（终态不可再流转到其他状态）
_VALID_TRANSITIONS: dict[str, frozenset[str]] = {
    STATUS_PENDING_DELIVERY: frozenset(
        {
            STATUS_DELIVERED,
            STATUS_CONFIRMED,
            STATUS_REVISED,
            STATUS_NEEDS_HUMAN_REVIEW,
        }
    ),
    STATUS_DELIVERED: frozenset(
        {
            STATUS_CONFIRMED,
            STATUS_REVISED,
            STATUS_NEEDS_HUMAN_REVIEW,
        }
    ),
    STATUS_CONFIRMED: frozenset(),  # 终态
    STATUS_REVISED: frozenset(),  # 终态
    STATUS_NEEDS_HUMAN_REVIEW: frozenset(),  # 终态
}

# 全部合法状态
_ALL_STATUSES: frozenset[str] = frozenset(
    {
        STATUS_PENDING_DELIVERY,
        STATUS_DELIVERED,
        STATUS_CONFIRMED,
        STATUS_REVISED,
        STATUS_NEEDS_HUMAN_REVIEW,
    }
)


def _event_to_dict(event: ConversationEvent) -> dict[str, Any]:
    """将 ConversationEvent 序列化为 dict。"""
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "peer_id": event.peer_id,
        "timestamp": event.timestamp,
        "vector_clock": event.vector_clock.to_dict(),
        "payload": dict(event.payload),
        "in_reply_to": event.in_reply_to,
        "degraded": event.degraded,
        "realtime": event.realtime,
        "based_on": event.based_on,
        "snapshot_stale": event.snapshot_stale,
    }


def _event_from_dict(data: dict[str, Any]) -> ConversationEvent:
    """从 dict 反序列化 ConversationEvent。"""
    vc_data = data.get("vector_clock")
    return ConversationEvent(
        event_id=str(data.get("event_id", "")),
        event_type=str(data.get("event_type", "")),
        peer_id=str(data.get("peer_id", "")),
        timestamp=str(data.get("timestamp", "")),
        vector_clock=VectorClock.from_dict(vc_data if isinstance(vc_data, dict) else {}),
        payload=dict(data.get("payload") or {}),
        in_reply_to=str(data.get("in_reply_to", "")),
        degraded=bool(data.get("degraded", False)),
        realtime=bool(data.get("realtime", False)),
        based_on=str(data.get("based_on", "")),
        snapshot_stale=bool(data.get("snapshot_stale", False)),
    )


class Mailbox:
    """信箱：管理本地 peer 的 inbox（收件箱）+ outbox（发件箱）。

    使用：
        mb = Mailbox(Path(".teamharness/async_comm"), "alice")
        mb.append_outbox(event)                      # 写入 outbox，状态 pending_delivery
        mb.update_status(event.event_id, STATUS_DELIVERED)  # 更新状态
        mb.remove_delivered(event_ids={event.event_id})     # 清理已处理消息
    """

    def __init__(self, base_dir: Path, peer_id: str) -> None:
        """初始化信箱。

        base_dir: 信箱根目录（如 .teamharness/async_comm/）
        peer_id: 本地 peer_id（用于目录隔离）

        目录结构：
            {base_dir}/{peer_id}/inbox.jsonl
            {base_dir}/{peer_id}/outbox.jsonl
            {base_dir}/{peer_id}/state.json  # 状态索引（event_id -> status 映射）
        """
        self.peer_id = peer_id
        self.mailbox_dir = base_dir / peer_id
        self.mailbox_dir.mkdir(parents=True, exist_ok=True)
        self.inbox_path = self.mailbox_dir / "inbox.jsonl"
        self.outbox_path = self.mailbox_dir / "outbox.jsonl"
        self.state_path = self.mailbox_dir / "state.json"
        self._state: dict[str, str] = self._load_state()

    # ------------------------------------------------------------------
    # 追加消息
    # ------------------------------------------------------------------

    def append_outbox(self, event: ConversationEvent) -> str:
        """追加消息到 outbox（初始状态 pending_delivery）。

        返回 event_id。幂等：相同 event_id 不重复写入。
        """
        if event.event_id in self._state:
            return event.event_id
        self._append_to_file(self.outbox_path, event)
        self._state[event.event_id] = STATUS_PENDING_DELIVERY
        self._save_state()
        return event.event_id

    def append_inbox(self, event: ConversationEvent) -> str:
        """追加消息到 inbox（初始状态 delivered）。

        幂等：相同 event_id 不重复写入。
        """
        if event.event_id in self._state:
            return event.event_id
        self._append_to_file(self.inbox_path, event)
        self._state[event.event_id] = STATUS_DELIVERED
        self._save_state()
        return event.event_id

    # ------------------------------------------------------------------
    # 加载消息
    # ------------------------------------------------------------------

    def load_outbox(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[ConversationEvent]:
        """加载 outbox 消息，可按状态过滤。"""
        return self._load_events(self.outbox_path, status=status, limit=limit)

    def load_inbox(
        self, *, status: str | None = None, limit: int | None = None
    ) -> list[ConversationEvent]:
        """加载 inbox 消息，可按状态过滤。"""
        return self._load_events(self.inbox_path, status=status, limit=limit)

    # ------------------------------------------------------------------
    # 状态管理
    # ------------------------------------------------------------------

    def update_status(self, event_id: str, new_status: str) -> bool:
        """更新消息状态（写入 state.json）。返回是否成功。

        状态流转校验：
        - pending_delivery → delivered / confirmed / revised / needs_human_review
        - delivered → confirmed / revised / needs_human_review
        - confirmed / revised / needs_human_review → 终态，不可再流转

        幂等：相同状态更新合法（无操作返回 True）。
        非法流转抛 ValueError。
        """
        if new_status not in _ALL_STATUSES:
            raise ValueError(f"未知状态：{new_status}")
        if event_id not in self._state:
            return False
        current_status = self._state[event_id]
        if new_status == current_status:
            return True  # 幂等：相同状态，无操作
        allowed = _VALID_TRANSITIONS.get(current_status, frozenset())
        if new_status not in allowed:
            raise ValueError(f"非法状态流转：{current_status} → {new_status}")
        self._state[event_id] = new_status
        self._save_state()
        return True

    def get_status(self, event_id: str) -> str | None:
        """查询消息状态。"""
        return self._state.get(event_id)

    def pending_delivery_count(self) -> int:
        """outbox 中 pending_delivery 状态的消息数。"""
        if not self.outbox_path.is_file():
            return 0
        count = 0
        with self.outbox_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                event_id = data.get("event_id", "")
                if self._state.get(event_id) == STATUS_PENDING_DELIVERY:
                    count += 1
        return count

    # ------------------------------------------------------------------
    # 清理
    # ------------------------------------------------------------------

    def remove_delivered(self, *, event_ids: set[str]) -> int:
        """从 outbox 删除已 delivered/confirmed/revised 的消息（原子重写）。

        返回删除数。复用 adoption.py 的 _rewrite_events_log 模式：
        先写 .tmp 文件再 os.replace 原子替换。
        """
        if not self.outbox_path.is_file():
            return 0
        kept_events: list[dict[str, Any]] = []
        removed_count = 0
        with self.outbox_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("event_id") in event_ids:
                    removed_count += 1
                    continue
                kept_events.append(data)
        # 原子重写：先写 .tmp 再 os.replace
        tmp_path = self.outbox_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            for data in kept_events:
                f.write(json.dumps(data, ensure_ascii=False) + "\n")
        os.replace(tmp_path, self.outbox_path)
        return removed_count

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _append_to_file(self, path: Path, event: ConversationEvent) -> None:
        """追加事件到 JSONL 文件（append-only）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(_event_to_dict(event), ensure_ascii=False) + "\n")

    def _load_events(
        self, path: Path, *, status: str | None = None, limit: int | None = None
    ) -> list[ConversationEvent]:
        """从 JSONL 文件加载事件，可按状态过滤、限制数量。"""
        if not path.is_file():
            return []
        events: list[ConversationEvent] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    event = _event_from_dict(data)
                except (json.JSONDecodeError, ValueError):
                    continue
                if status is not None and self._state.get(event.event_id) != status:
                    continue
                if limit is not None and len(events) >= limit:
                    break
                events.append(event)
        return events

    def _load_state(self) -> dict[str, str]:
        """加载 state.json。"""
        if not self.state_path.is_file():
            return {}
        try:
            with self.state_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            return {str(k): str(v) for k, v in data.items()}
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_state(self) -> None:
        """持久化 state.json（原子写：先 .tmp 再 os.replace）。"""
        tmp_path = self.state_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._state, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self.state_path)


__all__ = ["Mailbox"]
