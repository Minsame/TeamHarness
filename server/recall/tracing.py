"""OpenTelemetry 风格 trace_id 透传工具。

项目当前未引入 opentelemetry-sdk 依赖（pyproject.toml 仅声明 fastapi/sqlalchemy 等
核心包），本模块用 contextvars 实现等价的 trace_id 全链路透传，保证 recall_log.trace_id
字段写入与日志关联。后续接入真实 OpenTelemetry 时，只需在本模块内替换实现，调用方
接口（get_trace_id / set_trace_id / new_trace_id）保持不变。

设计：
- _trace_id_ctx：contextvars 存储当前请求 trace_id
- new_trace_id()：生成 32 位十六进制 trace_id（与 OpenTelemetry TraceId 格式对齐）
- set_trace_id / get_trace_id：在当前上下文读写
- ensure_trace_id：若当前无 trace_id 则生成并设置，返回当前值
- parse_trace_id_from_headers：从 X-Trace-Id / X-Request-Id 头取上游透传值
"""

from __future__ import annotations

import contextvars
import secrets


# contextvars 保证异步 / 多任务上下文隔离
_trace_id_ctx: contextvars.ContextVar[str] = contextvars.ContextVar(
    "teamharness_trace_id", default=""
)

# OpenTelemetry TraceId 标准为 32 位十六进制；此处对齐
_TRACE_ID_BYTES = 16  # 16 bytes → 32 hex chars


def new_trace_id() -> str:
    """生成 32 位十六进制 trace_id（与 OpenTelemetry TraceId 格式对齐）。"""
    return secrets.token_hex(_TRACE_ID_BYTES)


def set_trace_id(trace_id: str) -> contextvars.Token:
    """在当前上下文设置 trace_id，返回 Token 用于恢复。"""
    return _trace_id_ctx.set(trace_id)


def get_trace_id() -> str:
    """读取当前上下文的 trace_id，未设置返回空串。"""
    return _trace_id_ctx.get()


def reset_trace_id(token: contextvars.Token) -> None:
    """恢复 trace_id 到 set 之前状态（用于 finally 块）。"""
    _trace_id_ctx.reset(token)


def ensure_trace_id() -> str:
    """若当前无 trace_id 则生成并设置，返回当前值。"""
    tid = _trace_id_ctx.get()
    if not tid:
        tid = new_trace_id()
        _trace_id_ctx.set(tid)
    return tid


def parse_trace_id_from_headers(headers: dict[str, str]) -> str:
    """从 HTTP 头取上游透传 trace_id。

    支持的头（按优先级）：
    1. X-Trace-Id（项目约定）
    2. X-Request-Id（通用网关约定）
    3. traceparent（W3C Trace Context，取 trace-id 段，格式：version-trace-id-parent-id-...）
    """
    for key in ("x-trace-id", "x-request-id"):
        val = headers.get(key)
        if val:
            return val.strip()[:64]  # 截断保护，避免恶意超长值
    traceparent = headers.get("traceparent")
    if traceparent:
        # W3C 格式：00-<trace-id>-<parent-id>-<trace-flags>
        parts = traceparent.split("-")
        if len(parts) >= 2 and parts[1]:
            return parts[1][:64]
    return ""


__all__ = [
    "ensure_trace_id",
    "get_trace_id",
    "new_trace_id",
    "parse_trace_id_from_headers",
    "reset_trace_id",
    "set_trace_id",
]
