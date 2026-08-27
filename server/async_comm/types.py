"""async_comm 核心数据结构。

定义三个核心 DTO：
- VectorClock：版本向量，用于因果排序与冲突检测
- ConversationEvent：交流事件（ConversationLog 中的单条记录）
- PeerSnapshot：peer 的 harness 本地快照

对应 Task 11。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class VectorClock:
    """版本向量，用于因果排序与冲突检测。

    每个 peer 维护一个 counter，向量 = {peer_id: counter}。
    """

    counters: dict[str, int] = field(default_factory=dict)

    def increment(self, peer_id: str) -> None:
        """递增 peer_id 的 counter。"""
        self.counters[peer_id] = self.counters.get(peer_id, 0) + 1

    def merge(self, other: "VectorClock") -> "VectorClock":
        """合并两个向量（取各 peer 的最大值）。

        返回新的 VectorClock，不修改 self 与 other。
        """
        all_peers = set(self.counters) | set(other.counters)
        merged = {
            peer_id: max(self.counters.get(peer_id, 0), other.counters.get(peer_id, 0))
            for peer_id in all_peers
        }
        return VectorClock(counters=merged)

    def compare(self, other: "VectorClock") -> str:
        """比较因果关系。

        返回 "before" / "after" / "equal" / "concurrent"。
        - before: self 在 other 之前（self 是 other 的因果前驱）
        - after: self 在 other 之后
        - equal: 相同
        - concurrent: 并发（有冲突）
        """
        all_peers = set(self.counters) | set(other.counters)
        self_less = False
        self_greater = False
        for peer_id in all_peers:
            s = self.counters.get(peer_id, 0)
            o = other.counters.get(peer_id, 0)
            if s < o:
                self_less = True
            elif s > o:
                self_greater = True
        if not self_less and not self_greater:
            return "equal"
        if self_less and not self_greater:
            return "before"
        if self_greater and not self_less:
            return "after"
        return "concurrent"

    def to_dict(self) -> dict[str, int]:
        """序列化。"""
        return dict(self.counters)

    @classmethod
    def from_dict(cls, data: dict[str, int]) -> "VectorClock":
        """反序列化。"""
        return cls(counters=dict(data))


@dataclass
class ConversationEvent:
    """交流事件（ConversationLog 中的单条记录）。"""

    event_id: str  # UUID，幂等去重
    event_type: str  # ask / realtime_answer / simulated_answer / confirmed / revised / needs_human_review
    peer_id: str  # 对方 peer_id
    timestamp: str  # ISO 时间戳
    vector_clock: VectorClock = field(default_factory=VectorClock)
    payload: dict[str, Any] = field(default_factory=dict)
    in_reply_to: str = ""  # 回复链
    degraded: bool = False  # 影子联络标记
    realtime: bool = False  # 实时通信标记
    based_on: str = ""  # 基于的快照版本（如 "bob_v38"）
    snapshot_stale: bool = False  # 快照过期标记
    conversation_state: str = "active"  # Task 27：对话状态（active/paused/timeout_disconnect/resumed）


@dataclass
class PeerSnapshot:
    """peer 的 harness 本地快照。"""

    peer_id: str
    snapshot_version: str = ""  # 版本标识（如 "v38"）
    captured_at: str = ""  # 采集时间 ISO
    harness_path: Path = field(default_factory=Path)  # 快照目录
    manifest_path: Path = field(default_factory=Path)  # manifest 副本路径
    vector_clock_path: Path = field(default_factory=Path)  # vector_clock.json 路径
    vector_clock: VectorClock = field(default_factory=VectorClock)

    @property
    def is_stale(self) -> bool:
        """快照是否过期（基于 captured_at 和 ttl_days）。

        由 PeerSnapshotManager 判断，这里只存 captured_at。
        """
        return False


__all__ = [
    "ConversationEvent",
    "PeerSnapshot",
    "VectorClock",
]
