"""LLM 强制 JSON schema 输出 + 校验失败重试。

对应 SubTask 7.10 + 重点风险 🔴：
- LLM 调用必须返回符合 schema 的 JSON
- 校验失败时重试（最多 max_retries 次，默认 3 次）
- 重试时把上次错误信息追加到 messages，让 LLM 自纠
- 重试耗尽仍失败 → 抛 SchemaValidationError 或降级返回 None（由调用方决定）

设计要点：
- 校验用 jsonschema 库（若不可用退化为手动关键字段校验，避免硬依赖）
- 重试不卡死循环：硬上限 max_retries（默认 3，对齐协调卡片"重试≥3次须自查环"）
- 每次重试记录日志（attempt N / max_retries），便于排查
- 不依赖具体 LLM Provider，接受任何符合 LLMChatProtocol 的对象
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

logger = logging.getLogger(__name__)

# 默认最大重试次数（对齐协调卡片"重试≥3次须自查环"阈值）
DEFAULT_MAX_RETRIES = 3


class LLMChatLike(Protocol):
    """LLM Provider 协议（与 binding/llm.py LLMChatProtocol 对齐）。"""

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
    ) -> dict[str, Any]:
        """返回 {"content": str, "usage": {...}}。"""
        ...


@dataclass
class SchemaValidationResult:
    """schema 校验结果。"""

    ok: bool
    data: Any = None
    error: str = ""

    # 校验失败时保留原始 content，便于重试时回显给 LLM
    raw_content: str = ""


@dataclass
class ChatWithSchemaResult:
    """带 schema 校验的 LLM 调用结果。"""

    success: bool
    data: Any = None
    attempts: int = 0
    last_error: str = ""
    total_usage: dict[str, Any] = field(default_factory=dict)


class SchemaValidationError(Exception):
    """schema 校验失败（重试耗尽）。"""


# ---------------------------------------------------------------------------
# JSON 解析（容错：剥离 markdown 围栏）
# ---------------------------------------------------------------------------


def parse_llm_json(content: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON，剥离 markdown 代码块围栏。

    与 binding/llm.py._parse_llm_json 对齐，保持一致行为。
    """
    text = (content or "").strip()
    # 剥离 ```json ... ``` 围栏
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    # 兜底：若仍以 ``` 开头但缺闭合，尝试剥离首行
    if text.startswith("```"):
        lines = text.splitlines()
        if len(lines) >= 2:
            text = "\n".join(lines[1:])
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回非合法 JSON: {exc}") from exc


# ---------------------------------------------------------------------------
# schema 校验
# ---------------------------------------------------------------------------


def _try_import_jsonschema() -> Any | None:
    """尝试导入 jsonschema，不可用时返回 None（退化为手动校验）。"""
    try:
        import jsonschema  # type: ignore[import-untyped]
        return jsonschema
    except ImportError:
        return None


_JSONSCHEMA_LIB = _try_import_jsonschema()


def validate_against_schema(data: Any, schema: dict[str, Any]) -> SchemaValidationResult:
    """校验 data 是否符合 schema。

    优先用 jsonschema 库；不可用时退化为手动关键字段校验
    （type / required / properties 顶层）。
    """
    if _JSONSCHEMA_LIB is not None:
        try:
            _JSONSCHEMA_LIB.validate(instance=data, schema=schema)
            return SchemaValidationResult(ok=True, data=data)
        except _JSONSCHEMA_LIB.ValidationError as exc:
            return SchemaValidationResult(ok=False, error=str(exc.message))
        except _JSONSCHEMA_LIB.SchemaError as exc:
            return SchemaValidationResult(ok=False, error=f"schema 本身非法: {exc}")
    # 退化手动校验
    return _manual_validate(data, schema)


def _manual_validate(data: Any, schema: dict[str, Any]) -> SchemaValidationResult:
    """手动关键字段校验（jsonschema 不可用时兜底）。

    覆盖：
    - type：object / array / string / number / boolean / null
    - required：必填字段
    - properties：递归一层（不展开 nested）
    缺点：不支持 anyOf/oneOf/allOf/$ref 等高级特性。
    """
    if not isinstance(schema, dict):
        return SchemaValidationResult(ok=True, data=data)
    expected_type = schema.get("type")
    type_map = {
        "object": dict,
        "array": list,
        "string": str,
        "number": (int, float),
        "integer": int,
        "boolean": bool,
        "null": type(None),
    }
    if expected_type:
        py_type = type_map.get(expected_type)
        if py_type is not None and not isinstance(data, py_type):
            return SchemaValidationResult(
                ok=False, error=f"类型不符：期望 {expected_type}，实际 {type(data).__name__}"
            )
    if expected_type == "object" and isinstance(data, dict):
        required = schema.get("required", [])
        for key in required:
            if key not in data:
                return SchemaValidationResult(
                    ok=False, error=f"缺少必填字段: {key}"
                )
        properties = schema.get("properties", {})
        for key, subschema in properties.items():
            if key in data:
                sub_result = _manual_validate(data[key], subschema)
                if not sub_result.ok:
                    return SchemaValidationResult(
                        ok=False, error=f"字段 {key}: {sub_result.error}"
                    )
    if expected_type == "array" and isinstance(data, list):
        items_schema = schema.get("items")
        if items_schema:
            for i, item in enumerate(data):
                sub_result = _manual_validate(item, items_schema)
                if not sub_result.ok:
                    return SchemaValidationResult(
                        ok=False, error=f"数组第 {i} 项: {sub_result.error}"
                    )
    return SchemaValidationResult(ok=True, data=data)


