"""schema_validator 测试（SubTask 7.10 + 重点风险 🔴 LLM 强制 JSON schema + 校验失败重试）。"""

from __future__ import annotations

from typing import Any

import pytest

from server.distill_personal.schema_validator import (
    DEFAULT_MAX_RETRIES,
    ChatWithSchemaResult,
    SchemaValidationResult,
    chat_with_schema,
    parse_llm_json,
    validate_against_schema,
)


# ---------------------------------------------------------------------------
# parse_llm_json
# ---------------------------------------------------------------------------


def test_parse_llm_json_plain() -> None:
    """普通 JSON 字符串解析。"""
    data = parse_llm_json('{"skip": false, "confidence": 0.8}')
    assert data == {"skip": False, "confidence": 0.8}


def test_parse_llm_json_with_markdown_fence() -> None:
    """带 ```json ... ``` 围栏的解析。"""
    content = '```json\n{"skip": true, "confidence": 0.5}\n```'
    data = parse_llm_json(content)
    assert data == {"skip": True, "confidence": 0.5}


def test_parse_llm_json_with_bare_fence() -> None:
    """带 ``` 围栏（无 json 标签）的解析。"""
    content = '```\n{"skip": false}\n```'
    data = parse_llm_json(content)
    assert data == {"skip": False}


