"""Trae IDE 适配器（CN 版 / Global 版）。

Trae 记忆结构：
- 项目级规则：.trae/rules/
- 全局规则：~/.trae-cn/rules/ 或 ~/.trae/rules/
- 用户配置：~/.trae-cn/memory/user_profile.md
- 项目记忆：~/.trae-cn/memory/projects/<project_hash>/
- skills：~/.trae-cn/skills/
- 归档区：~/.trae-cn/meta/archive.md
- 图谱：~/.trae-cn/meta/graph.md
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)


class TraeAdapter(CodingSoftwareAdapter):
    """Trae IDE 适配器。

    自动检测 CN 版（~/.trae-cn/）和 Global 版（~/.trae/）。
    CN 版优先（若同时存在）。
    """

    name = "trae"
    display_name = "Trae IDE"

    def detect(self, project_root: Path) -> bool:
        """检测项目是否使用 Trae：项目根有 .trae/ 目录。"""
        return (project_root / ".trae").is_dir()

    def _get_global_root(self) -> Path:
        """返回全局根目录（CN 版优先）。"""
        home = Path.home()
        cn_root = home / ".trae-cn"
        if cn_root.is_dir():
            return cn_root
        return home / ".trae"

    def get_layout(self, project_root: Path) -> MemoryLayout:
        global_root = self._get_global_root()
        return MemoryLayout(
            project_rules_dir=project_root / ".trae" / "rules",
            global_rules_dir=global_root / "rules",
            user_profile_path=global_root / "memory" / "user_profile.md",
            archive_path=global_root / "meta" / "archive.md",
            graph_path=global_root / "meta" / "graph.md",
            cross_project_root=global_root / "memory" / "projects",
            rules_file_ext=".md",
            supports_frontmatter=True,
            hotspot_section_marker="## 热点规则",
        )

    def parse_existing_rules(self, rules_dir: Path) -> list[RuleEntry]:
        """解析 .trae/rules/ 下的规则文件（.md）。

        Trae 规则文件格式：Markdown，标题为 ## 规则名 或 # 规则名。
        """
        if not rules_dir.is_dir():
            return []
        entries: list[RuleEntry] = []
        for f in sorted(rules_dir.glob("*.md")):
            if f.name in ("archive.md", "graph.md"):
                continue
            content = f.read_text(encoding="utf-8")
            fm, body = self._parse_frontmatter(content)
            rule_id = fm.get("id", f.stem)
            title = fm.get("title", "") or self._extract_title(body) or f.stem
            category = fm.get("category")
            entries.append(
                RuleEntry(
                    rule_id=rule_id,
                    title=title,
                    content=body,
                    file_path=f,
                    category=category,
                    frontmatter=fm,
                )
            )
        return entries

    def write_rule(
        self,
        rules_dir: Path,
        rule_id: str,
        title: str,
        content: str,
        frontmatter: dict[str, Any] | None = None,
    ) -> Path:
        rules_dir.mkdir(parents=True, exist_ok=True)
        fm = {"id": rule_id, "title": title}
        if frontmatter:
            fm.update(frontmatter)
        fm_text = self._build_frontmatter(fm)
        file_path = rules_dir / f"{rule_id}.md"
        file_path.write_text(fm_text + content, encoding="utf-8")
        return file_path

    def read_hotspot_rules(self, user_profile_path: Path) -> list[RuleEntry]:
        """读取 user_profile.md 热点规则区。

        热点规则区格式：
        ## 热点规则（N/5）
        - **规则名**：规则简述
        """
        if not user_profile_path.is_file():
            return []
        content = user_profile_path.read_text(encoding="utf-8")
        entries: list[RuleEntry] = []
        in_hotspot = False
        for line in content.splitlines():
            if line.startswith("## 热点规则"):
                in_hotspot = True
                continue
            if in_hotspot:
                if line.startswith("## ") or line.startswith("# "):
                    break
                if line.startswith("- **"):
                    # 解析：- **规则名**：规则简述
                    rest = line[4:]  # 去掉 "- **"
                    if "**" in rest:
                        name, _, desc = rest.partition("**")
                        desc = desc.lstrip("：:").strip()
                        entries.append(
                            RuleEntry(
                                rule_id=name.strip(),
                                title=name.strip(),
                                content=desc,
                                file_path=user_profile_path,
                            )
                        )
        return entries

    def write_hotspot_rule(
        self,
        user_profile_path: Path,
        rule_id: str,
        title: str,
        content: str,
    ) -> None:
        """写入热点规则到 user_profile.md。

        策略：在热点规则区末尾追加一行。若区不存在则创建。
        """
        user_profile_path.parent.mkdir(parents=True, exist_ok=True)
        if user_profile_path.is_file():
            text = user_profile_path.read_text(encoding="utf-8")
        else:
            text = ""

        new_line = f"- **{rule_id}**：{content}"
        if "## 热点规则" in text:
            # 找到热点规则区，在下一个 ## 或 # 前插入
            lines = text.splitlines()
            out: list[str] = []
            in_hotspot = False
            inserted = False
            for line in lines:
                if line.startswith("## 热点规则"):
                    in_hotspot = True
                    out.append(line)
                    continue
                if in_hotspot and (line.startswith("## ") or line.startswith("# ")):
                    if not inserted:
                        out.append(new_line)
                        inserted = True
                    in_hotspot = False
                    out.append(line)
                    continue
                out.append(line)
            if not inserted:
                out.append(new_line)
            user_profile_path.write_text("\n".join(out) + "\n", encoding="utf-8")
        else:
            # 追加热点规则区
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n## 热点规则（0/5）\n\n{new_line}\n"
            user_profile_path.write_text(text, encoding="utf-8")

    @staticmethod
    def _extract_title(body: str) -> str:
        """从 Markdown body 提取第一个标题。"""
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""


__all__ = ["TraeAdapter"]
