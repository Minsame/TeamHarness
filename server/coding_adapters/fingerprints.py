"""AI coding 软件指纹表与跨平台路径解析。

对应 Task 1：定义 SOFTWARE_FINGERPRINTS（软件 → 路径/cli/provider 映射），
覆盖 trae / claude_code / codex / cursor / aider / windsurf 六款软件。

设计要点：
- 路径模板使用 ~ 与环境变量（%USERPROFILE% / $HOME 等），由 resolve_path 跨平台展开
  （~ 在 Windows 展开为 %USERPROFILE%，在 Unix 展开为 $HOME，故单条 ~ 模板即可跨平台）
- detect.paths 中任一命中即视为已安装；paths 全 miss 时回退到 shutil.which(cli)
- provider 字段为 Adapter 类名字符串，由 registry 路由到具体实现（后续 Task 实现）
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# 跨平台路径解析
# ---------------------------------------------------------------------------


def resolve_path(path_template: str) -> Path:
    """跨平台路径模板解析。

    依次展开环境变量与 ~：
    - Windows: %USERPROFILE% / %LOCALAPPDATA% / %APPDATA% 等
    - Linux/macOS: $HOME / $XDG_CONFIG_HOME 等
    - ~ 在所有平台展开为用户主目录（Windows=USERPROFILE，Unix=HOME）

    expandvars 先行以处理 %VAR% / $VAR，随后 expanduser 处理 ~。
    未匹配的变量或字面量原样保留。
    """
    expanded = os.path.expandvars(path_template)
    expanded = os.path.expanduser(expanded)
    return Path(expanded)


# ---------------------------------------------------------------------------
# 软件指纹表
# ---------------------------------------------------------------------------

SOFTWARE_FINGERPRINTS: dict[str, dict] = {
    "trae": {
        "detect": {
            "cli": None,
            "paths": ["~/.trae-cn"],
        },
        "sessions": "~/.trae-cn/sessions/*.jsonl",
        "rules": ["~/.trae-cn/rules/"],
        "memory": "~/.trae-cn/memory/",
        "provider": "TraeAdapter",
    },
    "claude_code": {
        "detect": {
            "cli": "claude",
            "paths": ["~/.claude"],
        },
        "sessions": "~/.claude/projects/**/*.jsonl",
        "rules": ["CLAUDE.md", "~/.claude/rules/"],
        "memory": "~/.claude/memory/",
        "provider": "ClaudeCodeAdapter",
    },
    "codex": {
        "detect": {
            "cli": "codex",
            "paths": ["~/.codex"],
        },
        "sessions": "~/.codex/sessions/*",
        "rules": ["~/.codex/config.toml"],
        "memory": "~/.codex/memory/",
        "provider": "CodexAdapter",
    },
    "cursor": {
        "detect": {
            "cli": None,
            "paths": ["~/.cursor"],
        },
        "sessions": "~/.cursor/state.vscdb",
        "rules": [".cursorrules", ".cursor/rules/"],
        "memory": "~/.cursor/memory/",
        "provider": "CursorAdapter",
    },
    "aider": {
        "detect": {
            "cli": "aider",
            "paths": [],
        },
        "sessions": ".aider.chat.history.md",
        "rules": [".aider.conf.yml"],
        "memory": None,
        "provider": "AiderAdapter",
    },
    "windsurf": {
        "detect": {
            "cli": None,
            "paths": ["~/.codeium/windsurf"],
        },
        "sessions": "~/.codeium/windsurf/sessions/*",
        "rules": [".windsurfrules"],
        "memory": "~/.codeium/windsurf/memory/",
        "provider": "WindsurfAdapter",
    },
}


__all__ = ["SOFTWARE_FINGERPRINTS", "resolve_path"]
