"""Task 21 测试：CLI 子命令 ask-peer / peers / shadow-log。

覆盖：
- ask-peer 命令解析（--peer / --question 参数）、在线实时路径、离线影子路径、--in-reply-to 传递
- peers 命令返回 peer 列表、--verbose 不报错
- shadow-log 命令返回事件列表、--peer / --limit / --type 过滤、无记录时返回空列表

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录，
通过 StubTransport 注入控制在线/离线场景，避免真实网络调用。
"""

from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path

import pytest

from server.client.cli import ClientCLI
from server.client.config import ClientConfig
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - ``reachable_peers``：可达 peer 集合（控制 is_peer_reachable 返回值）
    - ``auto_answer``：非空时 fetch 自动为已 deliver 的消息生成回答
      （in_reply_to 匹配 event_id，payload={"answer": auto_answer}）
    - ``discovered_peers``：discover_peers 返回的 PeerInfo 列表（None 时从 reachable_peers 生成）
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        auto_answer: str = "",
        discovered_peers: list[PeerInfo] | None = None,
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.auto_answer = auto_answer
        self.discovered_peers = discovered_peers
        self.delivered_messages: list[tuple[str, list[Message]]] = []

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(success=True, delivered_count=len(messages))

    def fetch(self, peer_id: str, since_vector_clock: dict | None = None) -> list[Message]:
        if self.auto_answer:
            responses: list[Message] = []
            for delivered_peer, messages in self.delivered_messages:
                if delivered_peer != peer_id:
                    continue
                for msg in messages:
                    responses.append(
                        Message(
                            message_id=str(uuid.uuid4()),
                            event_id=str(uuid.uuid4()),
                            sender_id=peer_id,
                            recipient_id=msg.sender_id,
                            msg_type="answer",
                            payload={"answer": self.auto_answer},
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            in_reply_to=msg.event_id,
                        )
                    )
            return responses
        return []

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        if self.discovered_peers is not None:
            return list(self.discovered_peers)
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_cli(
    tmp_path: Path,
    *,
    transport: StubTransport,
    member_id: str = "alice",
) -> ClientCLI:
    """构造 ClientCLI 测试实例（注入 Stub transport，隔离 tmp_path）。"""
    config = ClientConfig(member_id=member_id, repo_root=str(tmp_path))
    return ClientCLI(config=config, transport=transport)


def _run_capture(cli: ClientCLI, argv: list[str]) -> tuple[int, dict]:
    """运行命令并捕获 stdout JSON 输出，返回 (exit_code, parsed_data)。"""
    buf = io.StringIO()
    cli.stdout = buf
    cli.stderr = io.StringIO()
    rc = cli.run(argv)
    output = buf.getvalue().strip()
    data = json.loads(output) if output else {}
    return rc, data


# ---------------------------------------------------------------------------
# TestCliAskPeer
# ---------------------------------------------------------------------------


