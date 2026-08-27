"""Task 20 / Task 26 测试：MCP Server。

覆盖：
- TransportBridge：ask_peer（含 tag 路由）/ list_peers / share_asset /
  search_team_assets / get_conversation_log / resume_conversation
  （使用真实 TransportBridge + StubTransport）
- TOOL_DEFINITIONS：5 个工具定义存在，每个含 name / description / inputSchema，
  ask_peer 的 required 仅含 question（peer_id 与 tag 二选一），新增 resume_conversation
- execute_tool：5 个工具调用 + tag 路由 + 缺参错误返回 + 未知工具返回 error（使用 StubBridge）
- McpServer：list_tools 返回 5 个工具，call_tool 执行工具，未知工具返回 error

测试隔离：用 tmp_path fixture 为每个用例提供独立临时目录。
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from server.client.config import ClientConfig
from server.mcp_server.server import MCP_AVAILABLE, McpServer
from server.mcp_server.tools import TOOL_DEFINITIONS, execute_tool
from server.mcp_server.transport_bridge import TransportBridge
from server.transport.types import Message, PeerInfo, SyncResult


# ---------------------------------------------------------------------------
# Stub 类
# ---------------------------------------------------------------------------


class StubTransport:
    """Stub 实现 SyncTransport，可控的测试传输层。

    扩展点：
    - reachable_peers：可达 peer 集合
    - fetch_responses：按 peer_id 预置 fetch 返回消息
    - auto_answer：非空时 fetch 自动为已 deliver 的消息生成回答
      （in_reply_to 匹配 event_id，payload={"answer": auto_answer}）
    """

    def __init__(
        self,
        *,
        reachable_peers: set[str] | None = None,
        fetch_responses: dict[str, list[Message]] | None = None,
        auto_answer: str = "",
    ) -> None:
        self.reachable_peers = reachable_peers or set()
        self.fetch_responses = fetch_responses or {}
        self.delivered_messages: list[tuple[str, list[Message]]] = []
        self.auto_answer = auto_answer

    def deliver(self, peer_id: str, messages: list[Message]) -> SyncResult:
        self.delivered_messages.append((peer_id, messages))
        return SyncResult(
            success=True,
            delivered_count=len(messages),
            delivered_message_ids=[m.message_id for m in messages],
        )

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
        return self.fetch_responses.get(peer_id, [])

    def is_peer_reachable(self, peer_id: str) -> bool:
        return peer_id in self.reachable_peers

    def discover_peers(self) -> list[PeerInfo]:
        return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]


class StubBridge:
    """Stub TransportBridge，用于测试 execute_tool 和 McpServer。

    所有方法返回固定结果，不依赖文件系统或网络。
    """

    def __init__(
        self,
        *,
        ask_peer_result: dict[str, Any] | None = None,
        list_peers_result: list[dict[str, Any]] | None = None,
        share_asset_result: dict[str, Any] | None = None,
        search_result: list[dict[str, Any]] | None = None,
        resume_conversation_result: dict[str, Any] | None = None,
    ) -> None:
        self.ask_peer_result = ask_peer_result or {
            "event_id": "evt-1",
            "event_type": "realtime_answer",
            "peer_id": "bob",
            "answer": "42",
            "degraded": False,
            "realtime": True,
        }
        self.list_peers_result = list_peers_result or [
            {"peer_id": "bob", "online": True, "endpoint": "host:port"},
        ]
        self.share_asset_result = share_asset_result or {
            "success": True,
            "delivered_count": 1,
            "pending_count": 0,
        }
        self.search_result = search_result or [
            {"asset_id": "a1", "title": "asset-1", "relevance_score": 0.9},
        ]
        self.resume_conversation_result = resume_conversation_result or {
            "peer_id": "bob",
            "resumed": True,
            "events": [],
            "event_count": 0,
        }
        self.call_log: list[tuple[str, dict[str, Any]]] = []

    def ask_peer(
        self,
        peer_id: str,
        question: str,
        *,
        tag: str = "",
        in_reply_to: str = "",
    ) -> dict[str, Any]:
        self.call_log.append(
            ("ask_peer", {
                "peer_id": peer_id,
                "question": question,
                "tag": tag,
                "in_reply_to": in_reply_to,
            })
        )
        result = dict(self.ask_peer_result)
        # tag 路由时在结果中携带 tag 字段，模拟 TransportBridge 行为
        if tag and not peer_id:
            result["tag"] = tag
        return result

    def list_peers(self) -> list[dict[str, Any]]:
        self.call_log.append(("list_peers", {}))
        return list(self.list_peers_result)

    def share_asset(
        self,
        asset_id: str,
        to_peer_id: str,
        content: dict | None = None,
    ) -> dict[str, Any]:
        self.call_log.append(
            ("share_asset", {"asset_id": asset_id, "to_peer_id": to_peer_id, "content": content})
        )
        return dict(self.share_asset_result)

    def search_team_assets(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        self.call_log.append(("search_team_assets", {"query": query, "limit": limit}))
        return list(self.search_result)

    def resume_conversation(self, peer_id: str) -> dict[str, Any]:
        self.call_log.append(("resume_conversation", {"peer_id": peer_id}))
        result = dict(self.resume_conversation_result)
        result["peer_id"] = peer_id
        return result


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_config(tmp_path: Path, **overrides: Any) -> ClientConfig:
    """创建测试用 ClientConfig，repo_root 指向 tmp_path。"""
    defaults: dict[str, Any] = dict(
        repo_root=str(tmp_path),
        member_id="alice",
        topology="central",
        server_url="",
        api_key="",
        agent_id="agent-alice",
    )
    defaults.update(overrides)
    return ClientConfig(**defaults)


def _make_bridge(
    tmp_path: Path,
    *,
    transport: StubTransport | None = None,
) -> TransportBridge:
    """构造 TransportBridge 测试实例，注入 StubTransport。"""
    config = _make_config(tmp_path)
    transport = transport or StubTransport(
        reachable_peers={"bob"}, auto_answer="答案是 42"
    )
    return TransportBridge(config, transport=transport)


# ---------------------------------------------------------------------------
# TestTransportBridge
# ---------------------------------------------------------------------------


class TestTransportBridge:
    """TransportBridge 桥接层测试（真实 TransportBridge + StubTransport）。"""

    def test_ask_peer_returns_dict_with_answer(self, tmp_path: Path):
        """ask_peer 返回 dict，含回答内容。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.ask_peer("bob", "如何处理 X？")

        assert isinstance(result, dict)
        assert "answer" in result
        assert result["answer"] == "答案是 42"
        assert result["peer_id"] == "bob"
        assert result["degraded"] is False
        assert result["realtime"] is True
        assert "event_id" in result
        assert "timestamp" in result

    def test_ask_peer_offline_returns_degraded(self, tmp_path: Path):
        """peer 不可达时 ask_peer 走影子路径，返回 degraded=True。"""
        transport = StubTransport(reachable_peers=set())
        bridge = _make_bridge(tmp_path, transport=transport)

        result = bridge.ask_peer("bob", "问题？")

        assert isinstance(result, dict)
        assert result["degraded"] is True
        assert result["realtime"] is False
        assert "answer" in result

    def test_list_peers_returns_list(self, tmp_path: Path):
        """list_peers 返回列表。"""
        transport = StubTransport(reachable_peers={"bob", "carol"})
        bridge = _make_bridge(tmp_path, transport=transport)

        result = bridge.list_peers()

        assert isinstance(result, list)
        assert len(result) == 2
        peer_ids = {p["peer_id"] for p in result}
        assert peer_ids == {"bob", "carol"}
        for peer in result:
            assert "peer_id" in peer
            assert "online" in peer

    def test_list_peers_empty(self, tmp_path: Path):
        """无 peer 时返回空列表。"""
        transport = StubTransport(reachable_peers=set())
        bridge = _make_bridge(tmp_path, transport=transport)

        result = bridge.list_peers()

        assert isinstance(result, list)
        assert result == []

    def test_share_asset_returns_dict(self, tmp_path: Path):
        """share_asset 返回 dict 含投递结果。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.share_asset("asset-1", "bob", content={"key": "value"})

        assert isinstance(result, dict)
        assert "success" in result
        assert result["success"] is True
        assert "delivered_count" in result
        assert "pending_count" in result

    def test_share_asset_offline_returns_pending(self, tmp_path: Path):
        """peer 不可达时 share_asset 返回 pending_count=1。"""
        transport = StubTransport(reachable_peers=set())
        bridge = _make_bridge(tmp_path, transport=transport)

        result = bridge.share_asset("asset-1", "bob")

        assert isinstance(result, dict)
        assert result["success"] is False
        assert result["pending_count"] == 1

    def test_search_team_assets_returns_list(self, tmp_path: Path):
        """search_team_assets 返回列表（离线降级为本地匹配）。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.search_team_assets("lint", limit=5)

        assert isinstance(result, list)

    def test_get_conversation_log_returns_list(self, tmp_path: Path):
        """get_conversation_log 返回列表。"""
        bridge = _make_bridge(tmp_path)
        # 先产生一些交流事件
        bridge.ask_peer("bob", "问题？")

        result = bridge.get_conversation_log()

        assert isinstance(result, list)
        # 至少有 ask 事件和 answer 事件
        assert len(result) >= 2

    def test_get_conversation_log_by_peer(self, tmp_path: Path):
        """get_conversation_log 按 peer_id 过滤。"""
        bridge = _make_bridge(tmp_path)
        bridge.ask_peer("bob", "问题？")

        result = bridge.get_conversation_log(peer_id="bob")

        assert isinstance(result, list)
        assert len(result) >= 1
        for event in result:
            assert event["peer_id"] == "bob"

    def test_get_conversation_log_empty(self, tmp_path: Path):
        """无交流记录时返回空列表。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.get_conversation_log()

        assert isinstance(result, list)
        assert result == []

    def test_ask_peer_with_tag_routes_by_tag(self, tmp_path: Path):
        """ask_peer 传 tag（peer_id 为空）时按 tag 路由，结果携带 tag 字段（Task 26）。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.ask_peer("", "问题？", tag="运维")

        assert isinstance(result, dict)
        # tag 路由时结果含 tag 字段
        assert result.get("tag") == "运维"

    def test_ask_peer_with_peer_id_ignores_tag(self, tmp_path: Path):
        """ask_peer 同时提供 peer_id 和 tag 时，peer_id 优先，结果不含 tag 字段。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.ask_peer("bob", "问题？", tag="运维")

        assert isinstance(result, dict)
        # peer_id 优先，结果不应携带 tag 字段
        assert "tag" not in result
        assert result["peer_id"] == "bob"

    def test_ask_peer_with_in_reply_to_passthrough(self, tmp_path: Path):
        """ask_peer 的 in_reply_to 透传到 peer_comm（Task 26）。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.ask_peer("bob", "继续", in_reply_to="evt-abc")

        assert isinstance(result, dict)
        # in_reply_to 应在返回结果中体现（关联到 ask 事件 ID）
        # TransportBridge 返回的 in_reply_to 是 answer 事件的 in_reply_to（即 ask 事件 ID）
        assert "in_reply_to" in result

    def test_resume_conversation_returns_dict(self, tmp_path: Path):
        """resume_conversation 返回 dict 含 peer_id / resumed / events / event_count（Task 26）。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.resume_conversation("bob")

        assert isinstance(result, dict)
        assert result["peer_id"] == "bob"
        assert "resumed" in result
        assert isinstance(result["resumed"], bool)
        assert "events" in result
        assert isinstance(result["events"], list)
        assert "event_count" in result
        assert result["event_count"] == len(result["events"])

    def test_resume_conversation_empty_when_no_paused(self, tmp_path: Path):
        """无暂停对话时 resume_conversation 返回 resumed=False 与空事件列表。"""
        bridge = _make_bridge(tmp_path)

        result = bridge.resume_conversation("bob")

        assert result["resumed"] is False
        assert result["event_count"] == 0
        assert result["events"] == []


# ---------------------------------------------------------------------------
# TestToolDefinitions
# ---------------------------------------------------------------------------


class TestToolDefinitions:
    """工具定义测试。"""

    def test_five_tools_defined(self):
        """5 个工具定义存在。"""
        assert len(TOOL_DEFINITIONS) == 5

    def test_each_tool_has_required_fields(self):
        """每个工具有 name / description / inputSchema。"""
        for tool in TOOL_DEFINITIONS:
            assert "name" in tool, f"工具缺少 name: {tool}"
            assert "description" in tool, f"工具缺少 description: {tool}"
            assert "inputSchema" in tool, f"工具缺少 inputSchema: {tool}"
            assert isinstance(tool["inputSchema"], dict)
            assert tool["inputSchema"].get("type") == "object"

    def test_tool_names(self):
        """工具名称正确。"""
        names = {tool["name"] for tool in TOOL_DEFINITIONS}
        assert names == {
            "ask_peer",
            "list_peers",
            "search_team_assets",
            "share_asset",
            "resume_conversation",
        }

    def test_ask_peer_required_only_includes_question(self):
        """ask_peer 的 required 仅含 question（peer_id 与 tag 二选一，均非必填）。"""
        ask_peer = next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_peer")
        required = ask_peer["inputSchema"].get("required", [])
        assert "question" in required
        # peer_id 不在 required 中（与 tag 二选一）
        assert "peer_id" not in required
        assert "tag" not in required

    def test_ask_peer_schema_supports_tag(self):
        """ask_peer 的 schema 含 tag 参数。"""
        ask_peer = next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_peer")
        properties = ask_peer["inputSchema"].get("properties", {})
        assert "tag" in properties
        assert properties["tag"]["type"] == "string"

    def test_ask_peer_schema_supports_in_reply_to(self):
        """ask_peer 的 schema 含 in_reply_to 参数。"""
        ask_peer = next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_peer")
        properties = ask_peer["inputSchema"].get("properties", {})
        assert "in_reply_to" in properties
        assert properties["in_reply_to"]["type"] == "string"

    def test_ask_peer_description_mentions_usage_timing(self):
        """ask_peer 描述包含使用时机说明（Task 26 SubTask 26.1）。"""
        ask_peer = next(t for t in TOOL_DEFINITIONS if t["name"] == "ask_peer")
        description = ask_peer["description"]
        # 描述应包含使用时机关键词
        assert "当你需要" in description or "向其他成员" in description
        # 描述应说明 peer_id / tag 二选一路由
        assert "peer_id" in description
        assert "tag" in description

    def test_resume_conversation_tool_definition(self):
        """resume_conversation 工具定义存在且 schema 含必填 peer_id（Task 26 SubTask 26.3）。"""
        resume = next(
            (t for t in TOOL_DEFINITIONS if t["name"] == "resume_conversation"),
            None,
        )
        assert resume is not None, "缺少 resume_conversation 工具定义"
        assert resume["description"], "resume_conversation 描述为空"
        # 描述应包含"恢复"语义
        assert "恢复" in resume["description"]
        properties = resume["inputSchema"].get("properties", {})
        assert "peer_id" in properties
        required = resume["inputSchema"].get("required", [])
        assert "peer_id" in required

    def test_share_asset_required_includes_asset_id_and_to_peer_id(self):
        """share_asset 的 required 包含 asset_id 和 to_peer_id。"""
        share = next(t for t in TOOL_DEFINITIONS if t["name"] == "share_asset")
        required = share["inputSchema"].get("required", [])
        assert "asset_id" in required
        assert "to_peer_id" in required

    def test_search_team_assets_required_includes_query(self):
        """search_team_assets 的 required 包含 query。"""
        search = next(t for t in TOOL_DEFINITIONS if t["name"] == "search_team_assets")
        required = search["inputSchema"].get("required", [])
        assert "query" in required

    def test_tool_descriptions_non_empty(self):
        """每个工具描述非空。"""
        for tool in TOOL_DEFINITIONS:
            assert tool["description"], f"工具描述为空: {tool['name']}"


# ---------------------------------------------------------------------------
# TestExecuteTool
# ---------------------------------------------------------------------------


class TestExecuteTool:
    """execute_tool 函数测试（使用 StubBridge）。"""

    def test_execute_ask_peer(self):
        """ask_peer 工具调用（仅 peer_id）。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "ask_peer", {"peer_id": "bob", "question": "问题？"})

        assert "result" in result
        assert "error" not in result
        assert result["result"]["peer_id"] == "bob"
        # 确认 bridge.ask_peer 被调用
        assert len(bridge.call_log) == 1
        assert bridge.call_log[0][0] == "ask_peer"
        # 调用参数含 tag / in_reply_to（默认空字符串）
        call_args = bridge.call_log[0][1]
        assert call_args["peer_id"] == "bob"
        assert call_args["question"] == "问题？"
        assert call_args["tag"] == ""
        assert call_args["in_reply_to"] == ""

    def test_execute_ask_peer_with_tag(self):
        """ask_peer 工具按 tag 路由调用（Task 26 SubTask 26.2）。"""
        bridge = StubBridge()

        result = execute_tool(
            bridge,
            "ask_peer",
            {"tag": "运维", "question": "如何处理磁盘告警？"},
        )

        assert "result" in result
        assert "error" not in result
        call_args = bridge.call_log[0][1]
        assert call_args["peer_id"] == ""
        assert call_args["tag"] == "运维"
        assert call_args["question"] == "如何处理磁盘告警？"
        # tag 路由时结果携带 tag 字段
        assert result["result"]["tag"] == "运维"

    def test_execute_ask_peer_with_in_reply_to(self):
        """ask_peer 工具支持 in_reply_to 回复链参数。"""
        bridge = StubBridge()

        result = execute_tool(
            bridge,
            "ask_peer",
            {"peer_id": "bob", "question": "继续", "in_reply_to": "evt-abc"},
        )

        assert "result" in result
        assert "error" not in result
        call_args = bridge.call_log[0][1]
        assert call_args["in_reply_to"] == "evt-abc"

    def test_execute_ask_peer_peer_id_takes_precedence_over_tag(self):
        """同时提供 peer_id 和 tag 时，peer_id 优先（tag 不传给 bridge）。"""
        bridge = StubBridge()

        result = execute_tool(
            bridge,
            "ask_peer",
            {"peer_id": "bob", "tag": "运维", "question": "问题？"},
        )

        assert "result" in result
        assert "error" not in result
        call_args = bridge.call_log[0][1]
        # peer_id 优先，bridge 收到 peer_id 非空、tag 被忽略（TransportBridge 实际行为）
        assert call_args["peer_id"] == "bob"
        # StubBridge 透传 tag 字段，但 TransportBridge 真实逻辑会忽略 tag
        # 此处仅验证 execute_tool 正确读取两个参数

    def test_execute_ask_peer_without_peer_id_and_tag_returns_error(self):
        """未提供 peer_id 和 tag 时返回明确错误（Task 26 校验）。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "ask_peer", {"question": "问题？"})

        assert "error" in result
        assert "result" not in result
        assert "peer_id" in result["error"] or "tag" in result["error"]
        # bridge.ask_peer 不应被调用
        assert len(bridge.call_log) == 0

    def test_execute_list_peers(self):
        """list_peers 工具调用。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "list_peers", {})

        assert "result" in result
        assert "error" not in result
        assert isinstance(result["result"], list)
        assert len(result["result"]) >= 1

    def test_execute_share_asset(self):
        """share_asset 工具调用。"""
        bridge = StubBridge()

        result = execute_tool(
            bridge,
            "share_asset",
            {"asset_id": "a1", "to_peer_id": "bob", "content": {"k": "v"}},
        )

        assert "result" in result
        assert "error" not in result
        assert result["result"]["success"] is True

    def test_execute_search_team_assets(self):
        """search_team_assets 工具调用。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "search_team_assets", {"query": "lint", "limit": 5})

        assert "result" in result
        assert "error" not in result
        assert isinstance(result["result"], list)
        assert len(result["result"]) >= 1

    def test_execute_search_team_assets_default_limit(self):
        """search_team_assets 不传 limit 时使用默认值 10。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "search_team_assets", {"query": "lint"})

        assert "result" in result
        assert "error" not in result
        # 验证默认 limit 被传递
        assert bridge.call_log[0][1]["limit"] == 10

    def test_execute_resume_conversation(self):
        """resume_conversation 工具调用（Task 26 SubTask 26.3）。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "resume_conversation", {"peer_id": "bob"})

        assert "result" in result
        assert "error" not in result
        assert result["result"]["peer_id"] == "bob"
        assert len(bridge.call_log) == 1
        assert bridge.call_log[0][0] == "resume_conversation"
        assert bridge.call_log[0][1]["peer_id"] == "bob"

    def test_execute_resume_conversation_without_peer_id_returns_error(self):
        """resume_conversation 未提供 peer_id 时返回错误。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "resume_conversation", {})

        assert "error" in result
        assert "result" not in result
        assert "peer_id" in result["error"]
        assert len(bridge.call_log) == 0

    def test_execute_unknown_tool_returns_error(self):
        """未知工具返回 error。"""
        bridge = StubBridge()

        result = execute_tool(bridge, "nonexistent_tool", {})

        assert "error" in result
        assert "result" not in result
        assert "nonexistent_tool" in result["error"]

    def test_execute_tool_handles_exception(self):
        """工具执行异常时返回 error。"""
        class FailingBridge:
            def ask_peer(
                self,
                peer_id: str,
                question: str,
                *,
                tag: str = "",
                in_reply_to: str = "",
            ) -> dict[str, Any]:
                raise RuntimeError("simulated failure")

        result = execute_tool(FailingBridge(), "ask_peer", {"peer_id": "bob", "question": "x"})

        assert "error" in result
        assert "result" not in result
        assert "simulated failure" in result["error"]


