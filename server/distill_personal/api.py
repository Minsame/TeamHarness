"""distill_personal 域 FastAPI 路由 — Agent 7 公共 API 契约。

对应占位 API 契约（依赖方：Agent 10 集成测试，Agent 5 category-suggest 用 LLMChatProtocol 占位等待）：
- POST /v1/llm/chat (messages, schema) → {content, usage}
- GET  /v1/llm/budget (member_id) → {daily_token_budget, used}

依赖注入：
- configure_distill_api(services_dict) 由 FastAPI 启动事件调用
- LLMProvider（LocalLLMProvider 或自定义）注入后路由可调真实 LLM
- BudgetManager 注入后 /v1/llm/budget 返回真实预算
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.distill_personal.budget import BudgetManager
from server.distill_personal.llm_provider import LocalLLMProvider
from server.distill_personal.schema_validator import (
    DEFAULT_MAX_RETRIES,
    chat_with_schema,
    validate_against_schema,
)

logger = logging.getLogger(__name__)

llm_router = APIRouter(prefix="/v1/llm", tags=["distill_personal"])

# 模块级服务（启动时由 configure_distill_api 注入）
_LLM_PROVIDER: LocalLLMProvider | Any | None = None
_BUDGET_MGR: BudgetManager | None = None


def configure_distill_api(
    *,
    llm_provider: Any | None = None,
    budget_mgr: BudgetManager | None = None,
) -> None:
    """注入 distill_personal 域服务（由 FastAPI 启动事件调用）。"""
    global _LLM_PROVIDER, _BUDGET_MGR
    _LLM_PROVIDER = llm_provider
    _BUDGET_MGR = budget_mgr


def _require_llm() -> Any:
    if _LLM_PROVIDER is None:
        raise HTTPException(status_code=503, detail="LLMProvider 未配置")
    return _LLM_PROVIDER


def _require_budget_mgr() -> BudgetManager:
    if _BUDGET_MGR is None:
        raise HTTPException(status_code=503, detail="BudgetManager 未配置")
    return _BUDGET_MGR


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------


class LLMChatMessage(BaseModel):
    role: str
    content: str


class LLMChatRequest(BaseModel):
    """POST /v1/llm/chat 请求体。"""

    messages: list[LLMChatMessage]
    schema: dict[str, Any] | None = None
    model: str | None = None
    temperature: float = 0.2
    max_tokens: int | None = None
    member_id: str = ""  # 用于 budget 归属


class LLMUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class LLMChatResponse(BaseModel):
    """POST /v1/llm/chat 响应体。"""

    content: str
    usage: dict[str, Any] = Field(default_factory=dict)
    model: str = ""


class LLMBudgetResponse(BaseModel):
    """GET /v1/llm/budget 响应体。"""

    member_id: str
    daily_token_budget: int
    used: int
    remaining: int
    reset_at: str
    degraded: bool


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


@llm_router.post("/chat", response_model=LLMChatResponse)
def llm_chat(request: LLMChatRequest) -> LLMChatResponse:
    """POST /v1/llm/chat — LLM 服务端代理。

    - 接收 messages + schema，调用底层 LLM Provider
    - 若 schema 非 None，强制 JSON schema 校验 + 失败重试
    - 返回 {content, usage, model}
    - budget 归属：member_id 非空时按成员计费（扣减 budget）
    """
    llm = _require_llm()
    messages = [{"role": m.role, "content": m.content} for m in request.messages]

    # 无 schema：直接调用，不做校验
    if request.schema is None:
        resp = llm.chat(
            messages,
            schema=None,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
        )
        # budget 归属
        usage = dict(resp.get("usage") or {})
        _charge_budget(request.member_id, usage)
        return LLMChatResponse(
            content=str(resp.get("content", "")),
            usage=usage,
            model=str(resp.get("model") or request.model or ""),
        )

    # 有 schema：走 chat_with_schema 强制校验 + 重试
    result = chat_with_schema(
        llm,
        messages=messages,
        schema=request.schema,
        max_retries=DEFAULT_MAX_RETRIES,
    )
    if not result.success:
        raise HTTPException(
            status_code=502,
            detail=f"LLM schema 校验失败（重试 {result.attempts} 次）: {result.last_error}",
        )
    # budget 归属
    _charge_budget(request.member_id, result.total_usage)
    return LLMChatResponse(
        content=str(result.data) if result.data is not None else "",
        usage=dict(result.total_usage),
        model=str(request.model or ""),
    )


@llm_router.get("/budget", response_model=LLMBudgetResponse)
def llm_budget(
    member_id: str = Query(..., description="成员标识"),
) -> LLMBudgetResponse:
    """GET /v1/llm/budget — 查询成员每日 token 预算。"""
    mgr = _require_budget_mgr()
    budget = mgr.get_budget(member_id)
    return LLMBudgetResponse(
        member_id=budget.member_id,
        daily_token_budget=budget.daily_token_budget,
        used=budget.used,
        remaining=budget.remaining,
        reset_at=budget.reset_at,
        degraded=budget.degraded,
    )


# ---------------------------------------------------------------------------
# 内部：budget 归属
# ---------------------------------------------------------------------------


def _charge_budget(member_id: str, usage: dict[str, Any]) -> None:
    """按 usage 扣减成员 budget（缺陷 2.1 LLM 成本归属）。"""
    if not member_id or _BUDGET_MGR is None:
        return
    tokens = int(usage.get("total_tokens", 0))
    if tokens <= 0:
        return
    try:
        _BUDGET_MGR.consume(member_id, tokens)
    except Exception as exc:  # noqa: BLE001
        logger.warning("budget 扣减失败（member_id=%s）: %s", member_id, exc)


__all__ = [
    "LLMBudgetResponse",
    "LLMChatRequest",
    "LLMChatResponse",
    "LLMChatMessage",
    "LLMUsage",
    "configure_distill_api",
    "llm_router",
]