# ---------------------------------------------------------------------------
# 带重试的 LLM 调用
# ---------------------------------------------------------------------------


def chat_with_schema(
    llm: LLMChatLike,
    *,
    messages: list[dict[str, str]],
    schema: dict[str, Any],
    max_retries: int = DEFAULT_MAX_RETRIES,
    on_retry: Callable[[int, str, str], None] | None = None,
) -> ChatWithSchemaResult:
    """调用 LLM 并校验 schema，失败重试。

    流程：
    1. 调用 llm.chat(messages, schema=schema)
    2. 解析 content 为 JSON（剥离 markdown 围栏）
    3. 校验是否符合 schema
    4. 失败 → 追加"上次错误"提示到 messages，重试
    5. 成功 → 返回 ChatWithSchemaResult(success=True, data=...)
    6. 重试耗尽 → 返回 ChatWithSchemaResult(success=False, last_error=...)

    on_retry 回调签名：(attempt, error, raw_content) → None，用于日志/埋点。

    不抛异常（重试耗尽返回 success=False），由调用方决定降级策略。
    但若 LLM 调用本身抛异常（网络/鉴权），不重试直接传播
    （这类错误重试无意义，应让上层降级）。
    """
    if max_retries < 1:
        max_retries = 1
    current_messages = list(messages)
    last_error = ""
    total_usage: dict[str, Any] = {}
    raw_content = ""

    for attempt in range(1, max_retries + 1):
        try:
            resp = llm.chat(current_messages, schema=schema)
        except Exception as exc:  # noqa: BLE001
            # LLM 调用本身异常，不重试（网络/鉴权错误重试无意义）
            logger.warning("LLM 调用异常（attempt %d/%d）: %s", attempt, max_retries, exc)
            raise

        raw_content = str(resp.get("content", ""))
        # 累计 usage
        usage = resp.get("usage") or {}
        if isinstance(usage, dict):
            for k, v in usage.items():
                if isinstance(v, (int, float)):
                    total_usage[k] = total_usage.get(k, 0) + v

        # 解析 JSON
        try:
            data = parse_llm_json(raw_content)
        except ValueError as exc:
            last_error = f"JSON 解析失败: {exc}"
            logger.warning(
                "schema 校验 attempt %d/%d 失败: %s",
                attempt,
                max_retries,
                last_error,
            )
            if on_retry is not None:
                on_retry(attempt, last_error, raw_content)
            if attempt >= max_retries:
                break
            # 追加错误提示，让 LLM 自纠
            current_messages = _append_retry_hint(current_messages, last_error, raw_content)
            continue

        # schema 校验
        result = validate_against_schema(data, schema)
        if result.ok:
            return ChatWithSchemaResult(
                success=True,
                data=result.data,
                attempts=attempt,
                total_usage=total_usage,
            )
        last_error = f"schema 校验失败: {result.error}"
        logger.warning(
            "schema 校验 attempt %d/%d 失败: %s",
            attempt,
            max_retries,
            last_error,
        )
        if on_retry is not None:
            on_retry(attempt, last_error, raw_content)
        if attempt >= max_retries:
            break
        current_messages = _append_retry_hint(current_messages, last_error, raw_content)

    return ChatWithSchemaResult(
        success=False,
        attempts=max_retries,
        last_error=last_error,
        total_usage=total_usage,
    )


def _append_retry_hint(
    messages: list[dict[str, str]],
    error: str,
    raw_content: str,
) -> list[dict[str, str]]:
    """追加重试提示到 messages，让 LLM 看到上次错误自纠。

    不修改原 messages（返回新列表）。
    """
    new_messages = list(messages)
    # 把 LLM 上次的错误回答加入对话，再追加 user 提示
    new_messages.append({"role": "assistant", "content": raw_content})
    hint = (
        f"上次的回答不合规：{error}\n"
        "请严格按 schema 返回 JSON，不要包含任何额外文本或 markdown 围栏。"
    )
    new_messages.append({"role": "user", "content": hint})
    return new_messages


__all__ = [
    "ChatWithSchemaResult",
    "DEFAULT_MAX_RETRIES",
    "LLMChatLike",
    "SchemaValidationError",
    "SchemaValidationResult",
    "chat_with_schema",
    "parse_llm_json",
    "validate_against_schema",
]