# ---------------------------------------------------------------------------
# TestMcpServer
# ---------------------------------------------------------------------------


class TestMcpServer:
    """McpServer 主类测试。"""

    def test_list_tools_returns_five_tools(self, tmp_path: Path):
        """list_tools 返回 5 个工具（含 resume_conversation，Task 26）。"""
        server = McpServer(_make_config(tmp_path))

        tools = server.list_tools()

        assert len(tools) == 5
        names = {t["name"] for t in tools}
        assert names == {
            "ask_peer",
            "list_peers",
            "search_team_assets",
            "share_asset",
            "resume_conversation",
        }

    def test_list_tools_returns_copies(self, tmp_path: Path):
        """list_tools 返回的是副本，修改不影响内部状态。"""
        server = McpServer(_make_config(tmp_path))

        tools = server.list_tools()
        tools[0]["name"] = "modified"

        # 再次获取，内部状态未被修改
        tools_again = server.list_tools()
        assert "modified" not in {t["name"] for t in tools_again}

    def test_call_tool_ask_peer(self, tmp_path: Path):
        """call_tool 执行 ask_peer 工具。"""
        config = _make_config(tmp_path)
        # 注入 StubTransport 到 bridge
        server = McpServer(config)
        # 替换 bridge 的 transport 为 StubTransport
        from server.mcp_server.transport_bridge import TransportBridge
        from server.transport.types import Message, PeerInfo, SyncResult
        import uuid as _uuid

        class _Stub:
            def __init__(self):
                self.reachable_peers = {"bob"}
                self.auto_answer = "42"
                self.delivered = []

            def deliver(self, peer_id, messages):
                self.delivered.append((peer_id, messages))
                return SyncResult(success=True, delivered_count=len(messages))

            def fetch(self, peer_id, since_vector_clock=None):
                responses = []
                for p, msgs in self.delivered:
                    if p != peer_id:
                        continue
                    for msg in msgs:
                        responses.append(Message(
                            message_id=str(_uuid.uuid4()),
                            event_id=str(_uuid.uuid4()),
                            sender_id=peer_id,
                            recipient_id=msg.sender_id,
                            msg_type="answer",
                            payload={"answer": self.auto_answer},
                            timestamp=datetime.now(timezone.utc).isoformat(),
                            in_reply_to=msg.event_id,
                        ))
                return responses

            def is_peer_reachable(self, peer_id):
                return peer_id in self.reachable_peers

            def discover_peers(self):
                return [PeerInfo(peer_id=p, online=True) for p in self.reachable_peers]

        # 重建 bridge with stub transport
        server.bridge = TransportBridge(config, transport=_Stub())

        result = server.call_tool("ask_peer", {"peer_id": "bob", "question": "问题？"})

        assert "result" in result
        assert result["result"]["peer_id"] == "bob"
        assert result["result"]["answer"] == "42"

    def test_call_tool_ask_peer_with_tag(self, tmp_path: Path):
        """call_tool 执行 ask_peer 按 tag 路由（Task 26 SubTask 26.2）。"""
        config = _make_config(tmp_path)
        server = McpServer(config)
        # 使用默认 bridge（无 reachable peer），tag 路由无候选 → 降级影子联络
        result = server.call_tool(
            "ask_peer",
            {"tag": "运维", "question": "如何处理磁盘告警？"},
        )

        assert "result" in result
        assert "error" not in result
        # tag 路由时结果携带 tag 字段
        assert result["result"]["tag"] == "运维"

    def test_call_tool_ask_peer_without_peer_id_and_tag_returns_error(
        self, tmp_path: Path,
    ):
        """call_tool 执行 ask_peer 未提供 peer_id 和 tag 时返回错误。"""
        server = McpServer(_make_config(tmp_path))

        result = server.call_tool("ask_peer", {"question": "问题？"})

        assert "error" in result
        assert "result" not in result

    def test_call_tool_resume_conversation(self, tmp_path: Path):
        """call_tool 执行 resume_conversation 工具（Task 26 SubTask 26.3）。"""
        server = McpServer(_make_config(tmp_path))

        result = server.call_tool("resume_conversation", {"peer_id": "bob"})

        assert "result" in result
        assert "error" not in result
        assert result["result"]["peer_id"] == "bob"
        assert "resumed" in result["result"]
        assert "event_count" in result["result"]

    def test_call_tool_resume_conversation_without_peer_id_returns_error(
        self, tmp_path: Path,
    ):
        """call_tool 执行 resume_conversation 未提供 peer_id 时返回错误。"""
        server = McpServer(_make_config(tmp_path))

        result = server.call_tool("resume_conversation", {})

        assert "error" in result
        assert "result" not in result

    def test_call_tool_unknown_returns_error(self, tmp_path: Path):
        """call_tool 未知工具返回 error。"""
        server = McpServer(_make_config(tmp_path))

        result = server.call_tool("nonexistent", {})

        assert "error" in result
        assert "result" not in result

    def test_mcp_server_has_bridge(self, tmp_path: Path):
        """McpServer 初始化后拥有 bridge 属性。"""
        server = McpServer(_make_config(tmp_path))

        assert hasattr(server, "bridge")
        assert server.bridge is not None

    def test_mcp_available_flag_is_boolean(self):
        """MCP_AVAILABLE 是布尔值（mcp 包可能安装也可能未安装）。"""
        assert isinstance(MCP_AVAILABLE, bool)

    def test_run_without_mcp_package_does_not_raise(self, tmp_path: Path):
        """run() 在 mcp 包未安装时不抛异常（降级为 warning）。"""
        server = McpServer(_make_config(tmp_path))
        # 无论 mcp 是否安装，run() 都不应抛异常
        # mcp 已安装时会尝试启动 stdio_server，测试环境可能无 stdio
        # mcp 未安装时仅打印 warning
        if not MCP_AVAILABLE:
            server.run()  # 不应抛异常
        else:
            # mcp 已安装时 run() 会尝试启动服务，测试中跳过
            pytest.skip("mcp 包已安装，run() 会启动真实服务，跳过此测试")
