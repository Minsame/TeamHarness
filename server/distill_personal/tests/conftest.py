"""distill_personal 域测试共享 fixture。"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

import pytest

from server.distill_personal.session_provider import Session, SessionTurn


@pytest.fixture
def fake_trae_sessions_dir(tmp_path: Path) -> Path:
    """构造一个含若干 *.jsonl 会话的目录（模拟 Trae sessions 目录）。

    会话内容格式：每行一条 JSON 消息，含 role / content / timestamp。
    使用 os.utime 显式设置不同的 mtime，避免 Windows 文件系统精度问题。
    """
    sessions_dir = tmp_path / "sessions"
    sessions_dir.mkdir()

    # 会话 1：含规则信号（"必须" / "禁止"）
    s1 = sessions_dir / "session-001.jsonl"
    s1_lines = [
        {"role": "user", "content": "你好，帮我看看这个项目", "timestamp": "2026-08-07T10:00:00Z"},
        {"role": "assistant", "content": "好的，请问有什么问题？", "timestamp": "2026-08-07T10:00:05Z"},
        {"role": "user", "content": "提交前必须跑 lint，禁止跳过", "timestamp": "2026-08-07T10:00:30Z"},
        {"role": "assistant", "content": "明白，已记录规则", "timestamp": "2026-08-07T10:00:35Z"},
    ]
    s1.write_text(
        "\n".join(json.dumps(line) for line in s1_lines) + "\n",
        encoding="utf-8",
    )

    # 会话 2：含 memory 信号（"决定" / "踩坑"）
    s2 = sessions_dir / "session-002.jsonl"
    s2_lines = [
        {"role": "user", "content": "我们决定用 SQLAlchemy 作为 ORM", "timestamp": "2026-08-07T11:00:00Z"},
        {"role": "assistant", "content": "好的", "timestamp": "2026-08-07T11:00:05Z"},
        {"role": "user", "content": "踩坑：路径含空格会导致 build 失败", "timestamp": "2026-08-07T11:00:30Z"},
    ]
    s2.write_text(
        "\n".join(json.dumps(line) for line in s2_lines) + "\n",
        encoding="utf-8",
    )

    # 会话 3：纯闲聊（应被 Light 过滤）
    s3 = sessions_dir / "session-003.jsonl"
    s3_lines = [
        {"role": "user", "content": "你好", "timestamp": "2026-08-07T12:00:00Z"},
        {"role": "assistant", "content": "hi", "timestamp": "2026-08-07T12:00:05Z"},
    ]
    s3.write_text(
        "\n".join(json.dumps(line) for line in s3_lines) + "\n",
        encoding="utf-8",
    )

    # 显式设置不同的 mtime（避免 Windows 文件系统 mtime 精度问题导致测试不稳定）
    base = time.time() - 100
    os.utime(s1, (base, base))
    os.utime(s2, (base + 10, base + 10))
    os.utime(s3, (base + 20, base + 20))

    return sessions_dir


@pytest.fixture
def sample_sessions(fake_trae_sessions_dir: Path) -> list[Session]:
    """从 fake_trae_sessions_dir 加载全部会话为 Session 列表。"""
    from server.distill_personal.session_provider import GenericJsonlSessionProvider

    provider = GenericJsonlSessionProvider(fake_trae_sessions_dir)
    metas = provider.list_sessions()
    return [provider.read_session(m.session_id) for m in metas]


class FakeLLM:
    """假 LLM，按预设响应返回。

    使用：
        llm = FakeLLM(responses=[{"content": "...", "usage": {...}}])
        llm.chat(messages, schema=schema)  # 返回 responses[call_count]
    """

    def __init__(
        self,
        *,
        responses: list[dict[str, Any]] | None = None,
        always_return: dict[str, Any] | None = None,
        raise_on_call: Exception | None = None,
    ) -> None:
        self.responses = responses or []
        self.always_return = always_return
        self.raise_on_call = raise_on_call
        self.call_count = 0
        self.calls: list[dict[str, Any]] = []

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        self.calls.append({"messages": messages, "schema": schema, "kwargs": kwargs})
        self.call_count += 1
        if self.raise_on_call is not None:
            raise self.raise_on_call
        if self.always_return is not None:
            return dict(self.always_return)
        if not self.responses:
            return {"content": "{}", "usage": {"total_tokens": 10}}
        idx = min(self.call_count - 1, len(self.responses) - 1)
        return dict(self.responses[idx])


@pytest.fixture
def fake_llm() -> FakeLLM:
    """返回 FakeLLM 工厂。测试用 always_return 或 responses 自定义。"""
    return FakeLLM()
