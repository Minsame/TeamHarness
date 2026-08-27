"""TeamHarness MCP Server stdio 启动入口。

使用：python -m server.mcp_server

启动后通过 stdio 与 MCP 客户端通信（如 DSH 的 @deepseek-ai/dsh-mcp-client）。
工具发现和调用遵循 MCP 协议（list_tools / call_tool）。

环境变量（DSH overlay 注入或用户手动设置）：
    TEAMHARNESS_SERVER_URL: TeamHarness 服务端基址（如 http://localhost:8080）
    TEAMHARNESS_API_KEY: 当前成员 API Key
    TEAMHARNESS_REPO_ROOT: 本地工作目录（存储 mailbox / conversation_log）
    TEAMHARNESS_MEMBER_ID: 成员 ID（可选，默认从 API Key 反查）
    TEAMHARNESS_TOPOLOGY: 通信拓扑（central / p2p / hybrid，默认 central）

对应 Task 20。
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# 项目根加入 sys.path，确保 server.* 可导入（当 cwd 不是项目根时）
_PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from server.client.config import load_client_config  # noqa: E402
from server.mcp_server.server import MCP_AVAILABLE, McpServer  # noqa: E402

logger = logging.getLogger("teamharness.mcp")


def main() -> None:
    """启动 stdio MCP Server。"""
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,  # MCP 协议要求 stdout 只能输出 JSON-RPC，日志走 stderr
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    if not MCP_AVAILABLE:
        logger.error(
            "mcp 包未安装，无法启动 stdio MCP Server。请 pip install mcp"
        )
        sys.exit(1)

    # 从环境变量构造 ClientConfig（DSH overlay 注入或用户手动设置）
    config = load_client_config()
    logger.info(
        "TeamHarness MCP Server 启动：member_id=%s server_url=%s topology=%s",
        config.member_id,
        config.server_url,
        config.topology,
    )

    server = McpServer(config)
    server.run()


if __name__ == "__main__":
    main()
