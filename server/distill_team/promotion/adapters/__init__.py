"""coding 软件适配器子包。

提供对主流 coding 软件（Trae / Cursor / Claude Code / Windsurf / Cline）的记忆结构适配。
通过 create_adapter() 工厂函数获取适配器实例，不硬编码任何软件的路径。
"""

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.adapters.claude_code import ClaudeCodeAdapter
from server.distill_team.promotion.adapters.cline import ClineAdapter
from server.distill_team.promotion.adapters.cursor import CursorAdapter
from server.distill_team.promotion.adapters.factory import (
    create_adapter,
    list_adapters,
)
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.adapters.windsurf import WindsurfAdapter

__all__ = [
    "ClaudeCodeAdapter",
    "ClineAdapter",
    "CodingSoftwareAdapter",
    "CursorAdapter",
    "MemoryLayout",
    "RuleEntry",
    "TraeAdapter",
    "WindsurfAdapter",
    "create_adapter",
    "list_adapters",
]