class TestCliAskPeer:
    """ask-peer 子命令。"""

    def test_parse_peer_and_question_args(self, tmp_path: Path):
        """--peer / --question 参数正确解析，命令成功执行。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["ask-peer", "--peer", "bob", "--question", "hello?"])

        assert rc == 0
        assert data["success"] is True
        assert data["command"] == "ask-peer"
        assert data["data"]["peer_id"] == "bob"
        assert data["data"]["question"] == "hello?"

    def test_missing_peer_arg_exits_nonzero(self, tmp_path: Path):
        """缺少 --peer 参数时 argparse 报错，退出码非 0。"""
        transport = StubTransport(reachable_peers={"bob"})
        cli = _make_cli(tmp_path, transport=transport)

        rc = cli.run(["ask-peer", "--question", "hello?"])

        assert rc != 0

    def test_missing_question_arg_exits_nonzero(self, tmp_path: Path):
        """缺少 --question 参数时 argparse 报错，退出码非 0。"""
        transport = StubTransport(reachable_peers={"bob"})
        cli = _make_cli(tmp_path, transport=transport)

        rc = cli.run(["ask-peer", "--peer", "bob"])

        assert rc != 0

    def test_online_peer_returns_realtime_true(self, tmp_path: Path):
        """在线 peer 返回 realtime=true / degraded=false。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="答案是 42")
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["ask-peer", "--peer", "bob", "--question", "问题?"])

        assert rc == 0
        assert data["data"]["realtime"] is True
        assert data["data"]["degraded"] is False
        assert data["data"]["answer"] == "答案是 42"
        assert data["data"]["event_id"] != ""
        assert data["data"]["based_on"] == ""
        assert data["data"]["snapshot_stale"] is False

    def test_offline_peer_returns_degraded_true(self, tmp_path: Path):
        """离线 peer 返回 degraded=true / realtime=false（影子联络路径）。"""
        transport = StubTransport(reachable_peers=set())
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["ask-peer", "--peer", "bob", "--question", "问题?"])

        assert rc == 0
        assert data["data"]["degraded"] is True
        assert data["data"]["realtime"] is False
        assert data["data"]["peer_id"] == "bob"
        # 无快照时 based_on 为空、snapshot_stale 为 True
        assert data["data"]["snapshot_stale"] is True

    def test_in_reply_to_passed_to_transport(self, tmp_path: Path):
        """--in-reply-to 正确传递到 transport.deliver 的 Message。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        rc = cli.run([
            "ask-peer", "--peer", "bob", "--question", "follow up?",
            "--in-reply-to", "prev-event-id-123",
        ])
        assert rc == 0

        # 验证 transport.deliver 收到的 Message 携带正确的 in_reply_to
        assert len(transport.delivered_messages) == 1
        _, messages = transport.delivered_messages[0]
        assert len(messages) == 1
        assert messages[0].in_reply_to == "prev-event-id-123"

    def test_in_reply_to_recorded_in_conversation_log(self, tmp_path: Path):
        """--in-reply-to 写入 ConversationLog 中的 ask 事件。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        cli.run([
            "ask-peer", "--peer", "bob", "--question", "follow up?",
            "--in-reply-to", "prev-event-id-456",
        ])

        log_path = tmp_path / ".teamharness" / "async_comm" / "conversation.jsonl"
        assert log_path.is_file()
        ask_found = False
        with log_path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                event_data = json.loads(line)
                if event_data["event_type"] == "ask":
                    assert event_data["in_reply_to"] == "prev-event-id-456"
                    ask_found = True
                    break
        assert ask_found, "ConversationLog 中未找到 ask 事件"


# ---------------------------------------------------------------------------
# TestCliPeers
# ---------------------------------------------------------------------------


class TestCliPeers:
    """peers 子命令。"""

    def test_peers_returns_list(self, tmp_path: Path):
        """peers 命令返回 peer 列表与正确计数。"""
        transport = StubTransport(reachable_peers={"alice", "bob", "carol"})
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["peers"])

        assert rc == 0
        assert data["success"] is True
        assert data["data"]["count"] == 3
        assert set(data["data"]["peers"]) == {"alice", "bob", "carol"}

    def test_peers_empty_returns_empty_list(self, tmp_path: Path):
        """无 peer 时返回空列表与 count=0。"""
        transport = StubTransport(reachable_peers=set())
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["peers"])

        assert rc == 0
        assert data["data"]["count"] == 0
        assert data["data"]["peers"] == []

    def test_peers_verbose_no_error(self, tmp_path: Path):
        """--verbose 标志不报错，命令正常执行。"""
        transport = StubTransport(reachable_peers={"bob"})
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["peers", "--verbose"])

        assert rc == 0
        assert data["success"] is True
        assert data["data"]["count"] == 1

    def test_peers_verbose_short_flag(self, tmp_path: Path):
        """-v 短标志等价于 --verbose。"""
        transport = StubTransport(reachable_peers={"bob"})
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["peers", "-v"])

        assert rc == 0
        assert data["success"] is True


# ---------------------------------------------------------------------------
# TestCliShadowLog
# ---------------------------------------------------------------------------


