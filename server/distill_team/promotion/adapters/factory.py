"""coding 软件适配器工厂。

根据用户指定的软件名或自动检测返回对应适配器实例。
"""

from __future__ import annotations

from pathlib import Path

from server.distill_team.promotion.adapters.base import CodingSoftwareAdapter
from server.distill_team.promotion.adapters.claude_code import ClaudeCodeAdapter
from server.distill_team.promotion.adapters.cline import ClineAdapter
from server.distill_team.promotion.adapters.cursor import CursorAdapter
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.adapters.windsurf import WindsurfAdapter

# 已注册的适配器（按检测优先级排序）
# Trae 优先（本项目原生），其次 Cursor / Claude Code / Windsurf / Cline
_REGISTERED_ADAPTERS: list[CodingSoftwareAdapter] = [
    TraeAdapter(),
    CursorAdapter(),
    ClaudeCodeAdapter(),
    WindsurfAdapter(),
    ClineAdapter(),
]

# 名称到适配器的映射
_ADAPTER_MAP: dict[str, CodingSoftwareAdapter] = {
    a.name: a for a in _REGISTERED_ADAPTERS
}


def create_adapter(
    *,
    software: str | None = None,
    project_root: Path | None = None,
) -> CodingSoftwareAdapter:
    """创建 coding 软件适配器。

    - software 显式指定软件名（如 "trae" / "cursor" / "claude_code" / "windsurf" / "cline"）
    - software=None 时自动检测：遍历已注册适配器，返回第一个 detect 成功的
    - project_root 用于自动检测，默认当前工作目录
    """
    if software is not None:
        key = software.strip().lower()
        if key not in _ADAPTER_MAP:
            available = ", ".join(sorted(_ADAPTER_MAP.keys()))
            raise ValueError(
                f"未知的 coding 软件: {software}（支持: {available}）"
            )
        return _ADAPTER_MAP[key]

    # 自动检测
    root = project_root or Path.cwd()
    for adapter in _REGISTERED_ADAPTERS:
        try:
            if adapter.detect(root):
                return adapter
        except Exception:
            continue

    # 默认回退到 Trae（本项目原生）
    return TraeAdapter()


def list_adapters() -> list[CodingSoftwareAdapter]:
    """返回所有已注册的适配器。"""
    return list(_REGISTERED_ADAPTERS)


__all__ = ["create_adapter", "list_adapters"]
