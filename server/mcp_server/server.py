"""MCP Server 主类。

基于 Python MCP SDK（可选依赖，未安装时降级为 Stub）。

对应 Task 20：作为成员 AI 通信的统一 skill 入口，让 Claude Code 等 AI 软件
可通过 MCP 调用 ask_peer / search_team_assets / list_peers / share_asset 工具。

设计要点：
- MCP SDK 可选：``mcp`` 包未安装时降级为 Stub，保证模块可导入、可测试
- 工具注册：4 个工具共享同一 transport_bridge
- 与 CLI 共享底层：MCP Server 和 CLI 子命令调用同一套 PeerComm / async_comm 层
"""

from __future__ import annotations

import logging
from typing import Any

from server.mcp_server.tools import TOOL_DEFINITIONS, execute_tool
from server.mcp_server.transport_bridge import TransportBridge

logger = logging.getLogger(__name__)

# MCP SDK 可选导入：未安装时降级为 Stub（参考 p2p_transport.py 的 _StubWSConnection 模式）
try:
    from mcp.server import Server  # type: ignore
    from mcp.types import TextContent, Tool  # type: ignore

    MCP_AVAILABLE = True
except ImportError:
    MCP_AVAILABLE = False

    class Server:  # type: ignore
        """Stub MCP Server（mcp 包未安装时）。

        签名兼容真实 MCP Server，所有方法 no-op，保证模块可导入、可测试。
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

    class Tool:  # type: ignore
        """Stub MCP Tool 类型（mcp 包未安装时）。"""

        def __init__(
            self,
            *,
            name: str = "",
            description: str = "",
            inputSchema: dict | None = None,
        ) -> None:
            self.name = name
            self.description = description
            self.inputSchema = inputSchema or {}

    class TextContent:  # type: ignore
        """Stub MCP TextContent 类型（mcp 包未安装时）。"""

        def __init__(self, *, type: str = "text", text: str = "") -> None:
            self.type = type
            self.text = text


class McpServer:
    """TeamHarness MCP Server。

    使用：
        server = McpServer(config)
        server.run()  # 启动 MCP 服务

    或直接调用工具（不启动 MCP 协议）：
        server = McpServer(config)
        result = server.call_tool("ask_peer", {"peer_id": "bob", "question": "..."})

    Attributes:
        bridge: TransportBridge 实例（提供底层 ask_peer / list_peers 等方法）。
    """

    def __init__(self, config: Any) -> None:
        """初始化 MCP Server。

        Args:
            config: ClientConfig 实例（或任何可被 TransportBridge 接受的配置）。
        """
        self.bridge = TransportBridge(config)
        self._tools = TOOL_DEFINITIONS

    def list_tools(self) -> list[dict[str, Any]]:
        """列出所有可用工具。

        Returns:
            工具定义字典列表（含 name / description / inputSchema）。
        """
        return [dict(tool) for tool in self._tools]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """调用工具。

        Args:
            name: 工具名称（ask_peer / list_peers / share_asset / search_team_assets）。
            arguments: 工具参数。

        Returns:
            dict 含 result 或 error。
        """
        return execute_tool(self.bridge, name, arguments)

    def run(self) -> None:
        """启动 MCP 服务（需要 mcp 包安装）。

        未安装 mcp 包时打印 warning 并返回，不抛异常。
        """
        if not MCP_AVAILABLE:
            logger.warning(
                "mcp 包未安装，MCP Server 无法启动。请 pip install mcp"
            )
            return

        # MCP 协议启动（stdio 模式）
        # 真实场景下会注册工具处理器并启动 stdio_server
        # 此处保留入口，具体协议交互由 mcp SDK 处理
        try:
            import asyncio

            asyncio.run(self._serve())
        except Exception as exc:  # noqa: BLE001 — 启动异常兜底
            logger.error("MCP Server 启动失败: %s", exc)

    async def _serve(self) -> None:
        """MCP 协议服务循环（mcp 包安装时调用）。"""
        from mcp.server.stdio import stdio_server  # type: ignore

        server = Server("teamharness")

        @server.list_tools()  # type: ignore
        async def handle_list_tools() -> list[Tool]:
            return [
                Tool(
                    name=tool["name"],
                    description=tool["description"],
                    inputSchema=tool["inputSchema"],
                )
                for tool in self._tools
            ]

        @server.call_tool()  # type: ignore
        async def handle_call_tool(
            name: str, arguments: dict[str, Any]
        ) -> list[TextContent]:
            result = execute_tool(self.bridge, name, arguments)
            import json

            return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False))]

        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())


__all__ = [
    "MCP_AVAILABLE",
    "McpServer",
    "Server",
    "TextContent",
    "Tool",
]
