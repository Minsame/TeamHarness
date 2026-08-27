"""coding_adapters 领域包：本机 AI coding 软件检测与资产采集适配。

包含：
- SOFTWARE_FINGERPRINTS：软件 → 路径/cli/provider 映射表（fingerprints.py）
- resolve_path：跨平台路径模板解析（fingerprints.py）
- CodingSoftwareRegistry：本机软件检测与 provider 路由（registry.py）
  - discover_installed：路径直探 + PATH 扫描（Task 1 / Task 2 前两级）
  - discover_by_fingerprint：指纹模糊匹配兜底（Task 2 第三级）
- InstalledSoftware：已安装软件信息 DTO（registry.py）
- 各软件 Adapter（Task 3，均实现 SessionProvider Protocol）：
  - ClaudeCodeAdapter：~/.claude/projects/**/*.jsonl
  - CodexAdapter：~/.codex/sessions/*
  - CursorAdapter：~/.cursor/state.vscdb（SQLite 只读）
  - AiderAdapter：.aider.chat.history.md（Markdown）
  - WindsurfAdapter：~/.codeium/windsurf/sessions/*.jsonl
"""

from server.coding_adapters.aider import AiderAdapter
from server.coding_adapters.claude_code import ClaudeCodeAdapter
from server.coding_adapters.codex import CodexAdapter
from server.coding_adapters.cursor import CursorAdapter
from server.coding_adapters.fingerprints import (
    SOFTWARE_FINGERPRINTS,
    resolve_path,
)
from server.coding_adapters.registry import (
    CodingSoftwareRegistry,
    InstalledSoftware,
)
from server.coding_adapters.windsurf import WindsurfAdapter

__all__ = [
    "AiderAdapter",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CursorAdapter",
    "SOFTWARE_FINGERPRINTS",
    "WindsurfAdapter",
    "CodingSoftwareRegistry",
    "InstalledSoftware",
    "resolve_path",
]
