"""MCP 工具注册。

5 个工具：
- ask_peer：向 peer AI 提问（按 peer_id 或 tag 路由）
- search_team_assets：搜索团队资产
- list_peers：列出已知 peer
- share_asset：资产定向共享
- resume_conversation：恢复暂停的对话

对应 Task 20；Task 26 扩展 ask_peer 描述与 schema，新增 resume_conversation。
"""

from __future__ import annotations

from typing import Any

# 工具定义（名称、描述、输入 schema）
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "ask_peer",
        "description": (
            "当你需要向其他成员的 AI 提问、讨论或共享资产时调用。"
            "向指定 peer 的 AI 提问：peer_id 指定目标成员时走点对点通信，"
            "tag 指定标签时按标签路由到匹配的候选 peer。"
            "peer 在线时实时通信，离线时自动降级为影子联络。"
            "peer_id 与 tag 二选一（同时提供时优先 peer_id）。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer_id": {"type": "string", "description": "目标 peer 的成员 ID（与 tag 二选一）"},
                "tag": {"type": "string", "description": "按标签路由（如 \"运维\"），与 peer_id 二选一"},
                "question": {"type": "string", "description": "提问内容"},
                "in_reply_to": {"type": "string", "description": "关联的前驱事件 ID（回复链，可选）"},
            },
            "required": ["question"],
        },
    },
    {
        "name": "list_peers",
        "description": "列出当前可用的 peer 列表。",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "share_asset",
        "description": "向指定 peer 共享资产。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "asset_id": {"type": "string", "description": "资产 ID"},
                "to_peer_id": {"type": "string", "description": "目标 peer ID"},
                "content": {"type": "object", "description": "资产内容（可选）"},
            },
            "required": ["asset_id", "to_peer_id"],
        },
    },
    {
        "name": "search_team_assets",
        "description": "搜索团队共享资产库。",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "搜索查询"},
                "limit": {"type": "integer", "description": "返回数量上限", "default": 10},
            },
            "required": ["query"],
        },
    },
    {
        "name": "resume_conversation",
        "description": (
            "恢复与指定 peer 的暂停对话。当 peer 重新上线且有未完成的对话时调用。"
            "基于 in_reply_to 链重建上下文，返回历史事件列表。"
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "peer_id": {"type": "string", "description": "目标 peer 的成员 ID"},
            },
            "required": ["peer_id"],
        },
    },
]


def execute_tool(
    bridge: Any,
    tool_name: str,
    arguments: dict[str, Any],
) -> dict[str, Any]:
    """执行工具调用。

    Args:
        bridge: TransportBridge 实例（提供 ask_peer / list_peers /
            share_asset / search_team_assets / resume_conversation 方法）。
        tool_name: 工具名称。
        arguments: 工具参数。

    Returns:
        dict 含 result 或 error：
        - 成功：{"result": <工具返回值>}
        - 未知工具：{"error": "unknown tool: <name>"}
        - 入参校验失败：{"error": "<校验错误信息>"}
        - 执行异常：{"error": "<异常信息>"}
    """
    try:
        if tool_name == "ask_peer":
            peer_id = str(arguments.get("peer_id", ""))
            tag = str(arguments.get("tag", ""))
            question = str(arguments.get("question", ""))
            in_reply_to = str(arguments.get("in_reply_to", ""))
            # peer_id 与 tag 二选一
            if not peer_id and not tag:
                return {"error": "必须提供 peer_id 或 tag"}
            result = bridge.ask_peer(
                peer_id,
                question,
                tag=tag,
                in_reply_to=in_reply_to,
            )
            return {"result": result}
        if tool_name == "list_peers":
            result = bridge.list_peers()
            return {"result": result}
        if tool_name == "share_asset":
            asset_id = str(arguments.get("asset_id", ""))
            to_peer_id = str(arguments.get("to_peer_id", ""))
            content = arguments.get("content")
            result = bridge.share_asset(asset_id, to_peer_id, content=content)
            return {"result": result}
        if tool_name == "search_team_assets":
            query = str(arguments.get("query", ""))
            limit = int(arguments.get("limit", 10))
            result = bridge.search_team_assets(query, limit=limit)
            return {"result": result}
        if tool_name == "resume_conversation":
            peer_id = str(arguments.get("peer_id", ""))
            if not peer_id:
                return {"error": "必须提供 peer_id"}
            result = bridge.resume_conversation(peer_id)
            return {"result": result}
        return {"error": f"unknown tool: {tool_name}"}
    except Exception as exc:  # noqa: BLE001 — 工具执行异常捕获，返回 error
        return {"error": str(exc)}


__all__ = [
    "TOOL_DEFINITIONS",
    "execute_tool",
]
