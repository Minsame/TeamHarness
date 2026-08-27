"""LLM 服务端代理接入 + budget 管理。

对应 SubTask 7.7（POST /v1/llm/chat + GET /v1/llm/budget）+ SubTask 7.8（超限降级）：
- LLMProviderClient：客户端封装，通过 httpx 调服务端 /v1/llm/chat 与 /v1/llm/budget
- LocalLLMProvider：本地直连 LLM 兼容路径（OpenAI API 格式，便于测试与离线场景）
- LLMBudget：每成员 daily_token_budget + used + reset 逻辑
- 超限降级：budget 不足时调用方应跳过 Deep 阶段，候选入 pending（见 deep_stage.py）

设计要点：
- 客户端一级提炼通过服务端代理调用 LLM（统一计费与密钥管理）
- 服务端 LLMProvider 通过 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL 配置
- 兼容 OpenAI API 格式（覆盖 OpenAI / DeepSeek / Moonshot / 通义千问兼容模式 / 本地 vLLM）
- LLMProviderClient 实现 LLMChatLike 协议，可被 chat_with_schema 直接复用
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# budget 数据结构
# ---------------------------------------------------------------------------


@dataclass
class LLMBudget:
    """成员每日 token 预算。

    - daily_token_budget：每日总额度（按成员配置）
    - used：今日已用 token
    - reset_at：下次重置时间（ISO 字符串，通常为次日 00:00 UTC）
    - member_id：成员标识
    """

    member_id: str
    daily_token_budget: int = 100_000
    used: int = 0
    reset_at: str = ""
    # 超限降级标记（Deep 跳过，候选入 pending）
    degraded: bool = False

    @property
    def remaining(self) -> int:
        return max(0, self.daily_token_budget - self.used)

    @property
    def exhausted(self) -> bool:
        """是否已耗尽（剩余 <= 0）。"""
        return self.remaining <= 0

    def consume(self, tokens: int) -> int:
        """消费 tokens，返回实际消费量（不足时返回剩余并标记 degraded）。"""
        if tokens <= 0:
            return 0
        actual = min(tokens, self.remaining)
        self.used += actual
        if self.remaining <= 0:
            self.degraded = True
        return actual

    def reset(self, *, new_budget: int | None = None) -> None:
        """重置预算（次日恢复逻辑）。

        - new_budget=None：保持原 daily_token_budget
        - new_budget=int：更新 daily_token_budget
        - used 清零，degraded 清除
        """
        if new_budget is not None:
            self.daily_token_budget = max(0, new_budget)
        self.used = 0
        self.degraded = False
        self.reset_at = _next_day_iso()

    def to_dict(self) -> dict[str, Any]:
        return {
            "member_id": self.member_id,
            "daily_token_budget": self.daily_token_budget,
            "used": self.used,
            "remaining": self.remaining,
            "reset_at": self.reset_at,
            "degraded": self.degraded,
        }


# ---------------------------------------------------------------------------
# LLM chat 结果
# ---------------------------------------------------------------------------


@dataclass
class LLMChatResult:
    """LLM 单次调用结果（与 LLMChatProtocol.chat 返回值对齐）。"""

    content: str
    usage: dict[str, Any] = field(default_factory=dict)
    model: str = ""
    raw: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"content": self.content, "usage": dict(self.usage), "model": self.model}


# ---------------------------------------------------------------------------
# LLMProviderClient — 服务端代理客户端
# ---------------------------------------------------------------------------


class LLMProviderClient:
    """LLM 服务端代理客户端。

    通过 httpx 调用服务端：
    - POST /v1/llm/chat：发送 messages + schema，返回 {content, usage}
    - GET /v1/llm/budget?member_id=xxx：查询每日 token 预算

    实现 LLMChatLike 协议，可被 chat_with_schema 直接复用。

    使用：
        client = LLMProviderClient(server_url="https://th.example.com", api_key="...")
        result = client.chat(messages, schema=schema)  # dict 形式
        budget = client.get_budget(member_id="alice")
    """

    def __init__(
        self,
        *,
        server_url: str,
        api_key: str = "",
        member_id: str = "",
        model: str = "gpt-4o-mini",
        timeout_seconds: int = 30,
        http_client: Any | None = None,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.api_key = api_key
        self.member_id = member_id
        self.model = model
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client  # 注入便于测试

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """POST /v1/llm/chat。

        返回 {"content": str, "usage": {...}, "model": str}。

        服务端响应体格式（与 LocalLLMProvider 一致）：
            {
              "content": str,
              "usage": {"prompt_tokens": int, "completion_tokens": int, "total_tokens": int},
              "model": str
            }
        """
        payload: dict[str, Any] = {
            "messages": messages,
            "model": model or self.model,
            "temperature": temperature,
        }
        if schema is not None:
            payload["schema"] = schema
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if self.member_id:
            payload["member_id"] = self.member_id

        client = self._get_http_client()
        import httpx  # 延迟导入，避免无网络环境 import 失败

        try:
            resp = client.post(
                f"{self.server_url}/v1/llm/chat",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LLM 服务端代理请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"LLM 服务端代理返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        try:
            data = resp.json()
        except ValueError as exc:
            raise RuntimeError(f"LLM 服务端代理响应非 JSON: {exc}") from exc
        # 标准化字段
        return {
            "content": str(data.get("content", "")),
            "usage": dict(data.get("usage") or {}),
            "model": str(data.get("model") or model or self.model),
        }

    def get_budget(self, member_id: str | None = None) -> LLMBudget:
        """GET /v1/llm/budget?member_id=xxx。

        服务端响应体格式：
            {"daily_token_budget": int, "used": int, "reset_at": str, "degraded": bool}
        """
        mid = member_id or self.member_id
        client = self._get_http_client()
        import httpx

        try:
            resp = client.get(
                f"{self.server_url}/v1/llm/budget",
                params={"member_id": mid},
                headers=self._auth_headers(),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"LLM budget 查询失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"LLM budget 查询返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        return LLMBudget(
            member_id=mid,
            daily_token_budget=int(data.get("daily_token_budget", 100_000)),
            used=int(data.get("used", 0)),
            reset_at=str(data.get("reset_at", "")),
            degraded=bool(data.get("degraded", False)),
        )

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            import httpx
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


# ---------------------------------------------------------------------------
# LocalLLMProvider — 本地直连 LLM（OpenAI 兼容格式）
# ---------------------------------------------------------------------------


class LocalLLMProvider:
    """本地直连 LLM Provider（OpenAI API 兼容）。

    适用场景：
    - 客户端无服务端代理时直连 LLM（如本地 vLLM）
    - 测试环境 mock LLM（注入 fake_response 回调）
    - 离线场景（用本地小模型）

    通过 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL 环境变量配置。

    实现 LLMChatLike 协议，可被 chat_with_schema 直接复用。

    注意：本地直连不参与服务端统一计费，budget 需调用方自行维护。
    """

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 60,
        http_client: Any | None = None,
    ) -> None:
        self.base_url = (base_url or os.environ.get("LLM_BASE_URL", "")).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "gpt-4o-mini")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """调用 OpenAI 兼容 /v1/chat/completions。"""
        if not self.base_url:
            raise RuntimeError("LocalLLMProvider 未配置 LLM_BASE_URL")
        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        # schema 通过 response_format 传递（OpenAI 兼容模式）
        if schema is not None:
            payload["response_format"] = {"type": "json_object"}
        client = self._get_http_client()
        import httpx

        try:
            resp = client.post(
                f"{self.base_url}/v1/chat/completions",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"本地 LLM 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"本地 LLM 返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        choices = data.get("choices") or []
        content = ""
        if choices and isinstance(choices[0], dict):
            msg = choices[0].get("message") or {}
            content = str(msg.get("content", ""))
        usage = data.get("usage") or {}
        return {
            "content": content,
            "usage": dict(usage),
            "model": str(data.get("model") or model or self.model),
        }

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            import httpx
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers


# ---------------------------------------------------------------------------
# AnthropicLLMProvider — Anthropic Claude API 适配
# ---------------------------------------------------------------------------


class AnthropicLLMProvider:
    """Anthropic Claude LLM Provider（Messages API）。

    适用场景：
    - 接入 Anthropic Claude（claude-3-5-sonnet / claude-3-opus 等）
    - 通过 OpenRouter 等 Anthropic 兼容代理

    通过 LLM_BASE_URL + LLM_API_KEY + LLM_MODEL 环境变量配置。
    base_url 默认 https://api.anthropic.com（可指向代理）。

    API 差异（相对 OpenAI）：
    - 端点：/v1/messages（非 /v1/chat/completions）
    - 认证：x-api-key 头 + anthropic-version 头（非 Bearer）
    - system message 分离到顶层 system 字段
    - max_tokens 必填
    - 响应：content[0].text（非 choices[0].message.content）
    - usage：input_tokens + output_tokens（无 total_tokens，需汇总）

    JSON 模式：Anthropic 无原生 response_format，通过在 user message 中
    注入"以 JSON 格式输出"指令 + 解析响应文本实现。

    实现 LLMChatLike 协议（chat 方法），可被 chat_with_schema 直接复用。
    """

    # Anthropic API 版本（必填头）
    ANTHROPIC_VERSION = "2023-06-01"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout_seconds: int = 60,
        http_client: Any | None = None,
    ) -> None:
        self.base_url = (
            base_url or os.environ.get("LLM_BASE_URL", "https://api.anthropic.com")
        ).rstrip("/")
        self.api_key = api_key or os.environ.get("LLM_API_KEY", "")
        self.model = model or os.environ.get("LLM_MODEL", "claude-3-5-sonnet-20241022")
        self.timeout_seconds = timeout_seconds
        self._http_client = http_client

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        """调用 Anthropic /v1/messages。

        自动处理：
        - system message 分离到顶层 system 字段
        - schema 通过 prompt 指令注入（Anthropic 无原生 JSON 模式）
        - max_tokens 默认 4096（Anthropic 必填）
        """
        if not self.base_url:
            raise RuntimeError("AnthropicLLMProvider 未配置 LLM_BASE_URL")
        if not self.api_key:
            raise RuntimeError("AnthropicLLMProvider 未配置 LLM_API_KEY")

        # 分离 system message（Anthropic 要求 system 在顶层，不在 messages 中）
        system_prompt = ""
        non_system_messages: list[dict[str, str]] = []
        for msg in messages:
            if msg.get("role") == "system":
                system_prompt += msg.get("content", "") + "\n"
            else:
                non_system_messages.append(msg)

        # schema 注入：在最后一条 user message 追加 JSON 输出指令
        if schema is not None:
            schema_hint = (
                "\n\n请以 JSON 格式输出，遵循以下 schema：\n"
                + json.dumps(schema, ensure_ascii=False)
            )
            if non_system_messages and non_system_messages[-1].get("role") == "user":
                non_system_messages[-1] = {
                    **non_system_messages[-1],
                    "content": non_system_messages[-1].get("content", "") + schema_hint,
                }
            else:
                non_system_messages.append({"role": "user", "content": schema_hint})

        payload: dict[str, Any] = {
            "model": model or self.model,
            "messages": non_system_messages,
            "temperature": temperature,
            "max_tokens": max_tokens or 4096,  # Anthropic 必填
        }
        if system_prompt.strip():
            payload["system"] = system_prompt.strip()

        client = self._get_http_client()
        import httpx

        try:
            resp = client.post(
                f"{self.base_url}/v1/messages",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.timeout_seconds,
            )
        except httpx.HTTPError as exc:
            raise RuntimeError(f"Anthropic LLM 请求失败: {exc}") from exc
        if resp.status_code >= 400:
            raise RuntimeError(
                f"Anthropic LLM 返回 HTTP {resp.status_code}: {resp.text[:300]}"
            )
        data = resp.json()
        # 解析响应：content[0].text
        content_blocks = data.get("content") or []
        content = ""
        if content_blocks and isinstance(content_blocks[0], dict):
            content = str(content_blocks[0].get("text", ""))
        # usage 汇总：input_tokens + output_tokens → total_tokens
        usage_raw = data.get("usage") or {}
        input_tokens = int(usage_raw.get("input_tokens", 0))
        output_tokens = int(usage_raw.get("output_tokens", 0))
        usage = {
            "prompt_tokens": input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": input_tokens + output_tokens,
        }
        return {
            "content": content,
            "usage": usage,
            "model": str(data.get("model") or model or self.model),
        }

    def _get_http_client(self) -> Any:
        if self._http_client is None:
            import httpx
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        """Anthropic 认证头：x-api-key + anthropic-version。"""
        return {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": self.ANTHROPIC_VERSION,
        }


# ---------------------------------------------------------------------------
# create_llm_provider — 工厂函数
# ---------------------------------------------------------------------------


def create_llm_provider(
    *,
    provider: str | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    model: str | None = None,
    timeout_seconds: int = 60,
    http_client: Any | None = None,
) -> "LocalLLMProvider | AnthropicLLMProvider":
    """根据 LLM_PROVIDER 环境变量创建对应 LLM Provider。

    Args:
        provider: 显式指定 provider 类型（"openai" / "anthropic"）。
                  未指定时读 LLM_PROVIDER 环境变量，默认 "openai"。
        base_url/api_key/model: 显式配置，未指定时读 LLM_BASE_URL/LLM_API_KEY/LLM_MODEL 环境变量。
        timeout_seconds: HTTP 超时秒数。
        http_client: 注入 httpx client（用于测试）。

    Returns:
        LocalLLMProvider 或 AnthropicLLMProvider 实例。

    Raises:
        ValueError: provider 类型未知。
    """
    provider_type = (provider or os.environ.get("LLM_PROVIDER", "openai")).strip().lower()

    if provider_type == "openai":
        return LocalLLMProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
    elif provider_type == "anthropic":
        return AnthropicLLMProvider(
            base_url=base_url,
            api_key=api_key,
            model=model,
            timeout_seconds=timeout_seconds,
            http_client=http_client,
        )
    else:
        raise ValueError(
            f"未知的 LLM_PROVIDER: {provider_type}（支持: openai / anthropic）"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------


def _next_day_iso() -> str:
    """返回次日 00:00 UTC 的 ISO 字符串（budget 重置时间）。"""
    now = datetime.now(timezone.utc)
    # 次日 00:00 UTC
    next_day = datetime(now.year, now.month, now.day, tzinfo=timezone.utc)
    # +1 天
    from datetime import timedelta
    next_day = next_day + timedelta(days=1)
    return next_day.strftime("%Y-%m-%dT00:00:00Z")


def default_budget(member_id: str, *, daily_token_budget: int = 100_000) -> LLMBudget:
    """构造默认 budget（用于服务端首次响应 / 测试）。"""
    return LLMBudget(
        member_id=member_id,
        daily_token_budget=daily_token_budget,
        used=0,
        reset_at=_next_day_iso(),
        degraded=False,
    )


__all__ = [
    "LLMBudget",
    "LLMChatResult",
    "LLMProviderClient",
    "LocalLLMProvider",
    "default_budget",
]
