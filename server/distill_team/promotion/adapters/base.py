"""coding 软件记忆结构适配器基类。

TeamHarness 是通用项目，需要根据用户指定的 coding 软件适配识别其记忆结构。
不同软件（Trae / Cursor / Claude Code / Windsurf / Cline）的规则文件路径、
归档区位置、图谱位置各不相同，通过适配器层统一抽象。

升维管理模块通过适配器获取路径布局，不硬编码任何软件的路径。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class MemoryLayout:
    """某 coding 软件的记忆路径布局。

    对应 resource-harness 规则的三层结构：
    - 第1层：项目级规则（project_rules_dir）
    - 第2层：规则文件完整版（global_rules_dir）
    - 第3层：用户全局顶层（user_profile_path）
    """

    # 项目级规则目录（第1层）
    project_rules_dir: Path
    # 用户全局规则目录（第2层：完整版规则文件）
    global_rules_dir: Path
    # 用户全局顶层配置文件（第3层：热点规则区）
    user_profile_path: Path
    # 归档区路径（archive.md，只增不删）
    archive_path: Path
    # 图谱索引路径（graph.md）
    graph_path: Path
    # 跨项目记忆扫描根路径（用于跨项目再升维）
    cross_project_root: Path
    # 规则文件扩展名（如 .md / .mdc）
    rules_file_ext: str = ".md"
    # 是否支持 YAML frontmatter（Cursor .mdc / Cline / Windsurf 支持）
    supports_frontmatter: bool = True
    # 顶层配置文件中热点规则区的标记（用于定位热点区位置）
    hotspot_section_marker: str = "## 热点规则"

    def ensure_dirs(self) -> None:
        """确保所有目录存在。"""
        for p in (
            self.project_rules_dir,
            self.global_rules_dir,
            self.cross_project_root,
        ):
            p.mkdir(parents=True, exist_ok=True)
        # 确保文件所在目录存在
        for f in (
            self.user_profile_path,
            self.archive_path,
            self.graph_path,
        ):
            f.parent.mkdir(parents=True, exist_ok=True)


@dataclass
class RuleEntry:
    """解析已有规则时返回的规则条目。"""

    rule_id: str
    title: str
    content: str
    file_path: Path
    category: str | None = None
    frontmatter: dict[str, Any] = field(default_factory=dict)


class CodingSoftwareAdapter(ABC):
    """coding 软件记忆结构适配器基类。

    子类必须实现：
    - detect: 检测项目是否使用此软件
    - get_layout: 返回记忆路径布局
    - parse_existing_rules: 解析已有规则（用于查重）
    - write_rule: 写入规则到指定目录
    - read_hotspot_rules: 读取热点规则区
    - write_hotspot_rule: 写入热点规则区
    """

    name: str = "base"
    display_name: str = "Base Adapter"

    @abstractmethod
    def detect(self, project_root: Path) -> bool:
        """检测项目是否使用此 coding 软件。

        通过检查项目根目录的特征文件/目录判断。
        """
        ...

    @abstractmethod
    def get_layout(self, project_root: Path) -> MemoryLayout:
        """返回此软件的记忆路径布局。"""
        ...

    @abstractmethod
    def parse_existing_rules(self, rules_dir: Path) -> list[RuleEntry]:
        """解析指定目录下的已有规则（用于查重）。

        返回规则列表，每条含 id/title/content/file_path。
        """
        ...

    @abstractmethod
    def write_rule(
        self,
        rules_dir: Path,
        rule_id: str,
        title: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
    ) -> Path:
        """写入规则到指定目录，返回文件路径。"""
        ...

    @abstractmethod
    def read_hotspot_rules(self, user_profile_path: Path) -> list[RuleEntry]:
        """读取用户全局顶层的热点规则区。"""
        ...

    @abstractmethod
    def write_hotspot_rule(
        self,
        user_profile_path: Path,
        rule_id: str,
        title: str,
        content: str,
    ) -> None:
        """写入热点规则到用户全局顶层配置文件。"""
        ...

    # ------------------------------------------------------------------
    # 通用辅助方法（子类可复用）
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_frontmatter(content: str) -> tuple[dict[str, Any], str]:
        """解析 YAML frontmatter，返回 (frontmatter, body)。

        若无 frontmatter 返回 ({}, content)。
        """
        if not content.startswith("---"):
            return {}, content
        parts = content.split("---", 2)
        if len(parts) < 3:
            return {}, content
        fm_text = parts[1].strip()
        body = parts[2].lstrip("\n")
        fm: dict[str, Any] = {}
        for line in fm_text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if ":" in line:
                key, _, val = line.partition(":")
                fm[key.strip()] = val.strip().strip('"').strip("'")
        return fm, body

    @staticmethod
    def _build_frontmatter(fm: dict[str, Any]) -> str:
        """构建 YAML frontmatter 字符串。"""
        if not fm:
            return ""
        lines = ["---"]
        for k, v in fm.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        lines.append("")
        return "\n".join(lines)


__all__ = ["CodingSoftwareAdapter", "MemoryLayout", "RuleEntry"]
