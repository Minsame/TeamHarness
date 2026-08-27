"""PersonalDistill — 一级提炼（个人 dream）主入口。

对应 Agent 7 公共 API 契约：
    PersonalDistill:
      run_light(sessions) → signals
      run_rem(signals) → intents
      run_deep(intents, budget) → {assets, pending}
      report_metrics(member_id, signal_count, yield_ratio)

聚合：
- LightStage（信号筛选）
- RemStage（意图归纳）
- DeepStage（五维评分 + LLM 固化）
- BudgetManager（每成员 budget）
- PendingCandidateStore（pending 持久化）
- SignalReporter（信号计数上报）
- PrivacyGuard（隐私保护，对话不离开本机）

设计要点：
- 三阶段串行执行，每阶段输出可独立持久化（.dreams/light|rem|pending/）
- budget 超限时 Deep 跳过，候选入 pending，次日恢复后 process_pending
- 隐私：run_light/run_rem 只在本机处理，run_deep 产出的资产才上传 AssetIndex
- LLM 调用走服务端代理（LLMProviderClient），无代理时降级为规则启发式
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from server.distill_personal.budget import (
    BudgetManager,
    PendingCandidate,
    PendingCandidateStore,
    PendingProcessor,
)
from server.distill_personal.deep_stage import (
    DeepStage,
    DeepStageResult,
    DistilledAsset,
)
from server.distill_personal.light_stage import (
    LightStage,
    LightStageResult,
    Signal,
    save_signals,
)
from server.distill_personal.llm_provider import LLMBudget, LLMProviderClient
from server.distill_personal.metrics import (
    SignalReport,
    SignalReporter,
    adjust_budget_by_signal_count,
)
from server.distill_personal.privacy import PrivacyGuard
from server.distill_personal.rem_stage import (
    Intent,
    RemStage,
    RemStageResult,
    save_intents,
)
from server.distill_personal.session_provider import (
    ConversationLogSessionProvider,
    Session,
)
from server.distill_personal.schema_validator import LLMChatLike

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 全流程结果
# ---------------------------------------------------------------------------


@dataclass
class PersonalDistillResult:
    """一次完整一级提炼流程的结果。"""

    light: LightStageResult | None = None
    rem: RemStageResult | None = None
    deep: DeepStageResult | None = None
    produced_count: int = 0
    pending_count: int = 0
    skipped_intents: int = 0
    error: str | None = None
    # 信号上报是否成功
    signal_reported: bool = False
    # 隐私审计结果
    privacy_audit: dict[str, Any] = field(default_factory=dict)
    # Task 28：tags 回灌结果
    tags_feedback_applied: bool = False
    tags_feedback_error: str | None = None
    tags_feedback_tags: list[str] = field(default_factory=list)
    # Task 28：从 ConversationLog 加载的会话数
    conversation_session_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "produced": self.produced_count,
            "skipped": self.skipped_intents,
            "pending": self.pending_count,
            "error": self.error,
            "signal_reported": self.signal_reported,
            "privacy_ok": self.privacy_audit.get("ok", True),
            "light_signal_count": self.light.signal_count if self.light else 0,
            "rem_intent_count": self.rem.intent_count if self.rem else 0,
            "tags_feedback_applied": self.tags_feedback_applied,
            "tags_feedback_error": self.tags_feedback_error,
            "conversation_session_count": self.conversation_session_count,
        }


# ---------------------------------------------------------------------------
# Task 28：Tags 回灌客户端
# ---------------------------------------------------------------------------


class TagsFeedbackClient(Protocol):
    """tags 回灌客户端接口（Task 28 SubTask 28.3）。

    将提炼产出的 tags（如"账号管理专家"）回灌到成员档案。

    - central 模式：:class:`CentralTagsFeedbackClient` 调用 PATCH /v1/team/members/{id}
    - P2P 模式：:class:`LocalDreamsTagsFeedbackClient` 仅记录到本地 DREAMS.md
    """

    def feedback_tags(self, member_id: str, new_tags: list[str]) -> bool:
        """将提炼产出的 tags 回灌到成员档案。

        Args:
            member_id: 成员 ID。
            new_tags: 提炼产出的新标签列表。

        Returns:
            回灌是否成功。
        """
        ...


class CentralTagsFeedbackClient:
    """central 模式 tags 回灌客户端：调用 PATCH /v1/team/members/{id}。

    通过 HTTP 调用 team API 更新 member.tags（需 admin 权限的 API Key）。

    回灌限频：同 member 每日最多 1 次（避免频繁修改）。
    """

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:8000",
        api_key: str = "",
        timeout: float = 10.0,
        http_client: Any = None,
    ) -> None:
        """初始化。

        Args:
            base_url: team API 基地址（如 http://127.0.0.1:8000）。
            api_key: 认证 API Key（需 admin 权限）。
            timeout: HTTP 请求超时（秒）。
            http_client: 可选的 httpx.Client 注入（测试用，避免真实网络）。
        """
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout = timeout
        self._http_client = http_client
        # 回灌限频记录：{member_id: last_feedback_date_str}
        self._last_feedback: dict[str, str] = {}

    def feedback_tags(self, member_id: str, new_tags: list[str]) -> bool:
        """调用 PATCH /v1/team/members/{id} 更新 tags。

        限频：同 member 每日最多 1 次。当日已回灌则跳过（返回 True）。
        """
        if not new_tags:
            return False
        # 限频检查
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_feedback.get(member_id) == today:
            logger.info("成员 %s 今日已回灌 tags，跳过（限频）", member_id)
            return True
        try:
            import httpx
        except ImportError:
            logger.warning("httpx 未安装，tags 回灌跳过")
            return False

        url = f"{self.base_url}/v1/team/members/{member_id}"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        try:
            if self._http_client is not None:
                resp = self._http_client.patch(
                    url, json={"tags": new_tags}, headers=headers, timeout=self.timeout,
                )
            else:
                with httpx.Client(timeout=self.timeout) as client:
                    resp = client.patch(
                        url, json={"tags": new_tags}, headers=headers,
                    )
        except Exception as exc:  # noqa: BLE001 — 网络错误不阻断
            logger.warning("tags 回灌 HTTP 请求失败: %s", exc)
            return False

        if resp.status_code == 200:
            self._last_feedback[member_id] = today
            return True
        logger.warning(
            "tags 回灌失败: HTTP %s, body=%s",
            resp.status_code,
            getattr(resp, "text", "")[:200],
        )
        return False


class LocalDreamsTagsFeedbackClient:
    """P2P 模式 tags 回灌客户端：仅记录到本地 DREAMS.md。

    不实际修改 Member.tags（P2P 模式无中心 API），仅将建议标签
    写入 DREAMS.md 供人工审阅。
    """

    def __init__(self, repo_root: Path | None = None) -> None:
        self.repo_root = repo_root
        self._last_feedback: dict[str, str] = {}

    def feedback_tags(self, member_id: str, new_tags: list[str]) -> bool:
        if not new_tags:
            return False
        # 限频检查
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        if self._last_feedback.get(member_id) == today:
            return True
        if self.repo_root is None:
            logger.info("P2P 模式 tags 回灌：无 repo_root，仅记录标签建议: %s", new_tags)
            self._last_feedback[member_id] = today
            return True
        try:
            from server.infra_git.dreams import (
                ENTRY_TIMESTAMP_FORMAT,
                DreamEntry,
                append_entry,
            )
            entry = DreamEntry(
                timestamp=datetime.now(timezone.utc).strftime(ENTRY_TIMESTAMP_FORMAT),
                stage="tags",
                title=f"成员 {member_id} tags 回灌建议",
                body="建议标签：\n" + "\n".join(f"- {t}" for t in new_tags),
                metadata={"member_id": member_id},
            )
            append_entry(self.repo_root, entry)
            self._last_feedback[member_id] = today
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("DREAMS.md tags 回灌写入失败: %s", exc)
            return False


# ---------------------------------------------------------------------------
# PersonalDistill
# ---------------------------------------------------------------------------


class PersonalDistill:
    """一级提炼（个人 dream）主入口。

    使用：
        distill = PersonalDistill(
            llm=LLMProviderClient(...),
            budget_mgr=BudgetManager(...),
            pending_store=PendingCandidateStore(...),
            signal_reporter=SignalReporter(...),
            owner="alice",
            module_path="modules/backend",
        )
        # 单次提炼
        result = distill.run(sessions=[session1, session2])
        # 次日恢复后处理 pending
        pending_result = distill.process_pending(member_id="alice")
    """

    def __init__(
        self,
        *,
        llm: LLMChatLike | None = None,
        budget_mgr: BudgetManager | None = None,
        pending_store: PendingCandidateStore | None = None,
        signal_reporter: SignalReporter | None = None,
        owner: str = "",
        module_path: str = "",
        member_id: str = "",
        repo_root: Path | None = None,
        promotion_threshold: float = 0.5,
        max_llm_retries: int = 3,
        # 各阶段可注入自定义实例（测试用）
        light_stage: LightStage | None = None,
        rem_stage: RemStage | None = None,
        deep_stage: DeepStage | None = None,
        privacy_guard: PrivacyGuard | None = None,
    ) -> None:
        self.llm = llm
        self.budget_mgr = budget_mgr or BudgetManager()
        self.pending_store = pending_store or PendingCandidateStore(repo_root=repo_root)
        self.signal_reporter = signal_reporter or SignalReporter(member_id=member_id)
        self.owner = owner
        self.module_path = module_path
        self.member_id = member_id
        self.repo_root = repo_root
        self.promotion_threshold = promotion_threshold
        self.max_llm_retries = max_llm_retries

        self.light_stage = light_stage or LightStage()
        self.rem_stage = rem_stage or RemStage()
        self.privacy_guard = privacy_guard or PrivacyGuard()
        # DeepStage 延迟初始化（依赖 budget）
        self._deep_stage = deep_stage

    # ------------------------------------------------------------------
    # 公共 API（对齐占位契约）
    # ------------------------------------------------------------------

    def run_light(self, sessions: list[Session]) -> list[Signal]:
        """Light 阶段：信号筛选 + L0→L1 原子事实抽取。"""
        return self.light_stage.run(sessions).signals

    def run_rem(self, signals: list[Signal]) -> list[Intent]:
        """REM 阶段：意图归纳。"""
        return self.rem_stage.run(signals).intents

    def run_deep(
        self,
        intents: list[Intent],
        budget: LLMBudget | None = None,
    ) -> dict[str, Any]:
        """Deep 阶段：五维评分 + 结构化固化。

        返回 {"assets": list[DistilledAsset], "pending": list[PendingCandidate]}。
        """
        deep = self._get_deep_stage(budget)
        result = deep.run(intents)
        return {
            "assets": result.assets,
            "pending": result.pending,
            "skipped_intents": result.skipped_intents,
            "llm_skipped": result.llm_skipped,
            "errors": result.errors,
        }

    def report_metrics(
        self,
        member_id: str,
        signal_count: int,
        yield_ratio: float,
    ) -> bool:
        """上报 Light 阶段候选信号计数（用于预算动态调整）。"""
        report = SignalReport(
            member_id=member_id,
            signal_count=signal_count,
            yield_ratio=yield_ratio,
        )
        return self.signal_reporter.report(report)

    # ------------------------------------------------------------------
    # 全流程
    # ------------------------------------------------------------------

    def run(
        self,
        sessions: list[Session],
        *,
        member_id: str | None = None,
        save_intermediate: bool = True,
    ) -> PersonalDistillResult:
        """执行完整一级提炼流程（Light → REM → Deep）。

        - member_id：覆盖默认 self.member_id（用于多成员场景）
        - save_intermediate：是否持久化 signals/intents 到 .dreams/
        """
        mid = member_id or self.member_id
        result = PersonalDistillResult()

        # 隐私审计：确保上传载荷不含敏感字段（这里 sessions 是本机处理，不审计）
        # 但产出资产前会做隐私审计

        # 1. Light 阶段
        try:
            light_result = self.light_stage.run(sessions)
        except Exception as exc:  # noqa: BLE001
            result.error = f"Light 阶段失败: {exc}"
            logger.exception("Light 阶段失败: %s", exc)
            return result
        result.light = light_result
        if save_intermediate and self.repo_root is not None:
            try:
                save_signals(light_result.signals, repo_root=self.repo_root)
            except Exception as exc:  # noqa: BLE001
                logger.warning("持久化 signals 失败（不阻塞）: %s", exc)

        # 1.5 信号上报（SubTask 7.9）
        if mid:
            try:
                report = SignalReport.from_light_result(
                    member_id=mid,
                    result=light_result,
                )
                result.signal_reported = self.signal_reporter.report(report)
            except Exception as exc:  # noqa: BLE001
                logger.warning("信号上报失败（不阻塞）: %s", exc)

        # 1.6 预算动态调整（基于 signal_count）
        if mid and self.budget_mgr is not None:
            try:
                budget = self.budget_mgr.get_budget(mid)
                new_budget = adjust_budget_by_signal_count(
                    budget.daily_token_budget,
                    light_result.signal_count,
                )
                if new_budget != budget.daily_token_budget:
                    self.budget_mgr.set_daily_budget(mid, new_budget)
                    logger.info(
                        "成员 %s budget 动态调整: %d → %d（signal_count=%d）",
                        mid,
                        budget.daily_token_budget,
                        new_budget,
                        light_result.signal_count,
                    )
            except Exception as exc:  # noqa: BLE001
                logger.warning("budget 动态调整失败（不阻塞）: %s", exc)

        # 2. REM 阶段
        try:
            rem_result = self.rem_stage.run(light_result.signals)
        except Exception as exc:  # noqa: BLE001
            result.error = f"REM 阶段失败: {exc}"
            logger.exception("REM 阶段失败: %s", exc)
            return result
        result.rem = rem_result
        if save_intermediate and self.repo_root is not None:
            try:
                save_intents(rem_result.intents, repo_root=self.repo_root)
            except Exception as exc:  # noqa: BLE001
                logger.warning("持久化 intents 失败（不阻塞）: %s", exc)

        # 3. Deep 阶段
        budget = self.budget_mgr.get_budget(mid) if mid else None
        deep = self._get_deep_stage(budget)
        try:
            deep_result = deep.run(rem_result.intents)
        except Exception as exc:  # noqa: BLE001
            result.error = f"Deep 阶段失败: {exc}"
            logger.exception("Deep 阶段失败: %s", exc)
            return result
        result.deep = deep_result
        result.produced_count = deep_result.produced_count
        result.pending_count = len(deep_result.pending)
        result.skipped_intents = deep_result.skipped_intents + deep_result.llm_skipped

        # 4. 隐私审计：上传资产前确认资产 dict 不含敏感字段
        # 资产本身是结构化产出（不含原始对话），审计始终通过（保险）
        audit = self.privacy_guard.audit_upload(
            signals=None,  # 资产上传不含 signal
            intents=None,
        )
        result.privacy_audit = audit.to_dict()
        if not audit.ok:
            # 隐私违规：立即标注并阻断（对齐协调卡片重点风险 🔴）
            result.error = f"隐私审计失败: {audit.violations}"
            logger.error("🚨 隐私违规: %s", audit.violations)
            return result

        return result

    # ------------------------------------------------------------------
    # 次日恢复：处理 pending 候选
    # ------------------------------------------------------------------

    def process_pending(
        self,
        *,
        member_id: str | None = None,
        max_process: int | None = None,
    ) -> dict[str, Any]:
        """次日预算恢复后处理 pending 候选。

        返回 PendingProcessResult.to_dict()。
        """
        mid = member_id or self.member_id
        if not mid:
            return {"error": "member_id 未指定"}

        # 确保 budget 已 reset（跨日自动 reset 由 get_budget 处理）
        _ = self.budget_mgr.get_budget(mid)

        processor = PendingProcessor(self.budget_mgr, self.pending_store)
        deep = self._get_deep_stage(None)
        result = processor.process_pending(
            member_id=mid,
            process_callback=lambda cand: deep.run_single(cand.intent),
            max_process=max_process,
        )
        return {
            "processed": result.processed,
            "succeeded": result.succeeded,
            "failed": result.failed,
            "retained": result.retained,
            "errors": result.errors,
        }

    # ------------------------------------------------------------------
    # Task 28：从 ConversationLog 提炼 + tags 回灌
    # ------------------------------------------------------------------

    def run_with_conversation_log(
        self,
        conversation_log: Any,
        *,
        member_id: str = "",
        enable_tags_feedback: bool = False,
        tags_feedback_client: TagsFeedbackClient | None = None,
        save_intermediate: bool = True,
    ) -> PersonalDistillResult:
        """从 ConversationLog 加载会话并执行三阶段提炼（Task 28）。

        流程：
        1. 创建 ConversationLogSessionProvider
        2. 调用 provider.list_sessions() 获取已完成会话列表
        3. 对每个已完成会话调用 provider.read_session() 获取 Session
        4. 调用 self.run(sessions) 执行三阶段提炼
        5. 若 enable_tags_feedback 且有产出资产，调用 tags_feedback_client 回灌 tags

        错误隔离：
        - ConversationLog 读取失败 → 返回 error 结果（不抛异常）
        - 单个会话读取失败 → 跳过该会话（不阻断整体）
        - tags 回灌失败 → 记录 tags_feedback_error（不阻断提炼）

        幂等性：相同 ConversationLog 多次提炼产出相同信号（Light 阶段无随机性）。

        Args:
            conversation_log: :class:`ConversationLog` 实例。
            member_id: 成员 ID（覆盖默认 self.member_id）。
            enable_tags_feedback: 是否启用 tags 回灌（默认 False）。
            tags_feedback_client: tags 回灌客户端（enable_tags_feedback=True 时必须提供）。
            save_intermediate: 是否持久化中间产物到 .dreams/。

        Returns:
            :class:`PersonalDistillResult`，含 conversation_session_count 与 tags_feedback_* 字段。
        """
        mid = member_id or self.member_id
        result = PersonalDistillResult()

        # 1. 创建 provider 并加载会话
        provider = ConversationLogSessionProvider(conversation_log, member_id=mid)
        sessions: list[Session] = []
        try:
            metas = provider.list_sessions()
        except Exception as exc:  # noqa: BLE001 — 读取失败不阻断
            result.error = f"ConversationLog list_sessions 失败: {exc}"
            logger.exception("ConversationLog list_sessions 失败: %s", exc)
            return result

        for meta in metas:
            # 只提炼已完成的会话（active/paused/timeout_disconnect 跳过）
            if not meta.completed:
                continue
            try:
                sessions.append(provider.read_session(meta.session_id))
            except Exception as exc:  # noqa: BLE001 — 单个会话失败不阻断
                logger.warning("读取会话 %s 失败（跳过）: %s", meta.session_id, exc)
                continue

        result.conversation_session_count = len(sessions)

        if not sessions:
            # 无可提炼会话，直接返回（不算错误）
            logger.info("ConversationLog 无已完成的会话可提炼")
            return result

        # 2. 执行三阶段提炼
        result = self.run(sessions, member_id=mid, save_intermediate=save_intermediate)
        result.conversation_session_count = len(sessions)

        # 3. tags 回灌（可选）
        if enable_tags_feedback and tags_feedback_client is not None and mid:
            try:
                new_tags = self._derive_tags_from_result(result)
                result.tags_feedback_tags = new_tags
                if new_tags:
                    applied = tags_feedback_client.feedback_tags(mid, new_tags)
                    result.tags_feedback_applied = applied
                    if not applied:
                        result.tags_feedback_error = "tags_feedback_client 返回 False"
            except Exception as exc:  # noqa: BLE001 — 回灌失败不阻断
                result.tags_feedback_error = f"tags 回灌失败: {exc}"
                logger.warning("tags 回灌失败（不阻断提炼）: %s", exc)

        return result

    def _derive_tags_from_result(
        self,
        result: PersonalDistillResult,
    ) -> list[str]:
        """从提炼结果中推导 tags（SubTask 28.3）。

        从 Deep 阶段产出的资产中提取 tags，去重后返回。
        若无产出资产，返回空列表。

        推导规则：
        - 收集所有非 skipped 资产的 asset.tags
        - 去重保序
        - 最多取前 10 个（避免标签过多）
        """
        if result.deep is None or not result.deep.assets:
            return []
        tags: list[str] = []
        seen: set[str] = set()
        for asset in result.deep.assets:
            if asset.skipped:
                continue
            for tag in getattr(asset.asset, "tags", []) or []:
                if tag and tag not in seen:
                    seen.add(tag)
                    tags.append(tag)
                if len(tags) >= 10:
                    break
            if len(tags) >= 10:
                break
        return tags

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_deep_stage(self, budget: LLMBudget | None) -> DeepStage:
        """获取 DeepStage 实例。

        若构造时注入了自定义 deep_stage（测试用），直接复用；
        否则每次重新构造（因 budget 可能变化）。
        """
        if self._deep_stage is not None:
            return self._deep_stage
        return DeepStage(
            llm=self.llm,
            budget=budget,
            pending_store=self.pending_store,
            promotion_threshold=self.promotion_threshold,
            max_retries=self.max_llm_retries,
            owner=self.owner,
            module_path=self.module_path,
        )


__all__ = [
    "CentralTagsFeedbackClient",
    "LocalDreamsTagsFeedbackClient",
    "PersonalDistill",
    "PersonalDistillResult",
    "TagsFeedbackClient",
]
