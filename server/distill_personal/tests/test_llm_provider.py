"""llm_provider 测试（SubTask 7.7 LLM 服务端代理 + budget）。"""

from __future__ import annotations

from typing import Any

import pytest

from server.distill_personal.llm_provider import (
    LLMBudget,
    LLMProviderClient,
    LocalLLMProvider,
    AnthropicLLMProvider,
    create_llm_provider,
    default_budget,
)


# ---------------------------------------------------------------------------
# LLMBudget
# ---------------------------------------------------------------------------


def test_llm_budget_defaults() -> None:
    """LLMBudget 默认值。"""
    b = LLMBudget(member_id="alice")
    assert b.member_id == "alice"
    assert b.daily_token_budget == 100_000
    assert b.used == 0
    assert b.reset_at == ""
    assert b.degraded is False


def test_llm_budget_remaining_property() -> None:
    """remaining = daily_token_budget - used。"""
    b = LLMBudget(member_id="alice", daily_token_budget=1000, used=300)
    assert b.remaining == 700


def test_llm_budget_remaining_never_negative() -> None:
    """remaining 不为负。"""
    b = LLMBudget(member_id="alice", daily_token_budget=100, used=200)
    assert b.remaining == 0


def test_llm_budget_exhausted() -> None:
    """remaining <= 0 视为耗尽。"""
    assert not LLMBudget(member_id="x", daily_token_budget=100, used=50).exhausted
    assert LLMBudget(member_id="x", daily_token_budget=100, used=100).exhausted
    assert LLMBudget(member_id="x", daily_token_budget=100, used=200).exhausted


def test_llm_budget_consume_normal() -> None:
    """正常 consume 扣减 used。"""
    b = LLMBudget(member_id="x", daily_token_budget=1000)
    actual = b.consume(300)
    assert actual == 300
    assert b.used == 300
    assert not b.degraded


def test_llm_budget_consume_zero_or_negative_returns_zero() -> None:
    """consume 0 或负数返回 0。"""
    b = LLMBudget(member_id="x", daily_token_budget=1000)
    assert b.consume(0) == 0
    assert b.consume(-100) == 0
    assert b.used == 0


def test_llm_budget_consume_more_than_remaining_returns_actual_and_degrades() -> None:
    """consume 超过 remaining 返回实际消费量，并标记 degraded。"""
    b = LLMBudget(member_id="x", daily_token_budget=500)
    actual = b.consume(800)
    assert actual == 500
    assert b.used == 500
    assert b.exhausted
    assert b.degraded


def test_llm_budget_reset_clears_state() -> None:
    """reset 清零 used + degraded，reset_at 更新。"""
    b = LLMBudget(member_id="x", daily_token_budget=1000, used=800, degraded=True)
    b.reset()
    assert b.used == 0
    assert not b.degraded
    assert b.reset_at  # 非空


def test_llm_budget_reset_with_new_budget() -> None:
    """reset 时可更新 daily_token_budget。"""
    b = LLMBudget(member_id="x", daily_token_budget=1000)
    b.reset(new_budget=2000)
    assert b.daily_token_budget == 2000


def test_llm_budget_reset_negative_budget_clamped_to_zero() -> None:
    """reset 负数 budget 被 clamp 到 0。"""
    b = LLMBudget(member_id="x", daily_token_budget=1000)
    b.reset(new_budget=-100)
    assert b.daily_token_budget == 0


def test_llm_budget_to_dict() -> None:
    """to_dict 字段完整。"""
    b = LLMBudget(member_id="alice", daily_token_budget=1000, used=200)
    d = b.to_dict()
    assert d["member_id"] == "alice"
    assert d["daily_token_budget"] == 1000
    assert d["used"] == 200
    assert d["remaining"] == 800
    assert d["degraded"] is False


def test_default_budget_factory() -> None:
    """default_budget 工厂函数。"""
    b = default_budget("alice", daily_token_budget=5000)
    assert b.member_id == "alice"
    assert b.daily_token_budget == 5000
    assert b.used == 0
    assert b.reset_at  # 次日 00:00 UTC
    assert not b.degraded


