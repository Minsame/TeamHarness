"""Trae 深度适配。

对应技术方案 SubTask 1.5：
- frontmatter 双区设计：顶部 `---coding: trae---` 区放 coding 软件专用字段，
  下方 `---teamharness---` 区放 TeamHarness 字段，两区互不干扰。
- 会话路径自动探测 discover_sessions_root()：按 OS 查找 Trae 会话目录
  （Windows: c:\\Users\\<user>\\.trae-cn\\sessions\\*.jsonl）

设计目标：Trae 等 coding 软件只读自己的 coding 区，TeamHarness 只读 teamharness 区，
彼此不破坏对方字段。
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

# coding 区的 marker 行：第一个 frontmatter 必须含 `coding: <software>` 顶层键
CODING_MARKER_KEY = "coding"
# teamharness 区由独立的 frontmatter 块承载，块内顶层为 teamharness 命名空间
TEAMHARNESS_BLOCK_KEY = "teamharness"


@dataclass
class TraeFrontmatter:
    """双区 frontmatter 解析结果。

    coding_fields：coding 软件专用字段（含 `coding` 标识键）。
    teamharness_fields：TeamHarness 字段（id/type/owner/scope/tags/...）。
    body：双区之后的正文。
    """

    coding_fields: dict[str, Any] = field(default_factory=dict)
    teamharness_fields: dict[str, Any] = field(default_factory=dict)
    body: str = ""

    @property
    def coding_software(self) -> str:
        """当前 coding 软件标识（如 trae / cursor）。"""
        return str(self.coding_fields.get(CODING_MARKER_KEY, ""))


# ---------------------------------------------------------------------------
# 双区 frontmatter 解析与序列化
# ---------------------------------------------------------------------------

# 匹配连续两个 frontmatter 块（coding 区 + teamharness 区），中间允许空行
_DUAL_FRONTMATTER_RE = re.compile(
    r"^---\s*\n(?P<coding>.*?)\n---\s*\n\s*?\n?---\s*\n(?P<team>.*?)\n---\s*\n?(?P<body>.*)$",
    re.DOTALL,
)
# 单区匹配（仅有 coding 区或仅有 teamharness 区）
_SINGLE_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", re.DOTALL)


def parse_frontmatter_dual(content: str) -> TraeFrontmatter:
    """解析双区 frontmatter。

    支持三种情况：
    1. 双区（coding + teamharness）→ 同时填充两区
    2. 仅 coding 区 → coding_fields 填充，teamharness_fields 为空
    3. 仅 teamharness 区（单块且含 teamharness 键）→ teamharness_fields 填充
    4. 无 frontmatter → 全空，body=原文
    """
    m = _DUAL_FRONTMATTER_RE.match(content)
    if m:
        coding_raw = m.group("coding")
        team_raw = m.group("team")
        body = m.group("body")
        coding_data = yaml.safe_load(coding_raw) or {}
        team_data = yaml.safe_load(team_raw) or {}
        if not isinstance(coding_data, dict):
            coding_data = {}
        if not isinstance(team_data, dict):
            team_data = {}
        # teamharness 区允许整块即为 teamharness 命名空间，或字段平铺
        if TEAMHARNESS_BLOCK_KEY in team_data and isinstance(
            team_data[TEAMHARNESS_BLOCK_KEY], dict
        ):
            team_data = team_data[TEAMHARNESS_BLOCK_KEY]
        return TraeFrontmatter(
            coding_fields=coding_data, teamharness_fields=team_data, body=body
        )

    # 单区
    m = _SINGLE_FRONTMATTER_RE.match(content)
    if m:
        raw = m.group(1)
        body = m.group(2)
        data = yaml.safe_load(raw) or {}
        if not isinstance(data, dict):
            return TraeFrontmatter(body=content)
        if CODING_MARKER_KEY in data:
            return TraeFrontmatter(coding_fields=data, body=body)
        if TEAMHARNESS_BLOCK_KEY in data and isinstance(
            data[TEAMHARNESS_BLOCK_KEY], dict
        ):
            return TraeFrontmatter(
                teamharness_fields=data[TEAMHARNESS_BLOCK_KEY], body=body
            )
        # 单块无 marker，按 teamharness 兼容处理
        return TraeFrontmatter(teamharness_fields=data, body=body)

    return TraeFrontmatter(body=content)


def serialize_frontmatter_dual(fm: TraeFrontmatter) -> str:
    """序列化双区 frontmatter 为文本。

    coding 区始终输出（含 `coding` 标识键）；
    teamharness 区在 teamharness_fields 非空时输出。
    """
    parts: list[str] = []
    coding_yaml = yaml.safe_dump(
        fm.coding_fields, sort_keys=False, allow_unicode=True
    ).strip()
    parts.append(f"---\n{coding_yaml}\n---")
    if fm.teamharness_fields:
        team_yaml = yaml.safe_dump(
            {TEAMHARNESS_BLOCK_KEY: fm.teamharness_fields}
            if fm.teamharness_fields
            else {},
            sort_keys=False,
            allow_unicode=True,
        ).strip()
        parts.append(f"\n---\n{team_yaml}\n---")
    body = fm.body.lstrip("\n")
    if body:
        parts.append("\n" + body)
    else:
        parts.append("\n")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Trae 会话路径自动探测
# ---------------------------------------------------------------------------


def _windows_trae_root() -> Path | None:
    """Windows: c:\\Users\\<user>\\.trae-cn\\sessions\\。"""
    user_profile = os.environ.get("USERPROFILE") or os.path.getlogin()
    if not user_profile:
        return None
    return Path(user_profile) / ".trae-cn" / "sessions"


def _macos_trae_root() -> Path | None:
    """macOS: ~/.trae-cn/sessions/。"""
    home = os.environ.get("HOME")
    if not home:
        return None
    return Path(home) / ".trae-cn" / "sessions"


def _linux_trae_root() -> Path | None:
    """Linux: ~/.trae-cn/sessions/。"""
    home = os.environ.get("HOME")
    if not home:
        return None
    return Path(home) / ".trae-cn" / "sessions"


def discover_sessions_root() -> Path | None:
    """按操作系统自动探测 Trae 会话目录。

    返回的目录下应包含 *.jsonl 会话文件。
    若候选目录不存在则返回 None，由调用方决定降级策略。
    """
    candidates: list[Path | None] = []
    if sys.platform == "win32":
        candidates.append(_windows_trae_root())
    elif sys.platform == "darwin":
        candidates.append(_macos_trae_root())
    else:
        candidates.append(_linux_trae_root())

    # 同时支持显式环境变量覆盖（CI / 自定义安装路径）
    env_override = os.environ.get("TRAE_SESSIONS_ROOT")
    if env_override:
        candidates.insert(0, Path(env_override))

    for cand in candidates:
        if cand and cand.is_dir():
            return cand
    return None


def list_trae_sessions(since: float | None = None) -> list[Path]:
    """列出 Trae 会话文件（*.jsonl），按修改时间升序。

    since 为时间戳（epoch），仅返回修改时间 >= since 的会话（增量采集）。
    """
    root = discover_sessions_root()
    if root is None:
        return []
    sessions = sorted(
        (f for f in root.glob("*.jsonl") if f.is_file()),
        key=lambda p: p.stat().st_mtime,
    )
    if since is None:
        return sessions
    return [s for s in sessions if s.stat().st_mtime >= since]
