"""每成员 daily_token_budget 管理 + 超限降级 + pending 处理。

对应 SubTask 7.8：
- 每成员 daily_token_budget 配置生效
- 超预算时 Deep 跳过，候选写入 .dreams/pending/
- 次日预算恢复后 pending 候选被处理

设计要点：
- BudgetManager 维护内存中的成员 budget（服务端用，按 member_id 索引）
- budget 状态按 UTC 日期切换：跨日自动 reset
- pending 持久化到 .dreams/pending/ 下（每个候选一个 JSON 文件，含 intent + created_at）
- 次日恢复（budget.reset 后），调用 process_pending 把 pending 候选重新交给 Deep 阶段
- 隐私：pending 文件只含结构化 intent，不含原始对话（对齐 privacy.py）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from server.distill_personal.llm_provider import LLMBudget, default_budget

logger = logging.getLogger(__name__)


# pending 目录默认路径（相对 repo_root）
DEFAULT_DREAMS_DIR = Path(".teamharness-local") / "dreams"
DEFAULT_PENDING_DIRNAME = "pending"


@dataclass
class PendingCandidate:
    """pending 候选（Deep 阶段因预算不足跳过的 intent）。"""

    candidate_id: str
    intent: dict[str, Any]  # REM 阶段产出的 intent 结构
    created_at: str  # ISO 字符串
    reason: str = "budget_exhausted"  # 跳过原因
    # 关联的 session_id 列表（便于追溯）
    source_session_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "intent": dict(self.intent),
            "created_at": self.created_at,
            "reason": self.reason,
            "source_session_ids": list(self.source_session_ids),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PendingCandidate":
        return cls(
            candidate_id=str(data.get("candidate_id", "")),
            intent=dict(data.get("intent") or {}),
            created_at=str(data.get("created_at", "")),
            reason=str(data.get("reason", "budget_exhausted")),
            source_session_ids=list(data.get("source_session_ids") or []),
        )


class BudgetManager:
    """每成员 budget 管理器（服务端用）。

    使用：
        mgr = BudgetManager(default_daily_budget=100_000)
        mgr.ensure_member("alice")
        budget = mgr.get_budget("alice")
        if budget.exhausted:
            # 跳过 Deep，候选入 pending
            ...
        else:
            budget.consume(used_tokens)

    跨日自动 reset：
        get_budget 时检查 reset_at 是否已过，若已过则 reset
    """

    def __init__(
        self,
        *,
        default_daily_budget: int = 100_000,
    ) -> None:
        self.default_daily_budget = default_daily_budget
        self._budgets: dict[str, LLMBudget] = {}

    def ensure_member(self, member_id: str, *, daily_budget: int | None = None) -> LLMBudget:
        """确保成员 budget 存在（不存在则创建）。"""
        if member_id not in self._budgets:
            budget = default_budget(
                member_id,
                daily_token_budget=daily_budget or self.default_daily_budget,
            )
            self._budgets[member_id] = budget
        return self._budgets[member_id]

    def get_budget(self, member_id: str) -> LLMBudget:
        """获取成员 budget（自动跨日 reset）。"""
        budget = self.ensure_member(member_id)
        self._maybe_reset_if_crossed_day(budget)
        return budget

    def consume(self, member_id: str, tokens: int) -> int:
        """消费 tokens，返回实际消费量。"""
        budget = self.get_budget(member_id)
        return budget.consume(tokens)

    def set_daily_budget(self, member_id: str, daily_budget: int) -> None:
        """更新成员每日预算（治理用，可热更新）。"""
        budget = self.ensure_member(member_id)
        budget.daily_token_budget = max(0, daily_budget)

    def is_degraded(self, member_id: str) -> bool:
        """成员是否处于降级状态（budget 耗尽）。"""
        return self.get_budget(member_id).degraded

    def reset_member(self, member_id: str) -> None:
        """手动 reset 成员 budget（测试 / 治理用）。"""
        budget = self.ensure_member(member_id)
        budget.reset()

    def reset_all(self) -> None:
        """reset 全部成员（每日 cron 触发）。"""
        for budget in self._budgets.values():
            budget.reset()

    # ------------------------------------------------------------------
    # 跨日 reset
    # ------------------------------------------------------------------

    @staticmethod
    def _maybe_reset_if_crossed_day(budget: LLMBudget) -> None:
        """检查 reset_at 是否已过，若已过则 reset。"""
        if not budget.reset_at:
            return
        try:
            reset_dt = datetime.fromisoformat(budget.reset_at.replace("Z", "+00:00"))
        except ValueError:
            return
        now = datetime.now(timezone.utc)
        if now >= reset_dt:
            budget.reset()


# ---------------------------------------------------------------------------
# PendingCandidateStore — pending 候选持久化
# ---------------------------------------------------------------------------


class PendingCandidateStore:
    """pending 候选持久化存储。

    存储路径：.teamharness-local/dreams/pending/<candidate_id>.json
    每个候选一个 JSON 文件，便于按 id 索引与删除。

    使用：
        store = PendingCandidateStore(repo_root=Path(...))
        cid = store.save(candidate)
        ids = store.list_ids()
        candidate = store.load(cid)
        store.delete(cid)
    """

    def __init__(
        self,
        *,
        repo_root: Path | None = None,
        pending_dir: Path | None = None,
    ) -> None:
        if pending_dir is not None:
            self.pending_dir = pending_dir
        elif repo_root is not None:
            self.pending_dir = repo_root / DEFAULT_DREAMS_DIR / DEFAULT_PENDING_DIRNAME
        else:
            self.pending_dir = Path.cwd() / DEFAULT_DREAMS_DIR / DEFAULT_PENDING_DIRNAME

    def save(self, candidate: PendingCandidate) -> str:
        """保存候选，返回 candidate_id。父目录自动创建。"""
        self.pending_dir.mkdir(parents=True, exist_ok=True)
        path = self.pending_dir / f"{candidate.candidate_id}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(candidate.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(path)
        return candidate.candidate_id

    def load(self, candidate_id: str) -> PendingCandidate | None:
        path = self.pending_dir / f"{candidate_id}.json"
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("加载 pending 候选 %s 失败: %s", candidate_id, exc)
            return None
        return PendingCandidate.from_dict(data)

    def list_ids(self) -> list[str]:
        """列出全部 pending 候选 id（按 created_at 升序，便于先入先处理）。"""
        if not self.pending_dir.is_dir():
            return []
        items: list[tuple[str, str]] = []
        for p in self.pending_dir.glob("*.json"):
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
                created = str(data.get("created_at", ""))
                items.append((created, p.stem))
            except (OSError, json.JSONDecodeError):
                continue
        items.sort()
        return [cid for _, cid in items]

    def list_all(self) -> list[PendingCandidate]:
        """加载全部 pending 候选（按 created_at 升序）。"""
        result: list[PendingCandidate] = []
        for cid in self.list_ids():
            cand = self.load(cid)
            if cand is not None:
                result.append(cand)
        return result

    def delete(self, candidate_id: str) -> bool:
        path = self.pending_dir / f"{candidate_id}.json"
        if not path.is_file():
            return False
        path.unlink()
        return True

    def count(self) -> int:
        return len(self.list_ids())


# ---------------------------------------------------------------------------
# PendingProcessor — 次日预算恢复后处理 pending
# ---------------------------------------------------------------------------


ProcessPendingCallback = Callable[[PendingCandidate], dict[str, Any]]
"""处理 pending 候选的回调（通常为 Deep 阶段重新提炼）。