def test_parse_llm_json_empty_string_raises() -> None:
    """空字符串应抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_llm_json("")


def test_parse_llm_json_invalid_json_raises() -> None:
    """非合法 JSON 应抛 ValueError。"""
    with pytest.raises(ValueError):
        parse_llm_json("not json at all")


# ---------------------------------------------------------------------------
# validate_against_schema
# ---------------------------------------------------------------------------


_SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "asset": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["title", "tags"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["skip", "asset", "confidence"],
}


def test_validate_against_schema_passes_valid_data() -> None:
    """合法数据校验通过。"""
    data = {
        "skip": False,
        "asset": {"title": "rule-1", "tags": ["lint"]},
        "confidence": 0.8,
    }
    result = validate_against_schema(data, _SIMPLE_SCHEMA)
    assert result.ok
    assert result.data == data


def test_validate_against_schema_missing_required() -> None:
    """缺必填字段校验失败。"""
    data = {"skip": False, "asset": {"title": "t", "tags": []}}  # 缺 confidence
    result = validate_against_schema(data, _SIMPLE_SCHEMA)
    assert not result.ok
    assert "confidence" in result.error


def test_validate_against_schema_wrong_type() -> None:
    """类型不符校验失败。"""
    data = {
        "skip": "false",  # 应为 boolean
        "asset": {"title": "t", "tags": []},
        "confidence": 0.5,
    }
    result = validate_against_schema(data, _SIMPLE_SCHEMA)
    assert not result.ok
    assert "boolean" in result.error or "类型" in result.error


def test_validate_against_schema_nested_array_items() -> None:
    """数组元素类型校验。"""
    data = {
        "skip": False,
        "asset": {"title": "t", "tags": [1, 2, 3]},  # tags 应为 string 数组
        "confidence": 0.5,
    }
    result = validate_against_schema(data, _SIMPLE_SCHEMA)
    assert not result.ok


def test_validate_against_schema_no_type_constraint_passes() -> None:
    """schema 无 type 约束时直接通过。"""
    result = validate_against_schema("anything", {})
    assert result.ok


# ---------------------------------------------------------------------------
# chat_with_schema — 重试逻辑
# ---------------------------------------------------------------------------


class _StubLLM:
    """按预设响应列表返回的 LLM stub。"""

    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = responses
        self.call_count = 0
        self.received_messages: list[list[dict[str, str]]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.received_messages.append(list(messages))
        idx = min(self.call_count, len(self.responses) - 1)
        resp = self.responses[idx]
        self.call_count += 1
        return dict(resp)


def test_chat_with_schema_success_first_try() -> None:
    """首次返回合规 JSON → 成功，attempts=1。"""
    llm = _StubLLM(
        responses=[
            {
                "content": '{"skip": false, "asset": {"title": "t", "tags": ["x"]}, "confidence": 0.9}',
                "usage": {"total_tokens": 100},
            }
        ]
    )
    result = chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
    )
    assert result.success
    assert result.attempts == 1
    assert result.data["skip"] is False
    assert result.total_usage.get("total_tokens") == 100


def test_chat_with_schema_retries_on_invalid_json_then_succeeds() -> None:
    """首次非 JSON，重试后成功。"""
    llm = _StubLLM(
        responses=[
            {"content": "not json", "usage": {"total_tokens": 10}},
            {
                "content": '{"skip": false, "asset": {"title": "t", "tags": []}, "confidence": 0.5}',
                "usage": {"total_tokens": 50},
            },
        ]
    )
    result = chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
        max_retries=3,
    )
    assert result.success
    assert result.attempts == 2
    # usage 应累计
    assert result.total_usage.get("total_tokens") == 60
    # 第 2 次调用 messages 应含重试 hint
    second_messages = llm.received_messages[1]
    assert any("上次" in m.get("content", "") or "schema" in m.get("content", "")
               for m in second_messages if m["role"] == "user")


def test_chat_with_schema_retries_on_schema_violation() -> None:
    """首次 JSON 但 schema 不符，重试后成功。"""
    llm = _StubLLM(
        responses=[
            # 缺 confidence
            {"content": '{"skip": false, "asset": {"title": "t", "tags": []}}',
             "usage": {}},
            {"content": '{"skip": false, "asset": {"title": "t", "tags": []}, "confidence": 0.7}',
             "usage": {}},
        ]
    )
    result = chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
    )
    assert result.success
    assert result.attempts == 2


def test_chat_with_schema_exhausts_retries_returns_failure() -> None:
    """重试耗尽返回 success=False。"""
    llm = _StubLLM(
        responses=[
            {"content": "still not json", "usage": {}},
            {"content": "still not json 2", "usage": {}},
            {"content": "still not json 3", "usage": {}},
        ]
    )
    result = chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
        max_retries=3,
    )
    assert not result.success
    assert result.attempts == 3
    assert "JSON 解析失败" in result.last_error


def test_chat_with_schema_llm_exception_propagates() -> None:
    """LLM 调用本身抛异常（网络/鉴权），不重试，直接传播。"""

    class _ExplodingLLM:
        def chat(self, messages, *, schema=None, **kw):
            raise RuntimeError("network error")

    with pytest.raises(RuntimeError, match="network error"):
        chat_with_schema(
            _ExplodingLLM(),
            messages=[{"role": "user", "content": "hi"}],
            schema=_SIMPLE_SCHEMA,
        )


def test_chat_with_schema_max_retries_floor() -> None:
    """max_retries < 1 应被向上修正为 1。"""
    llm = _StubLLM(
        responses=[{"content": "invalid", "usage": {}}]
    )
    result = chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
        max_retries=0,
    )
    assert not result.success
    assert result.attempts == 1


def test_chat_with_schema_on_retry_callback_invoked() -> None:
    """on_retry 回调应被调用。"""
    llm = _StubLLM(
        responses=[
            {"content": "invalid", "usage": {}},
            {"content": "invalid 2", "usage": {}},
            {"content": "invalid 3", "usage": {}},
        ]
    )
    calls: list[tuple[int, str, str]] = []

    def on_retry(attempt: int, error: str, raw: str) -> None:
        calls.append((attempt, error, raw))

    chat_with_schema(
        llm,
        messages=[{"role": "user", "content": "hi"}],
        schema=_SIMPLE_SCHEMA,
        max_retries=3,
        on_retry=on_retry,
    )
    # 3 次都失败，每次失败都触发 on_retry
    assert len(calls) == 3
    assert calls[0][0] == 1
    assert calls[2][0] == 3


def test_chat_with_schema_does_not_mutate_original_messages() -> None:
    """重试 hint 应追加到副本，不修改原 messages。"""
    original = [{"role": "user", "content": "hi"}]
    llm = _StubLLM(
        responses=[
            {"content": "invalid", "usage": {}},
            {"content": '{"skip": false, "asset": {"title": "t", "tags": []}, "confidence": 0.5}',
             "usage": {}},
        ]
    )
    chat_with_schema(
        llm,
        messages=original,
        schema=_SIMPLE_SCHEMA,
        max_retries=3,
    )
    # 原 messages 不变
    assert original == [{"role": "user", "content": "hi"}]


def test_default_max_retries_is_3() -> None:
    """DEFAULT_MAX_RETRIES 应为 3（对齐协调卡片"重试≥3次须自查环"）。"""
    assert DEFAULT_MAX_RETRIES == 3


# ---------------------------------------------------------------------------
# ChatWithSchemaResult / SchemaValidationResult 数据结构
# ---------------------------------------------------------------------------


def test_chat_with_schema_result_defaults() -> None:
    """ChatWithSchemaResult 默认值。"""
    r = ChatWithSchemaResult(success=False)
    assert r.data is None
    assert r.attempts == 0
    assert r.last_error == ""
    assert r.total_usage == {}


def test_schema_validation_result_defaults() -> None:
    """SchemaValidationResult 默认值。"""
    r = SchemaValidationResult(ok=True)
    assert r.data is None
    assert r.error == ""
