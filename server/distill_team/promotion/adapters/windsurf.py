"""Windsurf IDE 适配器。

Windsurf 记忆结构：
- 项目级规则：.windsurfrules（单文件）或 .windsurf/rules/（目录，YAML frontmatter）
- 全局规则：~/.codeium/windsurf/rules/
- Memories：跨会话持久化（存储在 ~/.codeium/windsurf/ 下）
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from server.distill_team.promotion.adapters.base import (
    CodingSoftwareAdapter,
    MemoryLayout,
    RuleEntry,
)


class WindsurfAdapter(CodingSoftwareAdapter):
    """Windsurf IDE 适配器。"""

    name = "windsurf"
    display_name = "Windsurf IDE"

    def detect(self, project_root: Path) -> bool:
        """检测项目是否使用 Windsurf。"""
        return (
            (project_root / ".windsurf").is_dir()
            or (project_root / ".windsurfrules").is_file()
        )

    def get_layout(self, project_root: Path) -> MemoryLayout:
        home = Path.home()
        global_rules = home / ".codeium" / "windsurf" / "rules"
        return MemoryLayout(
            project_rules_dir=project_root / ".windsurf" / "rules",
            global_rules_dir=global_rules,
            user_profile_path=global_rules / "windsurf_global_rules.md",
            archive_path=global_rules / "archive.md",
            graph_path=global_rules / "graph.md",
            cross_project_root=home / ".codeium" / "windsurf" / "projects",
            rules_file_ext=".md",
            supports_frontmatter=True,
            hotspot_section_marker="## 热点规则",
        )

    def parse_existing_rules(self, rules_dir: Path) -> list[RuleEntry]:
        """解析 .windsurf/rules/ 下的规则文件。"""
        if not rules_dir.is_dir():
            return []
        entries: list[RuleEntry] = []
        for f in sorted(rules_dir.glob("*.md")):
            if f.name in ("archive.md", "graph.md", "windsurf_global_rules.md"):
                continue
            content = f.read_text(encoding="utf-8")
            fm, body = self._parse_frontmatter(content)
            rule_id = fm.get("id", f.stem)
            title = fm.get("title", "") or self._extract_title(body) or f.stem
            entries.append(
                RuleEntry(
                    rule_id=rule_id,
                    title=title,
                    content=body,
                    file_path=f,
                    category=fm.get("category"),
                    frontmatter=fm,
                )
            )
        # 兼容单文件 .windsurfrules
        parent = rules_dir.parent
        single_file = parent / ".windsurfrules"
        if single_file.is_file():
            content = single_file.read_text(encoding="utf-8")
            entries.append(
                RuleEntry(
                    rule_id="windsurfrules",
                    title=".windsurfrules",
                    content=content,
                    file_path=single_file,
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
        fm: dict[str, Any] = {"id": rule_id, "title": title}
        if frontmatter:
            fm.update(frontmatter)
        fm_text = self._build_frontmatter(fm)
        file_path = rules_dir / f"{rule_id}.md"
        file_path.write_text(fm_text + content, encoding="utf-8")
        return file_path

    def read_hotspot_rules(self, user_profile_path: Path) -> list[RuleEntry]:
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
                    rest = line[4:]
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
        user_profile_path.parent.mkdir(parents=True, exist_ok=True)
        if user_profile_path.is_file():
            text = user_profile_path.read_text(encoding="utf-8")
        else:
            text = "# Windsurf Global Rules\n"

        new_line = f"- **{rule_id}**：{content}"
        if "## 热点规则" in text:
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
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n## 热点规则（0/5）\n\n{new_line}\n"
            user_profile_path.write_text(text, encoding="utf-8")

    @staticmethod
    def _extract_title(body: str) -> str:
        for line in body.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                return stripped.lstrip("#").strip()
        return ""


__all__ = ["WindsurfAdapter"]