返回 dict（至少含 success: bool, asset_id: str | None, error: str | None）。
"""


@dataclass
class PendingProcessResult:
    """pending 批量处理结果。"""

    processed: int = 0
    succeeded: int = 0
    failed: int = 0
    retained: int = 0  # 仍保留在 pending 的数量（处理失败的）
    errors: list[str] = field(default_factory=list)


class PendingProcessor:
    """次日预算恢复后处理 pending 候选。

    使用：
        processor = PendingProcessor(budget_mgr, store)
        result = processor.process_pending(
            member_id="alice",
            process_callback=lambda cand: deep_stage.run_single(cand.intent),
        )
    """

    def __init__(
        self,
        budget_mgr: BudgetManager,
        store: PendingCandidateStore,
    ) -> None:
        self.budget_mgr = budget_mgr
        self.store = store

    def process_pending(
        self,
        *,
        member_id: str,
        process_callback: ProcessPendingCallback,
        max_process: int | None = None,
    ) -> PendingProcessResult:
        """处理 pending 候选，直到 budget 再次耗尽或全部处理完。

        - max_process：单次最多处理数量（None=不限，受 budget 约束）
        - 处理成功的候选从 pending 删除
        - 处理失败的候选保留（下次重试）
        - budget 再次耗尽时停止，剩余候选留待次日
        """
        result = PendingProcessResult()
        candidates = self.store.list_all()
        if max_process is not None:
            candidates = candidates[:max_process]

        for cand in candidates:
            # 检查 budget 是否仍可用
            budget = self.budget_mgr.get_budget(member_id)
            if budget.exhausted:
                logger.info(
                    "budget 再次耗尽，剩余 %d 个 pending 候选留待次日",
                    self.store.count() - result.processed,
                )
                break
            result.processed += 1
            try:
                cb_result = process_callback(cand)
            except Exception as exc:  # noqa: BLE001
                result.failed += 1
                result.errors.append(f"{cand.candidate_id}: {exc}")
                logger.warning("处理 pending 候选 %s 异常: %s", cand.candidate_id, exc)
                continue
            if cb_result.get("success"):
                result.succeeded += 1
                self.store.delete(cand.candidate_id)
                # 消费 tokens（若回调返回了 usage）
                usage = cb_result.get("usage") or {}
                tokens = int(usage.get("total_tokens", 0))
                if tokens > 0:
                    self.budget_mgr.consume(member_id, tokens)
            else:
                result.failed += 1
                err = cb_result.get("error") or "unknown error"
                result.errors.append(f"{cand.candidate_id}: {err}")
                # 失败的候选保留

        result.retained = self.store.count()
        return result


__all__ = [
    "BudgetManager",
    "DEFAULT_DREAMS_DIR",
    "DEFAULT_PENDING_DIRNAME",
    "PendingCandidate",
    "PendingCandidateStore",
    "PendingProcessor",
    "PendingProcessResult",
    "ProcessPendingCallback",
]
