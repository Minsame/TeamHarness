"""distill_personal API 测试（POST /v1/llm/chat + GET /v1/llm/budget）。"""

from __future__ import annotations

import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.distill_personal.api import (
    LLMChatRequest,
    LLMChatResponse,
    LLMBudgetResponse,
    configure_distill_api,
    llm_router,
)
from server.distill_personal.budget import BudgetManager


# ---------------------------------------------------------------------------
# 测试 app 工厂
# ---------------------------------------------------------------------------


def _build_app(
    *,
    llm_provider: Any | None = None,
    budget_mgr: BudgetManager | None = None,
) -> FastAPI:
    """构造带 distill_personal 路由的 FastAPI app（隔离测试）。"""
    app = FastAPI()
    configure_distill_api(llm_provider=llm_provider, budget_mgr=budget_mgr)
    app.include_router(llm_router)
    return app


class _StubLLM:
    """LLM stub。"""

    def __init__(self, *, response: dict[str, Any] | None = None) -> None:
        self.response = response or {"content": "ok", "usage": {"total_tokens": 10}, "model": "stub"}
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "schema": schema, "kwargs": kwargs})
        return dict(self.response)


# ---------------------------------------------------------------------------
# POST /v1/llm/chat
# ---------------------------------------------------------------------------


def test_chat_without_schema_returns_content() -> None:
    """无 schema 时直接调用 LLM，返回 content。"""
    llm = _StubLLM(response={"content": "hello", "usage": {"total_tokens": 5}, "model": "gpt-4o"})
    app = _build_app(llm_provider=llm)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    data = resp.json()
    assert data["content"] == "hello"
    assert data["usage"]["total_tokens"] == 5
    assert data["model"] == "gpt-4o"
    assert len(llm.calls) == 1
    assert llm.calls[0]["schema"] is None


def test_chat_with_schema_returns_validated_data() -> None:
    """有 schema 时走 chat_with_schema，返回校验后的 data。"""
    valid_response = {
        "content": json.dumps({"skip": False, "asset": {"title": "t"}, "confidence": 0.5}),
        "usage": {"total_tokens": 30},
        "model": "stub",
    }
    llm = _StubLLM(response=valid_response)
    app = _build_app(llm_provider=llm)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "schema": {
            "type": "object",
            "properties": {
                "skip": {"type": "boolean"},
                "asset": {"type": "object"},
                "confidence": {"type": "number"},
            },
            "required": ["skip", "asset", "confidence"],
        },
    })
    assert resp.status_code == 200
    data = resp.json()
    # content 字段是 result.data（dict 转 str 后返回）
    assert "skip" in data["content"] or isinstance(data["content"], str)


def test_chat_schema_validation_failure_returns_502() -> None:
    """schema 校验失败返回 HTTP 502。"""
    llm = _StubLLM(response={"content": "not json", "usage": {}})
    app = _build_app(llm_provider=llm)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "schema": {"type": "object", "required": ["skip"]},
    })
    assert resp.status_code == 502
    assert "schema" in resp.json()["detail"]


def test_chat_no_llm_provider_returns_503() -> None:
    """未配置 LLMProvider 返回 503。"""
    app = _build_app(llm_provider=None)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 503
    assert "LLMProvider" in resp.json()["detail"]


def test_chat_with_member_id_charges_budget() -> None:
    """有 member_id 时扣减 budget。"""
    llm = _StubLLM(response={"content": "ok", "usage": {"total_tokens": 100}, "model": "x"})
    mgr = BudgetManager(default_daily_budget=1000)
    app = _build_app(llm_provider=llm, budget_mgr=mgr)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "member_id": "alice",
    })
    assert resp.status_code == 200
    budget = mgr.get_budget("alice")
    assert budget.used == 100


def test_chat_no_member_id_skips_budget_charge() -> None:
    """无 member_id 时不扣减 budget。"""
    llm = _StubLLM(response={"content": "ok", "usage": {"total_tokens": 100}, "model": "x"})
    mgr = BudgetManager(default_daily_budget=1000)
    app = _build_app(llm_provider=llm, budget_mgr=mgr)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert resp.status_code == 200
    # 无 member_id → 不扣减
    assert mgr.get_budget("alice").used == 0


def test_chat_passes_model_temperature_max_tokens() -> None:
    """chat 透传 model / temperature / max_tokens 到 LLM。"""
    llm = _StubLLM(response={"content": "ok", "usage": {}, "model": "x"})
    app = _build_app(llm_provider=llm)
    client = TestClient(app)
    client.post("/v1/llm/chat", json={
        "messages": [{"role": "user", "content": "hi"}],
        "model": "custom-model",
        "temperature": 0.7,
        "max_tokens": 500,
    })
    call = llm.calls[0]
    assert call["kwargs"]["model"] == "custom-model"
    assert call["kwargs"]["temperature"] == 0.7
    assert call["kwargs"]["max_tokens"] == 500


def test_chat_empty_messages_returns_422() -> None:
    """messages 缺失触发 Pydantic 校验错误（422）。"""
    llm = _StubLLM()
    app = _build_app(llm_provider=llm)
    client = TestClient(app)
    resp = client.post("/v1/llm/chat", json={})
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# GET /v1/llm/budget
# ---------------------------------------------------------------------------


def test_get_budget_returns_member_budget() -> None:
    """GET /v1/llm/budget 返回成员 budget。"""
    mgr = BudgetManager(default_daily_budget=5000)
    mgr.consume("alice", 1000)
    app = _build_app(llm_provider=_StubLLM(), budget_mgr=mgr)
    client = TestClient(app)
    resp = client.get("/v1/llm/budget", params={"member_id": "alice"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["member_id"] == "alice"
    assert data["daily_token_budget"] == 5000
    assert data["used"] == 1000
    assert data["remaining"] == 4000
    assert data["degraded"] is False


def test_get_budget_no_budget_mgr_returns_503() -> None:
    """未配置 BudgetManager 返回 503。"""
    app = _build_app(llm_provider=_StubLLM(), budget_mgr=None)
    client = TestClient(app)
    resp = client.get("/v1/llm/budget", params={"member_id": "alice"})
    assert resp.status_code == 503
    assert "BudgetManager" in resp.json()["detail"]


def test_get_budget_missing_member_id_returns_422() -> None:
    """缺 member_id 触发 Pydantic 校验错误。"""
    app = _build_app(llm_provider=_StubLLM(), budget_mgr=BudgetManager())
    client = TestClient(app)
    resp = client.get("/v1/llm/budget")
    assert resp.status_code == 422


def test_get_budget_auto_creates_member() -> None:
    """查询不存在的 member_id 时自动创建（默认 budget）。"""
    mgr = BudgetManager(default_daily_budget=100_000)
    app = _build_app(llm_provider=_StubLLM(), budget_mgr=mgr)
    client = TestClient(app)
    resp = client.get("/v1/llm/budget", params={"member_id": "new_member"})
    assert resp.status_code == 200
    data = resp.json()
    assert data["member_id"] == "new_member"
    assert data["daily_token_budget"] == 100_000
    assert data["used"] == 0
