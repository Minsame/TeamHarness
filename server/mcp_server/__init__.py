"""MCP Server 模块：成员 AI 通信的统一 skill 入口。

对应 Task 20。作为 MCP (Model Context Protocol) Server，让 Claude Code 等
AI 软件可通过 MCP 调用 ask_peer / search_team_assets / list_peers /
share_asset 工具。

模块结构：
- TransportBridge：桥接 async_comm + transport 层，提供统一底层调用入口
- TOOL_DEFINITIONS / execute_tool：4 个工具的定义与执行
- McpServer：MCP Server 主类（mcp 包未安装时降级为 Stub）
"""

from __future__ import annotations

from server.mcp_server.server import MCP_AVAILABLE, McpServer
from server.mcp_server.tools import TOOL_DEFINITIONS, execute_tool
from server.mcp_server.transport_bridge import BridgeConfig, TransportBridge

__all__ = [
    "BridgeConfig",
    "MCP_AVAILABLE",
    "McpServer",
    "TOOL_DEFINITIONS",
    "TransportBridge",
    "execute_tool",
]