# ---------------------------------------------------------------------------
# LLMProviderClient（mocked httpx）
# ---------------------------------------------------------------------------


class _FakeResponse:
    """模拟 httpx.Response。"""

    def __init__(self, status_code: int, json_data: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._json = json_data or {}
        self.text = text or ""

    def json(self) -> dict:
        return self._json


class _FakeHttpClient:
    """模拟 httpx.Client。"""

    def __init__(self, *, post_resp: _FakeResponse | None = None, get_resp: _FakeResponse | None = None) -> None:
        self.post_resp = post_resp
        self.get_resp = get_resp
        self.post_calls: list[dict[str, Any]] = []
        self.get_calls: list[dict[str, Any]] = []

    def post(self, url: str, *, json: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.post_calls.append({"url": url, "json": json, "headers": headers})
        return self.post_resp or _FakeResponse(500, text="not configured")

    def get(self, url: str, *, params: Any = None, headers: Any = None, timeout: Any = None) -> _FakeResponse:
        self.get_calls.append({"url": url, "params": params, "headers": headers})
        return self.get_resp or _FakeResponse(500, text="not configured")


def test_llm_provider_client_chat_success() -> None:
    """chat 成功调用 POST /v1/llm/chat。"""
    resp = _FakeResponse(
        200,
        json_data={
            "content": "hello",
            "usage": {"total_tokens": 42},
            "model": "gpt-4o-mini",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    client = LLMProviderClient(
        server_url="https://th.example.com",
        api_key="key-123",
        member_id="alice",
        http_client=http,
    )
    result = client.chat(messages=[{"role": "user", "content": "hi"}], schema={"type": "object"})
    assert result["content"] == "hello"
    assert result["usage"]["total_tokens"] == 42
    assert result["model"] == "gpt-4o-mini"
    # 验证请求
    call = http.post_calls[0]
    assert call["url"] == "https://th.example.com/v1/llm/chat"
    assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert call["json"]["schema"] == {"type": "object"}
    assert call["json"]["member_id"] == "alice"
    # auth header
    assert call["headers"]["Authorization"] == "Bearer key-123"


def test_llm_provider_client_chat_http_error_raises() -> None:
    """HTTP 4xx/5xx 抛 RuntimeError。"""
    resp = _FakeResponse(503, text="service unavailable")
    http = _FakeHttpClient(post_resp=resp)
    client = LLMProviderClient(server_url="https://th.example.com", http_client=http)
    with pytest.raises(RuntimeError, match="HTTP 503"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_llm_provider_client_chat_non_json_response_raises() -> None:
    """非 JSON 响应抛 RuntimeError。"""
    class _NonJsonResponse(_FakeResponse):
        def json(self) -> dict:
            raise ValueError("not json")
    resp = _NonJsonResponse(200)
    http = _FakeHttpClient(post_resp=resp)
    client = LLMProviderClient(server_url="https://th.example.com", http_client=http)
    with pytest.raises(RuntimeError, match="非 JSON"):
        client.chat(messages=[{"role": "user", "content": "hi"}])


def test_llm_provider_client_chat_passes_optional_params() -> None:
    """chat 透传 model / temperature / max_tokens。"""
    resp = _FakeResponse(200, json_data={"content": "ok", "usage": {}, "model": "gpt-4o"})
    http = _FakeHttpClient(post_resp=resp)
    client = LLMProviderClient(server_url="https://th.example.com", http_client=http, model="default-model")
    client.chat(
        messages=[{"role": "user", "content": "hi"}],
        model="custom-model",
        temperature=0.5,
        max_tokens=100,
    )
    payload = http.post_calls[0]["json"]
    assert payload["model"] == "custom-model"
    assert payload["temperature"] == 0.5
    assert payload["max_tokens"] == 100


def test_llm_provider_client_get_budget_success() -> None:
    """get_budget 成功调用 GET /v1/llm/budget。"""
    resp = _FakeResponse(
        200,
        json_data={
            "daily_token_budget": 5000,
            "used": 1000,
            "reset_at": "2026-08-08T00:00:00Z",
            "degraded": False,
        },
    )
    http = _FakeHttpClient(get_resp=resp)
    client = LLMProviderClient(
        server_url="https://th.example.com",
        member_id="alice",
        http_client=http,
    )
    budget = client.get_budget()
    assert budget.member_id == "alice"
    assert budget.daily_token_budget == 5000
    assert budget.used == 1000
    assert budget.reset_at == "2026-08-08T00:00:00Z"
    assert not budget.degraded
    # 验证请求
    call = http.get_calls[0]
    assert call["url"] == "https://th.example.com/v1/llm/budget"
    assert call["params"] == {"member_id": "alice"}


def test_llm_provider_client_get_budget_explicit_member_id() -> None:
    """get_budget 显式 member_id 覆盖默认。"""
    resp = _FakeResponse(200, json_data={"daily_token_budget": 5000, "used": 0})
    http = _FakeHttpClient(get_resp=resp)
    client = LLMProviderClient(
        server_url="https://th.example.com",
        member_id="default",
        http_client=http,
    )
    client.get_budget(member_id="override")
    call = http.get_calls[0]
    assert call["params"] == {"member_id": "override"}


def test_llm_provider_client_get_budget_http_error_raises() -> None:
    """get_budget HTTP 错误抛 RuntimeError。"""
    resp = _FakeResponse(404, text="not found")
    http = _FakeHttpClient(get_resp=resp)
    client = LLMProviderClient(server_url="https://th.example.com", http_client=http)
    with pytest.raises(RuntimeError, match="HTTP 404"):
        client.get_budget(member_id="alice")


def test_llm_provider_client_no_api_key_no_auth_header() -> None:
    """无 api_key 时 Authorization 头不设置。"""
    resp = _FakeResponse(200, json_data={"content": "", "usage": {}, "model": ""})
    http = _FakeHttpClient(post_resp=resp)
    client = LLMProviderClient(server_url="https://th.example.com", http_client=http)
    client.chat(messages=[{"role": "user", "content": "hi"}])
    headers = http.post_calls[0]["headers"]
    assert "Authorization" not in headers
    assert headers["Content-Type"] == "application/json"


def test_llm_provider_client_server_url_trailing_slash_stripped() -> None:
    """server_url 末尾 / 被剥离。"""
    client = LLMProviderClient(server_url="https://th.example.com/")
    assert client.server_url == "https://th.example.com"


# ---------------------------------------------------------------------------
# LocalLLMProvider（mocked httpx）
# ---------------------------------------------------------------------------


def test_local_llm_provider_chat_success() -> None:
    """LocalLLMProvider.chat 成功调用 OpenAI 兼容 /v1/chat/completions。"""
    resp = _FakeResponse(
        200,
        json_data={
            "choices": [{"message": {"content": "answer"}}],
            "usage": {"total_tokens": 30},
            "model": "gpt-4o-mini",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    provider = LocalLLMProvider(
        base_url="https://api.openai.com",
        api_key="sk-xxx",
        model="gpt-4o-mini",
        http_client=http,
    )
    result = provider.chat(
        messages=[{"role": "user", "content": "hi"}],
        schema={"type": "object"},
    )
    assert result["content"] == "answer"
    assert result["usage"]["total_tokens"] == 30
    assert result["model"] == "gpt-4o-mini"
    payload = http.post_calls[0]["json"]
    assert payload["model"] == "gpt-4o-mini"
    # schema 通过 response_format 传递
    assert payload["response_format"] == {"type": "json_object"}


def test_local_llm_provider_no_base_url_raises() -> None:
    """未配置 base_url 时 chat 抛 RuntimeError。"""
    provider = LocalLLMProvider(base_url="", api_key="", model="")
    with pytest.raises(RuntimeError, match="未配置 LLM_BASE_URL"):
        provider.chat(messages=[{"role": "user", "content": "hi"}])


def test_local_llm_provider_http_error_raises() -> None:
    """HTTP 错误抛 RuntimeError。"""
    resp = _FakeResponse(401, text="unauthorized")
    http = _FakeHttpClient(post_resp=resp)
    provider = LocalLLMProvider(
        base_url="https://api.openai.com",
        api_key="sk-xxx",
        http_client=http,
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        provider.chat(messages=[{"role": "user", "content": "hi"}])


def test_local_llm_provider_empty_choices_returns_empty_content() -> None:
    """空 choices 返回空 content。"""
    resp = _FakeResponse(200, json_data={"choices": [], "usage": {}, "model": "x"})
    http = _FakeHttpClient(post_resp=resp)
    provider = LocalLLMProvider(
        base_url="https://api.openai.com",
        http_client=http,
    )
    result = provider.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["content"] == ""


def test_local_llm_provider_reads_env_vars(monkeypatch) -> None:
    """LocalLLMProvider 从环境变量读取配置。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example.com")
    monkeypatch.setenv("LLM_API_KEY", "env-key")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    provider = LocalLLMProvider()
    assert provider.base_url == "https://env.example.com"
    assert provider.api_key == "env-key"
    assert provider.model == "env-model"


# ---------------------------------------------------------------------------
# AnthropicLLMProvider（mocked httpx）
# ---------------------------------------------------------------------------


def test_anthropic_llm_provider_chat_success() -> None:
    """AnthropicLLMProvider.chat 成功调用 /v1/messages。"""
    resp = _FakeResponse(
        200,
        json_data={
            "content": [{"type": "text", "text": "answer"}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model": "claude-3-5-sonnet-20241022",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        model="claude-3-5-sonnet-20241022",
        http_client=http,
    )
    result = provider.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["content"] == "answer"
    assert result["usage"]["total_tokens"] == 15  # input + output
    assert result["usage"]["prompt_tokens"] == 10
    assert result["usage"]["completion_tokens"] == 5
    assert result["model"] == "claude-3-5-sonnet-20241022"
    # 验证请求
    call = http.post_calls[0]
    assert call["url"] == "https://api.anthropic.com/v1/messages"
    # Anthropic 认证头
    assert call["headers"]["x-api-key"] == "sk-ant-xxx"
    assert call["headers"]["anthropic-version"] == "2023-06-01"
    # payload 格式
    assert call["json"]["model"] == "claude-3-5-sonnet-20241022"
    assert call["json"]["messages"] == [{"role": "user", "content": "hi"}]
    assert call["json"]["max_tokens"] == 4096  # Anthropic 必填，默认 4096


def test_anthropic_llm_provider_system_message_separated() -> None:
    """system message 分离到顶层 system 字段，不在 messages 中。"""
    resp = _FakeResponse(
        200,
        json_data={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "model": "claude-3-5-sonnet-20241022",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        http_client=http,
    )
    provider.chat(messages=[
        {"role": "system", "content": "You are a helpful assistant."},
        {"role": "user", "content": "hi"},
    ])
    payload = http.post_calls[0]["json"]
    # system 分离到顶层
    assert payload["system"] == "You are a helpful assistant."
    # messages 中不包含 system
    assert all(m["role"] != "system" for m in payload["messages"])
    assert len(payload["messages"]) == 1
    assert payload["messages"][0]["role"] == "user"


def test_anthropic_llm_provider_schema_injects_json_instruction() -> None:
    """schema 通过 prompt 指令注入（Anthropic 无原生 JSON 模式）。"""
    resp = _FakeResponse(
        200,
        json_data={
            "content": [{"type": "text", "text": '{"skip": true}'}],
            "usage": {"input_tokens": 10, "output_tokens": 5},
            "model": "claude-3-5-sonnet-20241022",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        http_client=http,
    )
    provider.chat(
        messages=[{"role": "user", "content": "提炼规则"}],
        schema={"type": "object", "properties": {"skip": {"type": "boolean"}}},
    )
    payload = http.post_calls[0]["json"]
    # 无 response_format（Anthropic 不支持）
    assert "response_format" not in payload
    # schema 指令注入到 user message
    user_msg = payload["messages"][-1]["content"]
    assert "JSON" in user_msg
    assert "skip" in user_msg  # schema 内容出现在 prompt 中


def test_anthropic_llm_provider_max_tokens_required() -> None:
    """max_tokens 未传时默认 4096（Anthropic 必填）。"""
    resp = _FakeResponse(
        200,
        json_data={
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 5, "output_tokens": 2},
            "model": "claude-3-5-sonnet-20241022",
        },
    )
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        http_client=http,
    )
    provider.chat(messages=[{"role": "user", "content": "hi"}])
    assert http.post_calls[0]["json"]["max_tokens"] == 4096

    # 显式传 max_tokens
    provider.chat(messages=[{"role": "user", "content": "hi"}], max_tokens=100)
    assert http.post_calls[1]["json"]["max_tokens"] == 100


def test_anthropic_llm_provider_no_api_key_raises() -> None:
    """未配置 api_key 时 chat 抛 RuntimeError。"""
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com", api_key="", http_client=_FakeHttpClient()
    )
    with pytest.raises(RuntimeError, match="未配置 LLM_API_KEY"):
        provider.chat(messages=[{"role": "user", "content": "hi"}])


def test_anthropic_llm_provider_http_error_raises() -> None:
    """HTTP 错误抛 RuntimeError。"""
    resp = _FakeResponse(401, text="unauthorized")
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        http_client=http,
    )
    with pytest.raises(RuntimeError, match="HTTP 401"):
        provider.chat(messages=[{"role": "user", "content": "hi"}])


def test_anthropic_llm_provider_empty_content_returns_empty_string() -> None:
    """空 content 响应返回空字符串。"""
    resp = _FakeResponse(200, json_data={"content": [], "usage": {}, "model": "x"})
    http = _FakeHttpClient(post_resp=resp)
    provider = AnthropicLLMProvider(
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        http_client=http,
    )
    result = provider.chat(messages=[{"role": "user", "content": "hi"}])
    assert result["content"] == ""


def test_anthropic_llm_provider_reads_env_vars(monkeypatch) -> None:
    """AnthropicLLMProvider 从环境变量读取配置。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://env.anthropic.com")
    monkeypatch.setenv("LLM_API_KEY", "env-ant-key")
    monkeypatch.setenv("LLM_MODEL", "claude-3-opus")
    provider = AnthropicLLMProvider()
    assert provider.base_url == "https://env.anthropic.com"
    assert provider.api_key == "env-ant-key"
    assert provider.model == "claude-3-opus"


# ---------------------------------------------------------------------------
# create_llm_provider 工厂函数
# ---------------------------------------------------------------------------


def test_create_llm_provider_defaults_to_openai(monkeypatch) -> None:
    """未指定 provider 时默认 openai。"""
    monkeypatch.setenv("LLM_BASE_URL", "https://api.openai.com")
    monkeypatch.setenv("LLM_API_KEY", "sk-xxx")
    monkeypatch.delenv("LLM_PROVIDER", raising=False)
    provider = create_llm_provider()
    assert isinstance(provider, LocalLLMProvider)


def test_create_llm_provider_explicit_openai() -> None:
    """显式指定 openai。"""
    provider = create_llm_provider(
        provider="openai",
        base_url="https://api.openai.com",
        api_key="sk-xxx",
    )
    assert isinstance(provider, LocalLLMProvider)


def test_create_llm_provider_anthropic() -> None:
    """指定 anthropic 返回 AnthropicLLMProvider。"""
    provider = create_llm_provider(
        provider="anthropic",
        base_url="https://api.anthropic.com",
        api_key="sk-ant-xxx",
        model="claude-3-5-sonnet-20241022",
    )
    assert isinstance(provider, AnthropicLLMProvider)
    assert provider.model == "claude-3-5-sonnet-20241022"


def test_create_llm_provider_reads_llm_provider_env(monkeypatch) -> None:
    """从 LLM_PROVIDER 环境变量读取 provider 类型。"""
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("LLM_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("LLM_API_KEY", "sk-ant-xxx")
    provider = create_llm_provider()
    assert isinstance(provider, AnthropicLLMProvider)


def test_create_llm_provider_unknown_type_raises(monkeypatch) -> None:
    """未知 provider 类型抛 ValueError。"""
    monkeypatch.setenv("LLM_PROVIDER", "unknown")
    with pytest.raises(ValueError, match="未知"):
        create_llm_provider(
            base_url="https://x.com",
            api_key="x",
        )
