"""种子 Prompt 库（SubTask 8.6 + 8.11）。

职责：
- 加载 prompts/seeds/ 目录下预置的常见场景 Prompt
- 用于冷启动期反向验证基线（资产 < 50 时，用公开 Prompt 数据集校准模型输出）
- 提供 list_seeds / get_seed / match_seed 接口

种子文件格式：Markdown + frontmatter（id / category / scenario / tags）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from server.distill_team.convention import parse_frontmatter

logger = logging.getLogger(__name__)


@dataclass
class SeedPrompt:
    """种子 Prompt。"""

    seed_id: str
    category: str
    scenario: str  # 适用场景描述
    title: str
    content: str
    tags: list[str] = field(default_factory=list)
    file_path: str = ""


# 默认种子库目录（项目根/prompts/seeds/）
DEFAULT_SEEDS_DIRNAME = "prompts/seeds"


class SeedLibrary:
    """种子 Prompt 库。

    用法：
        lib = SeedLibrary(repo_root=Path("./repo"))
        seeds = lib.list_seeds()
        matched = lib.match_seed(category="rule-backend", tags=["lint"])
    """

    def __init__(self, repo_root: Path, *, seeds_dirname: str = DEFAULT_SEEDS_DIRNAME) -> None:
        self._repo_root = Path(repo_root)
        self._seeds_dir = self._repo_root / seeds_dirname

    # ------------------------------------------------------------------
    # 加载
    # ------------------------------------------------------------------

    def list_seeds(self) -> list[SeedPrompt]:
        """列出全部种子 Prompt。"""
        if not self._seeds_dir.is_dir():
            return []
        seeds: list[SeedPrompt] = []
        for f in sorted(self._seeds_dir.glob("*.md")):
            seed = self._load_seed_file(f)
            if seed is not None:
                seeds.append(seed)
        return seeds

    def get_seed(self, seed_id: str) -> SeedPrompt | None:
        """按 seed_id 查种子。"""
        for seed in self.list_seeds():
            if seed.seed_id == seed_id:
                return seed
        return None

    def match_seed(
        self,
        *,
        category: str | None = None,
        tags: list[str] | None = None,
        scenario_keyword: str | None = None,
    ) -> list[SeedPrompt]:
        """按 category / tags / scenario 关键词匹配种子。

        任一匹配即返回（OR 语义）。
        """
        seeds = self.list_seeds()
        result: list[SeedPrompt] = []
        for seed in seeds:
            if category and seed.category == category:
                result.append(seed)
                continue
            if tags and any(t in seed.tags for t in tags):
                result.append(seed)
                continue
            if scenario_keyword and scenario_keyword.lower() in seed.scenario.lower():
                result.append(seed)
                continue
        return result

    # ------------------------------------------------------------------
    # 内部：加载种子文件
    # ------------------------------------------------------------------

    def _load_seed_file(self, path: Path) -> SeedPrompt | None:
        """加载单个种子 Prompt 文件。"""
        try:
            content = path.read_text(encoding="utf-8")
        except Exception as exc:
            logger.warning("读取种子文件失败 %s: %s", path, exc)
            return None
        fm = parse_frontmatter(content)
        if not fm.get("id"):
            logger.warning("种子文件缺 id frontmatter %s", path)
            return None
        # 提取 body（去除 frontmatter）
        body = self._strip_frontmatter(content)
        return SeedPrompt(
            seed_id=str(fm.get("id", "")),
            category=str(fm.get("category", "")),
            scenario=str(fm.get("scenario", "")),
            title=str(fm.get("title", path.stem)),
            content=body,
            tags=self._parse_tags(fm.get("tags")),
            file_path=str(path),
        )

    def _strip_frontmatter(self, content: str) -> str:
        """去除 frontmatter，返回 body。"""
        if not content.startswith("---"):
            return content
        lines = content.splitlines()
        end_idx = -1
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                end_idx = i
                break
        if end_idx <= 0:
            return content
        return "\n".join(lines[end_idx + 1:]).strip()

    def _parse_tags(self, val) -> list[str]:
        if isinstance(val, list):
            return [str(x) for x in val]
        if isinstance(val, str):
            return [t.strip() for t in val.split(",") if t.strip()]
        return []


__all__ = ["DEFAULT_SEEDS_DIRNAME", "SeedLibrary", "SeedPrompt"]