class TestCliShadowLog:
    """shadow-log 子命令。"""

    def test_shadow_log_returns_events(self, tmp_path: Path):
        """shadow-log 返回事件列表（含 ask + realtime_answer）。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        # 先执行一次 ask-peer 生成事件
        cli.run(["ask-peer", "--peer", "bob", "--question", "test"])

        rc, data = _run_capture(cli, ["shadow-log"])

        assert rc == 0
        assert data["success"] is True
        assert data["data"]["count"] >= 2  # 至少 1 ask + 1 realtime_answer
        events = data["data"]["events"]
        # 每个事件应包含完整字段
        for ev in events:
            assert "event_id" in ev
            assert "event_type" in ev
            assert "peer_id" in ev
            assert "timestamp" in ev
            assert "degraded" in ev
            assert "realtime" in ev
            assert "based_on" in ev
            assert "snapshot_stale" in ev
            assert "payload" in ev

    def test_shadow_log_filter_by_peer(self, tmp_path: Path):
        """--peer 过滤仅返回指定 peer 的事件。"""
        transport = StubTransport(reachable_peers={"bob", "carol"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        # 为两个 peer 各生成事件
        cli.run(["ask-peer", "--peer", "bob", "--question", "q1"])
        cli.run(["ask-peer", "--peer", "carol", "--question", "q2"])

        rc, data = _run_capture(cli, ["shadow-log", "--peer", "bob"])

        assert rc == 0
        events = data["data"]["events"]
        assert len(events) > 0
        for ev in events:
            assert ev["peer_id"] == "bob"

    def test_shadow_log_limit(self, tmp_path: Path):
        """--limit 限制返回数量。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        # 生成 3 次 ask-peer，每次产生 ask + realtime_answer 共 2 条事件
        cli.run(["ask-peer", "--peer", "bob", "--question", "q1"])
        cli.run(["ask-peer", "--peer", "bob", "--question", "q2"])
        cli.run(["ask-peer", "--peer", "bob", "--question", "q3"])

        rc, data = _run_capture(cli, ["shadow-log", "--limit", "3"])

        assert rc == 0
        assert data["data"]["count"] == 3
        assert len(data["data"]["events"]) == 3

    def test_shadow_log_filter_by_type(self, tmp_path: Path):
        """--type 过滤仅返回指定类型的事件。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        cli.run(["ask-peer", "--peer", "bob", "--question", "q1"])

        rc, data = _run_capture(cli, ["shadow-log", "--type", "ask"])

        assert rc == 0
        events = data["data"]["events"]
        assert len(events) >= 1
        for ev in events:
            assert ev["event_type"] == "ask"

    def test_shadow_log_filter_by_type_realtime_answer(self, tmp_path: Path):
        """--type realtime_answer 过滤返回实时回答事件。"""
        transport = StubTransport(reachable_peers={"bob"}, auto_answer="42")
        cli = _make_cli(tmp_path, transport=transport)

        cli.run(["ask-peer", "--peer", "bob", "--question", "q1"])

        rc, data = _run_capture(cli, ["shadow-log", "--type", "realtime_answer"])

        assert rc == 0
        events = data["data"]["events"]
        assert len(events) >= 1
        for ev in events:
            assert ev["event_type"] == "realtime_answer"
            assert ev["realtime"] is True

    def test_shadow_log_empty_when_no_records(self, tmp_path: Path):
        """无记录时返回空列表与 count=0。"""
        transport = StubTransport(reachable_peers=set())
        cli = _make_cli(tmp_path, transport=transport)

        rc, data = _run_capture(cli, ["shadow-log"])

        assert rc == 0
        assert data["success"] is True
        assert data["data"]["count"] == 0
        assert data["data"]["events"] == []

    def test_shadow_log_offline_events_contain_degraded(self, tmp_path: Path):
        """离线路径产生的事件在 shadow-log 中标记 degraded=true。"""
        transport = StubTransport(reachable_peers=set())
        cli = _make_cli(tmp_path, transport=transport)

        cli.run(["ask-peer", "--peer", "bob", "--question", "offline q?"])

        rc, data = _run_capture(cli, ["shadow-log", "--type", "simulated_answer"])

        assert rc == 0
        events = data["data"]["events"]
        assert len(events) >= 1
        for ev in events:
            assert ev["event_type"] == "simulated_answer"
            assert ev["degraded"] is True
            assert ev["realtime"] is False
