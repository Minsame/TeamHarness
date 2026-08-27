"""采纳率上报测试（SubTask 6.10 + 6.11）。

覆盖：
- AdoptionEvent 自动生成 event_id + timestamp
- 非法事件类型 raise
- record / record_recall / record_view 等便捷方法
- 本地缓存（JSONL append-only）
- load_pending_events
- pending_count
- flush 在线成功路径（mock httpx）
- flush 离线跳过 + 保留本地
- flush 失败保留 + 重试
- _rewrite_events_log（删除已 ack 事件）
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.client.adoption import (
    AdoptionEvent,
    AdoptionReporter,
    DEFAULT_FLUSH_BATCH_SIZE,
    EVENT_TYPES,
    FlushResult,
)
from server.client.config import ClientConfig
from server.client.placeholders import MetricsBatchAck


# ---------------------------------------------------------------------------
# AdoptionEvent
# ---------------------------------------------------------------------------


def test_event_auto_generates_id_and_timestamp():
    e = AdoptionEvent(event_type="recall", asset_id="rule-1")
    assert e.event_id  # 自动生成 uuid
    assert e.timestamp  # 自动生成 ISO 时间
    assert e.event_type == "recall"


def test_event_invalid_type_raises():
    with pytest.raises(ValueError, match="非法事件类型"):
        AdoptionEvent(event_type="invalid", asset_id="x")


def test_event_types_constant():
    assert "recall" in EVENT_TYPES
    assert "view" in EVENT_TYPES
    assert "adopt" in EVENT_TYPES
    assert "modify" in EVENT_TYPES
    assert "reject" in EVENT_TYPES


def test_event_to_dict_round_trip():
    e = AdoptionEvent(
        event_type="adopt",
        asset_id="rule-1",
        agent_id="agent-1",
        member_id="alice",
        module_path="modules/backend",
        metadata={"source": "cli"},
    )
    data = e.to_dict()
    restored = AdoptionEvent.from_dict(data)
    assert restored.event_id == e.event_id
    assert restored.event_type == "adopt"
    assert restored.module_path == "modules/backend"
    assert restored.metadata == {"source": "cli"}


def test_event_from_dict_tolerates_missing_fields():
    e = AdoptionEvent.from_dict({"event_type": "recall"})
    assert e.event_type == "recall"
    assert e.asset_id == ""
    # event_id 在 from_dict 时不会重新生成（保留原值或为空）
    # 但构造 AdoptionEvent 走 __post_init__ 时会补；from_dict 直接赋值
    # 这里仅断言不报错


# ---------------------------------------------------------------------------
# AdoptionReporter record
# ---------------------------------------------------------------------------


@pytest.fixture
def reporter(tmp_path: Path) -> AdoptionReporter:
    cfg = ClientConfig(repo_root=str(tmp_path), agent_id="agent-1", member_id="alice")
    return AdoptionReporter(cfg)


def test_record_writes_jsonl(reporter: AdoptionReporter):
    eid = reporter.record_recall(asset_id="rule-1", module_path="modules/backend")
    assert eid
    # 文件存在且为 JSONL
    assert reporter.events_log_path.is_file()
    line = reporter.events_log_path.read_text(encoding="utf-8").strip()
    data = json.loads(line)
    assert data["event_id"] == eid
    assert data["event_type"] == "recall"
    assert data["asset_id"] == "rule-1"
    assert data["module_path"] == "modules/backend"


def test_record_appends_multiple(reporter: AdoptionReporter):
    reporter.record_recall(asset_id="rule-1")
    reporter.record_view(asset_id="rule-2")
    reporter.record_adopt(asset_id="rule-3")
    events = reporter.load_pending_events()
    assert len(events) == 3
    assert events[0].event_type == "recall"
    assert events[1].event_type == "view"
    assert events[2].event_type == "adopt"


def test_record_modify_includes_hashes(reporter: AdoptionReporter):
    eid = reporter.record_modify(
        asset_id="rule-1", old_hash="sha256:old", new_hash="sha256:new"
    )
    events = reporter.load_pending_events()
    assert len(events) == 1
    assert events[0].metadata["old_hash"] == "sha256:old"
    assert events[0].metadata["new_hash"] == "sha256:new"


def test_record_reject(reporter: AdoptionReporter):
    reporter.record_reject(asset_id="rule-x")
    events = reporter.load_pending_events()
    assert events[0].event_type == "reject"


# ---------------------------------------------------------------------------
# pending_count / load_pending_events
# ---------------------------------------------------------------------------


def test_pending_count_empty(reporter: AdoptionReporter):
    assert reporter.pending_count() == 0


def test_pending_count_after_records(reporter: AdoptionReporter):
    for i in range(5):
        reporter.record_recall(asset_id=f"rule-{i}")
    assert reporter.pending_count() == 5


def test_load_pending_events_limit(reporter: AdoptionReporter):
    for i in range(10):
        reporter.record_recall(asset_id=f"rule-{i}")
    events = reporter.load_pending_events(limit=3)
    assert len(events) == 3


def test_load_pending_events_skips_invalid_lines(reporter: AdoptionReporter):
    # 手动写入无效行
    reporter.events_log_path.parent.mkdir(parents=True, exist_ok=True)
    with reporter.events_log_path.open("w", encoding="utf-8") as f:
        f.write("invalid json\n")
        f.write(json.dumps({"event_type": "recall", "asset_id": "x", "event_id": "id1"}) + "\n")
    events = reporter.load_pending_events()
    assert len(events) == 1
    assert events[0].asset_id == "x"


# ---------------------------------------------------------------------------
# flush - 在线成功
# ---------------------------------------------------------------------------


class MockTransport(httpx.BaseTransport):
    """httpx mock transport，按预设响应返回。

    实现 httpx.BaseTransport.handle_request 接口（而非 __call__），
    以兼容 httpx.Client(transport=...) 的要求。
    """

    def __init__(self, responses: list[dict[str, Any]] | dict[str, Any] | Exception):
        self.responses = responses
        self.calls: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        if isinstance(self.responses, Exception):
            raise self.responses
        if isinstance(self.responses, list):
            resp = self.responses.pop(0) if self.responses else {"accepted": 0, "rejected": 0}
        else:
            resp = self.responses
        # 列表中可能混入 Exception（模拟间歇性失败）
        if isinstance(resp, Exception):
            raise resp
        return httpx.Response(
            status_code=resp.get("status_code", 200),
            json=resp.get(
                "body",
                {"accepted": resp.get("accepted", 0), "rejected": resp.get("rejected", 0)},
            ),
        )


def make_reporter_with_http(
    tmp_path: Path,
    transport: httpx.BaseTransport,
    *,
    server_url: str = "https://th.example.com",
    api_key: str = "sk-test",
) -> AdoptionReporter:
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url=server_url,
        api_key=api_key,
        agent_id="agent-1",
        member_id="alice",
    )
    http_client = httpx.Client(transport=transport)
    return AdoptionReporter(cfg, http_client=http_client)


def test_flush_online_success(tmp_path: Path):
    transport = MockTransport({"accepted": 3, "rejected": 0})
    reporter = make_reporter_with_http(tmp_path, transport)
    for i in range(3):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    assert result.flushed == 3
    assert result.rejected == 0
    assert result.retained == 0  # 全部 ack，本地清空
    assert result.ok is True
    # 验证本地缓存已清空
    assert reporter.pending_count() == 0


def test_flush_online_partial_rejected(tmp_path: Path):
    transport = MockTransport({"accepted": 2, "rejected": 1})
    reporter = make_reporter_with_http(tmp_path, transport)
    for i in range(3):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    # 服务端返回 accepted=2 rejected=1，但客户端策略是整批 ack 后删除（不区分单条）
    # 实际：accepted + rejected = batch size 即视为整批成功
    # 注意：当前实现是 _rewrite_events_log 排除所有 batch 内 event_id
    assert result.flushed == 2
    assert result.rejected == 1


def test_flush_offline_skips_and_retains(reporter: AdoptionReporter):
    for i in range(3):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=False)
    assert result.flushed == 0
    assert result.retained == 3
    assert result.error == "offline mode"
    # 本地缓存仍保留
    assert reporter.pending_count() == 3


def test_flush_no_events_returns_empty(reporter: AdoptionReporter):
    result = reporter.flush(online=True)
    assert result.flushed == 0
    assert result.retained == 0
    assert result.ok is True


def test_flush_http_error_retains_local(tmp_path: Path):
    """HTTP 异常 → 事件保留本地，下次重试。"""
    transport = MockTransport(httpx.ConnectError("connection refused"))
    reporter = make_reporter_with_http(tmp_path, transport)
    for i in range(2):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    assert result.flushed == 0
    assert result.retained == 2
    assert result.error is not None
    # 本地保留
    assert reporter.pending_count() == 2


def test_flush_server_error_retains_local(tmp_path: Path):
    """服务端 500 错误 → 整批保留。"""
    transport = MockTransport({"status_code": 500, "body": {"error": "internal"}})
    reporter = make_reporter_with_http(tmp_path, transport)
    for i in range(2):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    assert result.flushed == 0
    assert result.error is not None
    # 本地保留
    assert reporter.pending_count() == 2


def test_flush_batch_size_split(tmp_path: Path):
    """事件数超过 flush_batch_size 时分批上传。"""
    # 5 条事件，batch_size=2 → 3 批
    transport = MockTransport([
        {"accepted": 2, "rejected": 0},
        {"accepted": 2, "rejected": 0},
        {"accepted": 1, "rejected": 0},
    ])
    reporter = make_reporter_with_http(tmp_path, transport)
    reporter.flush_batch_size = 2
    for i in range(5):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    assert result.flushed == 5
    assert reporter.pending_count() == 0
    # 验证调用次数 = 3 次（5/2=3 批）
    assert len(transport.calls) == 3


def test_flush_batch_error_stops_subsequent(tmp_path: Path):
    """第一批失败 → 停止后续 batch（避免雪崩），但后续事件保留。"""
    transport = MockTransport([
        {"status_code": 500, "body": {"error": "fail"}},
    ])
    reporter = make_reporter_with_http(tmp_path, transport)
    reporter.flush_batch_size = 2
    for i in range(5):
        reporter.record_recall(asset_id=f"rule-{i}")
    result = reporter.flush(online=True)
    assert result.flushed == 0
    # 仅调用 1 次（第一批失败后停止）
    assert len(transport.calls) == 1
    # 本地保留全部
    assert reporter.pending_count() == 5


def test_flush_retry_after_failure(tmp_path: Path):
    """第一次失败后，第二次成功 → 本地清空。"""
    transport = MockTransport([
        httpx.ConnectError("first fail"),
        {"accepted": 2, "rejected": 0},
    ])
    reporter = make_reporter_with_http(tmp_path, transport)
    for i in range(2):
        reporter.record_recall(asset_id=f"rule-{i}")
    # 第一次 flush 失败
    r1 = reporter.flush(online=True)
    assert r1.flushed == 0
    assert r1.retained == 2
    # 第二次 flush 成功
    r2 = reporter.flush(online=True)
    assert r2.flushed == 2
    assert r2.retained == 0


# ---------------------------------------------------------------------------
# 路径
# ---------------------------------------------------------------------------


def test_cache_path_under_teamharness_dir(reporter: AdoptionReporter, tmp_path: Path):
    assert reporter.events_log_path == tmp_path / ".teamharness" / "adoption-events.jsonl"
    assert reporter.state_path == tmp_path / ".teamharness" / "adoption-state.json"


def test_record_creates_parent_dir(reporter: AdoptionReporter):
    # 删除 .teamharness 目录
    import shutil
    if reporter.cache_dir.exists():
        shutil.rmtree(reporter.cache_dir)
    reporter.record_recall(asset_id="x")
    assert reporter.events_log_path.is_file()


# ---------------------------------------------------------------------------
# 默认参数
# ---------------------------------------------------------------------------


def test_default_flush_batch_size():
    assert DEFAULT_FLUSH_BATCH_SIZE == 100
