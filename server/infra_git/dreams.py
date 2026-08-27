"""DREAMS.md 按月切分 + 归档压缩。

对应技术方案 SubTask 1.7：
- DREAMS.md 按月切分为 DREAMS/2026-08.md
- 历史月份归档压缩（DREAMS/archive/<YYYY-MM>.md.gz）
- DREAMS.md 入 git（人类可读审查日记），对应技术方案 3.4.1

提炼审查日记由二级提炼 Deep 阶段产出写入，供人工审阅与追溯。
"""

from __future__ import annotations

import gzip
import re
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

DREAMS_DIRNAME = "DREAMS"
ARCHIVE_DIRNAME = "archive"
MONTH_FORMAT = "%Y-%m"
ENTRY_TIMESTAMP_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


@dataclass
class DreamEntry:
    """单条提炼审查日记条目。"""

    timestamp: str
    stage: str  # light / rem / deep / skip
    title: str
    body: str
    metadata: dict[str, str]

    def render(self) -> str:
        """渲染为 Markdown 段落。"""
        meta_str = ""
        if self.metadata:
            meta_lines = [f"- {k}: {v}" for k, v in self.metadata.items()]
            meta_str = "\n".join(meta_lines) + "\n"
        return (
            f"## {self.timestamp} [{self.stage}] {self.title}\n\n"
            f"{meta_str}\n{self.body}\n"
        )


# ---------------------------------------------------------------------------
# 路径计算
# ---------------------------------------------------------------------------


def month_key(d: date | datetime | None = None) -> str:
    """返回月份键，如 2026-08。"""
    if d is None:
        d = datetime.now(timezone.utc)
    if isinstance(d, datetime):
        d = d.date()
    return d.strftime(MONTH_FORMAT)


def get_monthly_path(repo_root: Path, d: date | datetime | None = None) -> Path:
    """返回当月 DREAMS 文件路径：repo_root/DREAMS/2026-08.md。"""
    return Path(repo_root) / DREAMS_DIRNAME / f"{month_key(d)}.md"


def get_archive_path(repo_root: Path, key: str) -> Path:
    """返回归档文件路径：repo_root/DREAMS/archive/2026-08.md.gz。"""
    return Path(repo_root) / DREAMS_DIRNAME / ARCHIVE_DIRNAME / f"{key}.md.gz"


# ---------------------------------------------------------------------------
# 追加与读取
# ---------------------------------------------------------------------------


def _ensure_header(path: Path, key: str) -> None:
    """确保月度文件有标题头。"""
    if not path.exists() or not path.read_text(encoding="utf-8").strip():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# DREAMS 审查日记 - {key}\n\n"
            f"> 本文件由二级提炼引擎自动追加，记录 Light/REM/Deep/Skip 阶段产出。\n\n",
            encoding="utf-8",
        )


def append_entry(
    repo_root: Path,
    entry: DreamEntry,
    d: date | datetime | None = None,
) -> Path:
    """向当月 DREAMS 文件追加一条审查条目，返回文件路径。"""
    path = get_monthly_path(repo_root, d)
    _ensure_header(path, month_key(d))
    with path.open("a", encoding="utf-8") as f:
        f.write("\n" + entry.render())
    return path


def read_month(repo_root: Path, key: str) -> str:
    """读取某月 DREAMS 文件原文。"""
    path = Path(repo_root) / DREAMS_DIRNAME / f"{key}.md"
    if not path.is_file():
        return ""
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# 解析条目
# ---------------------------------------------------------------------------

_ENTRY_HEADER_RE = re.compile(
    r"^## (?P<ts>\S+) \[(?P<stage>[a-z]+)\] (?P<title>.*)$"
)


def parse_entries(content: str) -> list[DreamEntry]:
    """从月度文件解析出条目清单（用于审查界面数据接口）。

    解析顺序：header → 空行 → metadata 区（连续 "- " 开头）→ 空行 → body。
    metadata 区与 body 之间以空行分隔；body 持续到下一个 header 或文件末尾。
    """
    entries: list[DreamEntry] = []
    lines = content.splitlines()
    i = 0
    while i < len(lines):
        m = _ENTRY_HEADER_RE.match(lines[i])
        if not m:
            i += 1
            continue
        ts = m.group("ts")
        stage = m.group("stage")
        title = m.group("title").strip()
        i += 1
        # 跳过 header 后的空行
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        # 收集连续的 metadata 行（以 "- " 开头）
        metadata: dict[str, str] = {}
        while i < len(lines) and lines[i].startswith("- "):
            kv = lines[i][2:].split(":", 1)
            if len(kv) == 2:
                metadata[kv[0].strip()] = kv[1].strip()
            i += 1
        # 跳过 metadata 后的空行
        while i < len(lines) and lines[i].strip() == "":
            i += 1
        # 收集 body 直到下一个 header
        body_lines: list[str] = []
        while i < len(lines) and not _ENTRY_HEADER_RE.match(lines[i]):
            body_lines.append(lines[i])
            i += 1
        # 去掉 body 尾部空行
        while body_lines and body_lines[-1].strip() == "":
            body_lines.pop()
        entries.append(
            DreamEntry(
                timestamp=ts,
                stage=stage,
                title=title,
                body="\n".join(body_lines).strip(),
                metadata=metadata,
            )
        )
    return entries


# ---------------------------------------------------------------------------
# 归档压缩
# ---------------------------------------------------------------------------


def list_months(repo_root: Path) -> list[str]:
    """列出所有月度文件（含归档），按键升序。"""
    root = Path(repo_root) / DREAMS_DIRNAME
    if not root.is_dir():
        return []
    keys: set[str] = set()
    for f in root.glob("*.md"):
        if f.stem != "DREAMS":
            keys.add(f.stem)
    archive = root / ARCHIVE_DIRNAME
    if archive.is_dir():
        for f in archive.glob("*.md.gz"):
            keys.add(f.name.removesuffix(".md.gz"))
    return sorted(keys)


def archive_months(
    repo_root: Path,
    keep_keys: list[str] | None = None,
    *,
    keep_recent: int = 3,
) -> list[str]:
    """归档历史月度文件：将非保留月份压缩为 .gz 移入 archive/。

    keep_keys: 显式保留的月份键（不归档）。
    keep_recent: 额外保留最近 N 个月（默认 3）。
    返回已归档的月份键清单。
    """
    root = Path(repo_root) / DREAMS_DIRNAME
    if not root.is_dir():
        return []
    keep = set(keep_keys or [])
    all_keys = sorted(
        f.stem for f in root.glob("*.md") if f.stem != "DREAMS"
    )
    if not all_keys:
        return []
    # 保留最近 N 个月
    keep.update(all_keys[-keep_recent:])
    archive_dir = root / ARCHIVE_DIRNAME
    archive_dir.mkdir(parents=True, exist_ok=True)
    archived: list[str] = []
    for key in all_keys:
        if key in keep:
            continue
        src = root / f"{key}.md"
        if not src.is_file():
            continue
        dst = archive_dir / f"{key}.md.gz"
        with src.open("rb") as fin, gzip.open(dst, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        src.unlink()
        archived.append(key)
    return archived


def read_archived_month(repo_root: Path, key: str) -> str:
    """读取已归档月份的解压内容。"""
    path = get_archive_path(repo_root, key)
    if not path.is_file():
        return ""
    return gzip.decompress(path.read_bytes()).decode("utf-8")
