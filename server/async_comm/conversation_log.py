"""ConversationLog：peer 间交流事件的 append-only JSONL 日志。

对应 Task 15：作为 async_comm 模块的事件存储底座，记录 peer 间所有交流事件
（ask / realtime_answer / simulated_answer / confirmed / revised / needs_human_review）。

设计原则：
- **append-only**：只追加不修改（参考 server/client/adoption.py 的 events.jsonl 模式）
- **幂等去重**：相同 event_id 不重复写入
- **回复链**：通过 in_reply_to 字段串联问答，load_thread 双向遍历
- **按 peer / type 过滤**：支持多种查询维度

文件格式：每行一个 JSON 对象（JSONL），vector_clock 字段以 dict 形式存储。
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

from server.async_comm.constants import (
    CONV_STATE_ACTIVE,
    CONV_STATE_PAUSED,
    CONV_STATE_RESUMED,
    CONV_STATE_TIMEOUT_DISCONNECT,
    EVENT_ASK,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_REVISED,
    EVENT_SIMULATED_ANSWER,
)
from server.async_comm.types import ConversationEvent, VectorClock

# 支持的事件类型集合（用于校验，非强制约束）
SUPPORTED_EVENT_TYPES: frozenset[str] = frozenset(
    {
        EVENT_ASK,
        EVENT_REALTIME_ANSWER,
        EVENT_SIMULATED_ANSWER,
        EVENT_CONFIRMED,
        EVENT_REVISED,
        EVENT_NEEDS_HUMAN_REVIEW,
    }
)


# ---------------------------------------------------------------------------
# 序列化辅助函数
# ---------------------------------------------------------------------------


def event_to_dict(event: ConversationEvent) -> dict:
    """将 ConversationEvent 序列化为可写入 JSONL 的 dict。

    vector_clock 字段转为 dict（通过 VectorClock.to_dict()）。
    """
    return {
        "event_id": event.event_id,
        "event_type": event.event_type,
        "peer_id": event.peer_id,
        "timestamp": event.timestamp,
        "vector_clock": event.vector_clock.to_dict(),
        "payload": event.payload,
        "in_reply_to": event.in_reply_to,
        "degraded": event.degraded,
        "realtime": event.realtime,
        "based_on": event.based_on,
        "snapshot_stale": event.snapshot_stale,
        "conversation_state": event.conversation_state,
    }


def event_from_dict(data: dict) -> ConversationEvent:
    """从 dict 反序列化为 ConversationEvent。

    vector_clock 字段从 dict 还原为 VectorClock（通过 VectorClock.from_dict()）。
    缺失字段使用 ConversationEvent 的默认值（向后兼容旧数据）。
    """
    return ConversationEvent(
        event_id=data["event_id"],
        event_type=data["event_type"],
        peer_id=data["peer_id"],
        timestamp=data["timestamp"],
        vector_clock=VectorClock.from_dict(data.get("vector_clock") or {}),
        payload=dict(data.get("payload") or {}),
        in_reply_to=data.get("in_reply_to", ""),
        degraded=bool(data.get("degraded", False)),
        realtime=bool(data.get("realtime", False)),
        based_on=data.get("based_on", ""),
        snapshot_stale=bool(data.get("snapshot_stale", False)),
        conversation_state=data.get("conversation_state", CONV_STATE_ACTIVE),
    )


# ---------------------------------------------------------------------------
# ConversationLog
# ---------------------------------------------------------------------------


class ConversationLog:
    """peer 间交流事件的 append-only JSONL 日志。

    使用：
        log = ConversationLog(Path(".teamharness/async_comm/conversation.jsonl"))
        log.append(ConversationEvent(event_id=..., event_type="ask", peer_id="bob", ...))
        events = log.load_by_peer("bob")
    """

    def __init__(self, log_path: Path) -> None:
        """初始化 ConversationLog。

        Args:
            log_path: JSONL 日志文件路径（如 .teamharness/async_comm/conversation.jsonl）。
                      父目录自动创建。对话状态文件（conversation_state.json）与日志同目录。
        """
        self.log_path = log_path
        # 父目录自动创建
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        # Task 27：对话状态文件（按 peer_id 索引，独立于 append-only 日志）
        self._state_file = self.log_path.parent / "conversation_state.json"
        # 线程安全锁（daemon 线程与 PeerComm 调用线程并发访问）
        self._state_lock = threading.Lock()
        self._state_cache: dict[str, dict] = self._load_state_file()

    def append(self, event: ConversationEvent) -> str:
        """追加事件到日志（append-only）。

        幂等：相同 event_id 不重复写入（append 前先检查 exists）。

        Args:
            event: 要追加的 ConversationEvent。

        Returns:
            event_id（即使重复写入也返回该 event_id）。
        """
        # 幂等去重：已存在则不重复写入
        if self.exists(event.event_id):
            return event.event_id
        # append-only：用 "a" 模式打开，绝不修改已有行
        with self.log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event_to_dict(event), ensure_ascii=False) + "\n")
        return event.event_id

    def load_all(self, *, limit: int | None = None) -> list[ConversationEvent]:
        """加载所有事件（按时间升序）。

        Args:
            limit: 限制返回数量（取时间最早的 limit 条）。None 表示不限制。

        Returns:
            按时间升序排列的事件列表。空日志返回空列表。
        """
        events = self._read_all()
        # 按时间升序排序
        events.sort(key=lambda e: e.timestamp)
        if limit is not None:
            events = events[:limit]
        return events

    def load_by_peer(self, peer_id: str, *, limit: int | None = None) -> list[ConversationEvent]:
        """加载与指定 peer 的所有事件（按时间升序）。

        Args:
            peer_id: 对方 peer_id。
            limit: 限制返回数量。

        Returns:
            按时间升序排列的事件列表。
        """
        events = self._read_all()
        filtered = [e for e in events if e.peer_id == peer_id]
        filtered.sort(key=lambda e: e.timestamp)
        if limit is not None:
            filtered = filtered[:limit]
        return filtered

    def load_by_type(self, event_type: str, *, limit: int | None = None) -> list[ConversationEvent]:
        """加载指定类型的所有事件。

        Args:
            event_type: 事件类型（ask / realtime_answer / ...）。
            limit: 限制返回数量。

        Returns:
            按时间升序排列的事件列表。
        """
        events = self._read_all()
        filtered = [e for e in events if e.event_type == event_type]
        filtered.sort(key=lambda e: e.timestamp)
        if limit is not None:
            filtered = filtered[:limit]
        return filtered

    def load_thread(self, root_event_id: str) -> list[ConversationEvent]:
        """加载某个事件的完整回复链。

        从 root_event_id 开始，沿 in_reply_to 链向前（找前驱）和向后（找所有回复本事件的事件）
        查找所有相关事件。

        Args:
            root_event_id: 起始事件 ID。

        Returns:
            按时间排序的事件列表。若 root_event_id 不存在，返回空列表。
        """
        all_events = self._read_all()
        by_id: dict[str, ConversationEvent] = {e.event_id: e for e in all_events}

        if root_event_id not in by_id:
            return []

        related: set[str] = set()
        related.add(root_event_id)

        # 向前：沿 in_reply_to 链查找前驱（root 回复了谁，递归向上）
        current = by_id.get(root_event_id)
        while current is not None and current.in_reply_to:
            parent_id = current.in_reply_to
            if parent_id in by_id and parent_id not in related:
                related.add(parent_id)
                current = by_id[parent_id]
            else:
                break

        # 向后：查找所有 in_reply_to == 当前 event_id 的事件（递归向下）
        to_process: list[str] = [root_event_id]
        while to_process:
            current_id = to_process.pop()
            for e in all_events:
                if e.in_reply_to == current_id and e.event_id not in related:
                    related.add(e.event_id)
                    to_process.append(e.event_id)

        # 按时间排序
        thread_events = [by_id[eid] for eid in related]
        thread_events.sort(key=lambda e: e.timestamp)
        return thread_events

    def count(
        self,
        *,
        peer_id: str | None = None,
        event_type: str | None = None,
    ) -> int:
        """统计事件数，可按 peer 和/或类型过滤。

        Args:
            peer_id: 按 peer 过滤（None 表示不过滤）。
            event_type: 按类型过滤（None 表示不过滤）。

        Returns:
            符合条件的事件数。
        """
        events = self._read_all()
        if peer_id is not None:
            events = [e for e in events if e.peer_id == peer_id]
        if event_type is not None:
            events = [e for e in events if e.event_type == event_type]
        return len(events)

    def get_event(self, event_id: str) -> ConversationEvent | None:
        """按 event_id 查询单条事件。

        Args:
            event_id: 事件 ID。

        Returns:
            匹配的 ConversationEvent，不存在返回 None。
        """
        events = self._read_all()
        for e in events:
            if e.event_id == event_id:
                return e
        return None

    def exists(self, event_id: str) -> bool:
        """判断 event_id 是否已存在（幂等检查）。

        Args:
            event_id: 事件 ID。

        Returns:
            已存在返回 True，否则 False。
        """
        if not self.log_path.is_file():
            return False
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if data.get("event_id") == event_id:
                    return True
        return False

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _read_all(self) -> list[ConversationEvent]:
        """读取日志中所有事件（不排序，按文件顺序）。

        文件不存在或为空时返回空列表。解析失败的行跳过（容错）。
        """
        if not self.log_path.is_file():
            return []
        events: list[ConversationEvent] = []
        with self.log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    events.append(event_from_dict(data))
                except (json.JSONDecodeError, KeyError, ValueError):
                    # 容错：跳过无法解析的行（不破坏整个日志）
                    continue
        return events

    # ------------------------------------------------------------------
    # Task 27：对话状态管理（独立于 append-only 日志的可覆盖状态文件）
    # ------------------------------------------------------------------

    def set_conversation_state(
        self,
        peer_id: str,
        state: str,
        *,
        last_event_id: str = "",
        reason: str = "",
    ) -> None:
        """设置对话状态（幂等：相同状态无副作用）。

        状态写入 conversation_state.json（原子写：.tmp + os.replace）。
        线程安全：通过 _state_lock 保护并发读写。

        Args:
            peer_id: 对方 peer_id。
            state: 对话状态（active / paused / timeout_disconnect / resumed）。
            last_event_id: 关联的最后事件 ID（用于 resume_conversation 重建链）。
            reason: 状态变更原因（如 "peer_offline" / "session_timeout"）。
        """
        with self._state_lock:
            current = self._state_cache.get(peer_id)
            # 幂等：相同状态且 last_event_id 一致 → 无操作
            if (
                current is not None
                and current.get("state") == state
                and current.get("last_event_id", "") == last_event_id
            ):
                return
            entry = {
                "peer_id": peer_id,
                "state": state,
                "last_event_id": last_event_id,
                "reason": reason,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            self._state_cache[peer_id] = entry
            self._save_state_file()

    def get_conversation_state(self, peer_id: str) -> dict | None:
        """查询对话状态。

        Args:
            peer_id: 对方 peer_id。

        Returns:
            状态 dict（含 peer_id / state / last_event_id / reason / updated_at），
            无记录返回 None。
        """
        with self._state_lock:
            entry = self._state_cache.get(peer_id)
            return dict(entry) if entry is not None else None

    def list_paused_conversations(self) -> list[dict]:
        """列出所有待恢复的对话（paused + timeout_disconnect）。

        Returns:
            状态 dict 列表，按 updated_at 升序。
        """
        with self._state_lock:
            paused = [
                dict(e)
                for e in self._state_cache.values()
                if e.get("state") in (CONV_STATE_PAUSED, CONV_STATE_TIMEOUT_DISCONNECT)
            ]
        paused.sort(key=lambda e: e.get("updated_at", ""))
        return paused

    def list_active_conversations(self) -> list[dict]:
        """列出所有活跃状态的对话（state == active）。

        供 daemon 超时检测使用。

        Returns:
            状态 dict 列表。
        """
        with self._state_lock:
            active = [
                dict(e)
                for e in self._state_cache.values()
                if e.get("state") == CONV_STATE_ACTIVE
            ]
        return active

    def clear_conversation_state(self, peer_id: str) -> None:
        """清除对话状态（resumed 后或对话结束时调用）。

        Args:
            peer_id: 对方 peer_id。
        """
        with self._state_lock:
            if peer_id in self._state_cache:
                del self._state_cache[peer_id]
                self._save_state_file()

    def resume_conversation(self, peer_id: str) -> list[ConversationEvent]:
        """恢复对话：基于 in_reply_to 链重建上下文。

        流程：
        1. 读取 conversation_state.json 中该 peer 的状态
        2. 若为 paused / timeout_disconnect：
           - 用 load_thread 重建回复链上下文（基于 last_event_id）
           - 标记为 resumed
           - 返回历史事件列表
        3. 若无状态或已 active/resumed → 返回空列表（无需恢复）

        Args:
            peer_id: 对方 peer_id。

        Returns:
            重建的对话上下文事件列表（按时间排序），无需恢复时返回空列表。
        """
        with self._state_lock:
            entry = self._state_cache.get(peer_id)

        if entry is None:
            return []

        state = entry.get("state", "")
        if state not in (CONV_STATE_PAUSED, CONV_STATE_TIMEOUT_DISCONNECT):
            return []

        last_event_id = entry.get("last_event_id", "")
        if not last_event_id:
            # 无 last_event_id → 退化到加载该 peer 全部事件
            events = self.load_by_peer(peer_id)
        else:
            # 基于 last_event_id 重建回复链上下文
            events = self.load_thread(last_event_id)
            # 若 load_thread 返回空（事件可能已清理），退化到按 peer 加载
            if not events:
                events = self.load_by_peer(peer_id)

        # 标记为 resumed
        self.set_conversation_state(
            peer_id,
            CONV_STATE_RESUMED,
            last_event_id=last_event_id,
            reason=f"resumed_from_{state}",
        )
        return events

    # ------------------------------------------------------------------
    # Task 27 内部：状态文件读写
    # ------------------------------------------------------------------

    def _load_state_file(self) -> dict[str, dict]:
        """加载 conversation_state.json。

        文件不存在或解析失败时返回空 dict（容错）。
        """
        if not self._state_file.is_file():
            return {}
        try:
            with self._state_file.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        # 校验每个 entry 为 dict
        return {str(k): v for k, v in data.items() if isinstance(v, dict)}

    def _save_state_file(self) -> None:
        """持久化 conversation_state.json（原子写：.tmp + os.replace）。

        调用方需已持有 _state_lock。
        """
        tmp_path = self._state_file.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(self._state_cache, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, self._state_file)


__all__ = [
    "SUPPORTED_EVENT_TYPES",
    "ConversationLog",
    "event_from_dict",
    "event_to_dict",
]
