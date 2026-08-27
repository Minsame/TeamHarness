"""Light 阶段候选信号计数上报（SubTask 7.9）。

对应：
- Light 阶段候选信号计数上报服务端（用于预算动态调整）
- 上报内容：member_id / signal_count / yield_ratio / type_counts
- 上报通道：复用 Agent 6 的 AdoptionReporter（写入本地 adoption-events.jsonl）
  或直接 POST /v1/metrics（若 Agent 9 已就绪）
- 服务端基于 signal_count 动态调整该成员的 daily_token_budget：
  - signal_count 高 → 适当提升 budget（更多候选进入 Deep）
  - signal_count 低 → 保持或降低 budget（避免浪费）

设计要点：
- 上报是尽力而为（best-effort），失败不阻塞提炼流程
- 上报事件类型用 "distill_signal"（与 adoption 的 recall/view 区分）
- 服务端动态调整逻辑见 BudgetManager.adjust_by_signal_count
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.distill_personal.light_stage import LightStageResult

logger = logging.getLogger(__name__)

# 信号上报事件类型
SIGNAL_REPORT_EVENT_TYPE = "distill_signal"


@dataclass
class SignalReport:
    """Light 阶段信号上报载荷。"""

    member_id: str
    signal_count: int
    yield_ratio: float
    type_counts: dict[str, int] = field(default_factory=dict)
    scanned_sessions: int = 0
    scanned_turns: int = 0
    skipped_turns: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "signal_count": self.signal_count,
            "yield_ratio": round(self.yield_ratio, 4),
            "type_counts": dict(self.type_counts),
            "scanned_sessions": self.scanned_sessions,
            "scanned_turns": self.scanned_turns,
            "skipped_turns": self.skipped_turns,
        }

    @classmethod
    def from_light_result(
        cls,
        *,
        member_id: str,
        result: LightStageResult,
    ) -> "SignalReport":
        """从 LightStageResult 构造上报载荷。"""
        return cls(
            member_id=member_id,
            signal_count=result.signal_count,
            yield_ratio=result.yield_ratio,
            type_counts=dict(result.type_counts),
            scanned_sessions=result.scanned_sessions,
            scanned_turns=result.scanned_turns,
            skipped_turns=result.skipped_turns,
        )


class SignalReporter:
    """Light 阶段信号上报器。

    使用：
        reporter = SignalReporter(adoption_reporter=...)
        reporter.report(report)  # 写入本地 adoption-events.jsonl
    """

    def __init__(
        self,
        *,
        adoption_reporter: Any | None = None,
        member_id: str = "",
    ) -> None:
        """初始化。

        adoption_reporter：复用 Agent 6 的 AdoptionReporter，把信号上报事件
        写入本地 adoption-events.jsonl，联网时由守护进程批量 flush 到 /v1/metrics。
        若为 None，则只记录日志（best-effort，不阻塞提炼）。
        """
        self.adoption_reporter = adoption_reporter
        self.member_id = member_id

    def report(self, report: SignalReport) -> bool:
        """上报信号计数。

        返回 True 表示已记录（写入本地缓存或联网上报），
        返回 False 表示失败（仅记录 warning）。
        """
        if self.adoption_reporter is None:
            logger.info(
                "Light 信号上报（无 adoption_reporter，仅日志）: %s",
                report.to_dict(),
            )
            return True
        try:
            # AdoptionEvent 校验 event_type 必须在 EVENT_TYPES 中，
            # distill_signal 是新事件类型 → 不实例化 AdoptionEvent，
            # 直接构造 JSON 载荷写入 adoption-events.jsonl（格式与 AdoptionEvent 一致）
            self._write_signal_event_directly(report)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Light 信号上报失败: %s", exc)
            return False

    def _write_signal_event_directly(self, report: SignalReport) -> None:
        """直接写信号事件到 adoption-events.jsonl（绕过 AdoptionEvent event_type 校验）。

        AdoptionReporter.record 限制了 event_type 必须在 EVENT_TYPES 中，
        但 distill_signal 是新事件类型，需要兼容写入。
        采用与 AdoptionEvent 相同的 JSON 结构，仅 event_type 不同。
        """
        import json
        import uuid
        from datetime import datetime, timezone

        reporter = self.adoption_reporter
        # 复用 AdoptionReporter 的 events_log_path
        cache_dir = reporter.cache_dir  # type: ignore[attr-defined]
        events_log_path = reporter.events_log_path  # type: ignore[attr-defined]
        cache_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "event_id": str(uuid.uuid4()),
            "event_type": SIGNAL_REPORT_EVENT_TYPE,
            "asset_id": "",
            "agent_id": "",
            "member_id": report.member_id or self.member_id,
            "module_path": "",
            "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "metadata": report.to_dict(),
        }
        with events_log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# 服务端：基于 signal_count 动态调整 budget
# ---------------------------------------------------------------------------


# 动态调整阈值（对齐技术方案 8.6「成本控制」）
SIGNAL_COUNT_HIGH_THRESHOLD = 50  # signal_count >= 50 视为高产
SIGNAL_COUNT_LOW_THRESHOLD = 5    # signal_count < 5 视为低产
BUDGET_ADJUST_RATIO_HIGH = 1.2    # 高产时 budget 提升 20%
BUDGET_ADJUST_RATIO_LOW = 0.8     # 低产时 budget 降低 20%


def adjust_budget_by_signal_count(
    current_budget: int,
    signal_count: int,
    *,
    high_threshold: int = SIGNAL_COUNT_HIGH_THRESHOLD,
    low_threshold: int = SIGNAL_COUNT_LOW_THRESHOLD,
    high_ratio: float = BUDGET_ADJUST_RATIO_HIGH,
    low_ratio: float = BUDGET_ADJUST_RATIO_LOW,
) -> int:
    """基于 signal_count 动态调整 daily_token_budget。

    - signal_count >= high_threshold → current_budget * high_ratio
    - signal_count < low_threshold → current_budget * low_ratio
    - 其他 → 保持不变

    返回新的 budget 值（int）。
    """
    if signal_count >= high_threshold:
        return int(current_budget * high_ratio)
    if signal_count < low_threshold:
        return int(current_budget * low_ratio)
    return current_budget


__all__ = [
    "BUDGET_ADJUST_RATIO_HIGH",
    "BUDGET_ADJUST_RATIO_LOW",
    "SIGNAL_COUNT_HIGH_THRESHOLD",
    "SIGNAL_COUNT_LOW_THRESHOLD",
    "SIGNAL_REPORT_EVENT_TYPE",
    "SignalReport",
    "SignalReporter",
    "adjust_budget_by_signal_count",
]
