"""distill_personal 域 — 一级提炼（个人 dream）。

对应 Agent 7：
- SessionProvider 抽象（Trae 适配 + 通用 JSONL 兜底 + discover_sessions_root）
- 对话记录增量采集
- Light/REM/Deep 三阶段（信号筛选 → 意图归纳 → 五维评分 + frontmatter 资产）
- 四类资产子 Prompt 模板（rule/memory/skill/tool）
- LLM 服务端代理接入（POST /v1/llm/chat + GET /v1/llm/budget）
- 每成员 daily_token_budget + 超限降级（Deep 跳过，候选入 pending）
- Light 阶段候选信号计数上报
- LLM 强制 JSON schema + 校验失败重试
- cost estimate 命令
- 隐私保护（对话不离开本机）
"""

from __future__ import annotations

from server.distill_personal.budget import (
    BudgetManager,
    PendingCandidate,
    PendingCandidateStore,
    PendingProcessor,
)
from server.distill_personal.cost_estimate import CostEstimator
from server.distill_personal.deep_stage import DeepStage, DistilledAsset, FiveDimScore
from server.distill_personal.incremental_collector import IncrementalCollector
from server.distill_personal.light_stage import LightStage, Signal
from server.distill_personal.llm_provider import (
    LLMBudget,
    LLMChatResult,
    LLMProviderClient,
    LocalLLMProvider,
)
from server.distill_personal.metrics import SignalReporter, adjust_budget_by_signal_count
from server.distill_personal.personal_distill import PersonalDistill
from server.distill_personal.privacy import PrivacyGuard
from server.distill_personal.prompts import (
    ASSET_PROMPT_TEMPLATES,
    get_prompt_template,
    render_system_prompt,
)
from server.distill_personal.rem_stage import Intent, RemStage
from server.distill_personal.schema_validator import chat_with_schema, validate_against_schema
from server.distill_personal.session_provider import (
    GenericJsonlSessionProvider,
    MultiSessionProvider,
    Session,
    SessionMeta,
    SessionProvider,
    SessionTurn,
    TraeSessionProvider,
    create_session_provider,
)

__all__ = [
    "ASSET_PROMPT_TEMPLATES",
    "BudgetManager",
    "CostEstimator",
    "DeepStage",
    "DistilledAsset",
    "FiveDimScore",
    "GenericJsonlSessionProvider",
    "IncrementalCollector",
    "Intent",
    "LLMBudget",
    "LLMChatResult",
    "LLMProviderClient",
    "LightStage",
    "LocalLLMProvider",
    "MultiSessionProvider",
    "PendingCandidate",
    "PendingCandidateStore",
    "PendingProcessor",
    "PersonalDistill",
    "PrivacyGuard",
    "RemStage",
    "Session",
    "SessionMeta",
    "SessionProvider",
    "SessionTurn",
    "Signal",
    "SignalReporter",
    "TraeSessionProvider",
    "adjust_budget_by_signal_count",
    "chat_with_schema",
    "create_session_provider",
    "get_prompt_template",
    "render_system_prompt",
    "validate_against_schema",
]
