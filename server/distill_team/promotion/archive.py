"""升维管理模块 - 归档区管理。

对应 resource-harness 规则的"归档溯源"环节（经验提炼流程.md 步骤6）。

核心职责：
- 维护 archive.md（只增不删的归档区）
- 经验归档：将提炼完成的经验追加到 archive.md
- 触发失败案例记录：将规则未触发的失败案例追加到 archive.md
- 归档区压缩：根据引用次数压缩/删除低价值经验
- 解析归档区：列出已有经验和失败案例

文件编码：UTF-8（无 BOM），避免 PowerShell Out-File -Encoding UTF8 带 BOM 的问题。

archive.md 文件格式：
    # 归档区

    ## 经验归档

    ### E001：经验标题
    - 升维至：R005
    - 升维时间：2026-08-11
    - 原始错误案例：[完整记录]
    - 升维策略：推翻重来
    - 来源会话：6a76c1d0...

    ## 触发失败案例

    ### TF-001：案例标题
    - 对应规则：R003
    - 未触发原因：...
    - 发生时间：2026-08-11
    - 修补动作：...
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.models import ArchiveEntry, TriggerFailureCase

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# CompressionReport
# ---------------------------------------------------------------------------


@dataclass
class CompressionReport:
    """归档区压缩报告。"""

    total_entries: int  # 总条目数
    compressed_entries: int  # 压缩为摘要的条目数
    removed_entries: int  # 删除的条目数
    before_lines: int  # 压缩前行数
    after_lines: int  # 压缩后行数


# ---------------------------------------------------------------------------
# archive.md 文件结构常量
# ---------------------------------------------------------------------------

_ARCHIVE_TITLE = "# 归档区"
_EXPERIENCE_SECTION = "## 经验归档"
_FAILURE_SECTION = "## 触发失败案例"

# 经验条目标题正则：### E001：标题（使用全角冒号 ：）
_ENTRY_HEADER_RE = re.compile(r"^###\s+(E\d+)：(.*)$")
# 失败案例标题正则：### TF-001：标题
_CASE_HEADER_RE = re.compile(r"^###\s+(TF-\d+)：(.*)$")
# 字段行正则：- 字段名：值（非贪婪匹配 key，避免值中含全角冒号时错位）
_FIELD_RE = re.compile(r"^-\s+(.+?)：(.*)$")


# ---------------------------------------------------------------------------
# ArchiveManager
# ---------------------------------------------------------------------------


class ArchiveManager:
    """归档区管理器。

    用法：
        layout = adapter.get_layout(project_root)
        mgr = ArchiveManager(layout)
        # 归档经验
        entry_id = mgr.archive_experience(entry)
        # 记录触发失败案例
        case_id = mgr.record_trigger_failure(case)
        # 压缩归档区
        report = mgr.compress_archive()

    设计约束：
    - archive.md 只增不删（压缩操作除外）
    - 文件不存在时自动创建（含标题和段落标题）
    - 追加写入，不覆盖已有内容
    - ID 自动递增，通过扫描已有条目确定下一个 ID
    - 文件操作用 UTF-8 编码（无 BOM）
    """

    def __init__(self, layout: MemoryLayout) -> None:
        self._layout = layout

    # ------------------------------------------------------------------
    # 经验归档
    # ------------------------------------------------------------------

    def archive_experience(self, entry: ArchiveEntry) -> str:
        """归档经验到 archive.md。

        1. 生成 entry_id（如 E001，自动递增）
        2. 写入 archive.md 的"经验归档"段落
        3. 返回 entry_id

        若 entry.entry_id 为空或与已有 ID 冲突，自动分配下一个 ID。
        """
        path = self._layout.archive_path
        self._ensure_archive_file(path)

        existing = self.list_entries()
        existing_ids = {e.entry_id for e in existing}
        if not entry.entry_id or entry.entry_id in existing_ids:
            entry.entry_id = self._next_entry_id(existing)

        block = self._format_entry_block(entry)
        self._append_to_section(path, _EXPERIENCE_SECTION, block)
        logger.info("经验已归档: %s → %s", entry.entry_id, entry.promoted_to)
        return entry.entry_id

    def get_next_entry_id(self) -> str:
        """获取下一个经验 ID（E001, E002, ...）。"""
        existing = self.list_entries()
        return self._next_entry_id(existing)

    def get_next_failure_case_id(self) -> str:
        """获取下一个触发失败案例 ID（TF-001, TF-002, ...）。"""
        existing = self.list_failure_cases()
        return self._next_case_id(existing)

    def record_trigger_failure(self, case: TriggerFailureCase) -> str:
        """记录触发失败案例到 archive.md。

        1. 自动分配 case_id（若未提供或冲突）
        2. 写入 archive.md 的"触发失败案例"段落
        3. 返回 case_id
        """
        path = self._layout.archive_path
        self._ensure_archive_file(path)

        existing = self.list_failure_cases()
        existing_ids = {c.case_id for c in existing}
        if not case.case_id or case.case_id in existing_ids:
            case.case_id = self._next_case_id(existing)

        block = self._format_case_block(case)
        self._append_to_section(path, _FAILURE_SECTION, block)
        logger.info("触发失败案例已记录: %s → %s", case.case_id, case.rule_id)
        return case.case_id

    # ------------------------------------------------------------------
    # 解析归档区
    # ------------------------------------------------------------------

    def list_entries(self) -> list[ArchiveEntry]:
        """解析 archive.md，返回已有经验条目列表。"""
        path = self._layout.archive_path
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        return self._parse_entries(text)

    def list_failure_cases(self) -> list[TriggerFailureCase]:
        """解析 archive.md，返回已有触发失败案例列表。"""
        path = self._layout.archive_path
        if not path.exists():
            return []
        text = path.read_text(encoding="utf-8")
        return self._parse_failure_cases(text)

    # ------------------------------------------------------------------
    # 归档区压缩
    # ------------------------------------------------------------------

    def compress_archive(self) -> CompressionReport:
        """压缩归档区。

        对应经验提炼流程.md 的"归档区压缩"规则：
        - 被多条规则引用的旧经验 → 保留（高溯源价值）
        - 只被一条规则引用且该规则仍存在 → 压缩为摘要
        - 对应规则已被删除（无引用）→ 可安全删除

        通过扫描规则文件中是否提及 entry_id 判定引用次数。
        返回 CompressionReport（含压缩前后行数、压缩条目数）。
        """
        path = self._layout.archive_path
        if not path.exists():
            return CompressionReport(0, 0, 0, 0, 0)

        before_text = path.read_text(encoding="utf-8")
        before_lines = self._count_lines(before_text)

        entries = self.list_entries()
        cases = self.list_failure_cases()

        # 统计每条经验被引用次数（在规则文件中提及 entry_id）
        ref_counts = self._count_references(entries)

        # 分类：保留 / 压缩 / 删除
        keep_entries: list[ArchiveEntry] = []
        compress_entries: list[ArchiveEntry] = []
        remove_entries: list[ArchiveEntry] = []

        for entry in entries:
            count = ref_counts.get(entry.entry_id, 0)
            rule_exists = self._rule_exists(entry.promoted_to)
            if count >= 2:
                # 多规则引用 → 高溯源价值 → 保留
                keep_entries.append(entry)
            elif count == 1 and rule_exists:
                # 单规则引用且规则存在 → 压缩为摘要
                compress_entries.append(entry)
            else:
                # 无引用或规则已删除 → 安全删除
                remove_entries.append(entry)

        new_text = self._build_compressed_content(
            keep_entries, compress_entries, cases
        )
        after_lines = self._count_lines(new_text)

        # 写回（压缩是允许覆盖的特例）
        path.write_text(new_text, encoding="utf-8")

        report = CompressionReport(
            total_entries=len(entries),
            compressed_entries=len(compress_entries),
            removed_entries=len(remove_entries),
            before_lines=before_lines,
            after_lines=after_lines,
        )
        logger.info(
            "归档区压缩完成: 总计 %d 条, 压缩 %d 条, 删除 %d 条, 行数 %d → %d",
            report.total_entries,
            report.compressed_entries,
            report.removed_entries,
            report.before_lines,
            report.after_lines,
        )
        return report

    # ------------------------------------------------------------------
    # 内部：文件初始化与追加
    # ------------------------------------------------------------------

    def _ensure_archive_file(self, path: Path) -> None:
        """确保 archive.md 存在；不存在则创建含初始标题的空文件。"""
        if path.exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        skeleton = (
            f"{_ARCHIVE_TITLE}\n\n"
            f"{_EXPERIENCE_SECTION}\n\n"
            f"{_FAILURE_SECTION}\n"
        )
        path.write_text(skeleton, encoding="utf-8")
        logger.info("初始化归档区文件: %s", path)

    def _append_to_section(
        self, path: Path, section_header: str, block: str
    ) -> None:
        """将 block 追加到指定段落末尾。

        archive.md 只增不删：定位到段落标题，在下一个段落标题之前插入新条目。
        """
        text = path.read_text(encoding="utf-8")

        header_pattern = re.escape(section_header)
        match = re.search(rf"^{header_pattern}\s*$", text, re.MULTILINE)
        if not match:
            # 段落不存在，追加到文件末尾
            if text and not text.endswith("\n"):
                text += "\n"
            text += f"\n{section_header}\n\n{block}\n"
            path.write_text(text, encoding="utf-8")
            return

        section_start = match.end()

        # 查找下一个 ## 段落标题
        next_match = re.search(r"^##\s+\S", text[section_start:], re.MULTILINE)
        if next_match:
            section_end = section_start + next_match.start()
            suffix = text[section_end:]
        else:
            section_end = len(text)
            suffix = ""

        prefix = text[:section_end].rstrip()

        if suffix:
            new_text = prefix + "\n\n" + block + "\n\n" + suffix
        else:
            new_text = prefix + "\n\n" + block + "\n"

        path.write_text(new_text, encoding="utf-8")

    # ------------------------------------------------------------------
    # 内部：ID 生成
    # ------------------------------------------------------------------

    def _next_entry_id(self, existing: list[ArchiveEntry]) -> str:
        """根据已有条目确定下一个经验 ID（E001 格式，零填充至 3 位）。"""
        max_num = 0
        for e in existing:
            m = re.match(r"^E(\d+)$", e.entry_id)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        return f"E{max_num + 1:03d}"

    def _next_case_id(self, existing: list[TriggerFailureCase]) -> str:
        """根据已有案例确定下一个案例 ID（TF-001 格式，零填充至 3 位）。"""
        max_num = 0
        for c in existing:
            m = re.match(r"^TF-(\d+)$", c.case_id)
            if m:
                num = int(m.group(1))
                if num > max_num:
                    max_num = num
        return f"TF-{max_num + 1:03d}"

    # ------------------------------------------------------------------
    # 内部：格式化
    # ------------------------------------------------------------------

    def _format_entry_block(self, entry: ArchiveEntry) -> str:
        """格式化经验条目为 markdown 块。"""
        date_str = self._format_date(entry.promoted_at)
        lines = [
            f"### {entry.entry_id}：{entry.title}",
            f"- 升维至：{entry.promoted_to}",
            f"- 升维时间：{date_str}",
            f"- 原始错误案例：{entry.original_case}",
            f"- 升维策略：{entry.promotion_strategy}",
            f"- 来源会话：{entry.source_session}",
        ]
        return "\n".join(lines)

    def _format_compressed_entry_block(self, entry: ArchiveEntry) -> str:
        """格式化压缩后的经验条目（不含原始案例正文）。"""
        date_str = self._format_date(entry.promoted_at)
        lines = [
            f"### {entry.entry_id}：（已压缩）{entry.title}",
            f"- 升维至：{entry.promoted_to}",
            f"- 升维时间：{date_str}",
            f"- 升维策略：{entry.promotion_strategy}",
            f"- 来源会话：{entry.source_session}",
            "- [原始案例已压缩]",
        ]
        return "\n".join(lines)

    def _format_case_block(self, case: TriggerFailureCase) -> str:
        """格式化触发失败案例为 markdown 块。"""
        date_str = self._format_date(case.occurred_at)
        lines = [
            f"### {case.case_id}：规则 {case.rule_id} 未触发",
            f"- 对应规则：{case.rule_id}",
            f"- 未触发原因：{case.reason}",
            f"- 发生时间：{date_str}",
            f"- 修补动作：{case.fix_action}",
        ]
        return "\n".join(lines)

    # ------------------------------------------------------------------
    # 内部：解析
    # ------------------------------------------------------------------

    def _parse_entries(self, text: str) -> list[ArchiveEntry]:
        """解析经验条目列表。"""
        section_text = self._extract_section(text, _EXPERIENCE_SECTION)
        if not section_text:
            return []

        # 按 ### E001： 切分
        parts = re.split(r"^###\s+(E\d+)：", section_text, flags=re.MULTILINE)
        # parts = [pre_text, entry_id_1, body_1, entry_id_2, body_2, ...]

        entries: list[ArchiveEntry] = []
        i = 1
        while i < len(parts):
            entry_id = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            entry = self._parse_entry_body(entry_id, body)
            if entry:
                entries.append(entry)
            i += 2

        return entries

    def _parse_failure_cases(self, text: str) -> list[TriggerFailureCase]:
        """解析触发失败案例列表。"""
        section_text = self._extract_section(text, _FAILURE_SECTION)
        if not section_text:
            return []

        parts = re.split(r"^###\s+(TF-\d+)：", section_text, flags=re.MULTILINE)

        cases: list[TriggerFailureCase] = []
        i = 1
        while i < len(parts):
            case_id = parts[i]
            body = parts[i + 1] if i + 1 < len(parts) else ""
            case = self._parse_case_body(case_id, body)
            if case:
                cases.append(case)
            i += 2

        return cases

    def _parse_entry_body(
        self, entry_id: str, body: str
    ) -> ArchiveEntry | None:
        """解析单条经验条目。

        body 是 ### E001： 之后的文本，首行为标题，后续为字段行。
        原始错误案例字段可能跨多行（直到下一个 - 字段行）。
        """
        lines = body.split("\n")
        if not lines:
            return None

        title = lines[0].strip()

        fields: dict[str, str] = {}
        original_case_lines: list[str] = []
        in_original_case = False

        for line in lines[1:]:
            if in_original_case:
                # 原始案例多行收集：遇到下一个 - 字段行则结束
                if line.startswith("- "):
                    in_original_case = False
                    m = _FIELD_RE.match(line)
                    if m:
                        fields[m.group(1)] = m.group(2).strip()
                else:
                    original_case_lines.append(line)
                continue

            m = _FIELD_RE.match(line)
            if m:
                key = m.group(1)
                val = m.group(2).strip()
                if key == "原始错误案例":
                    in_original_case = True
                    original_case_lines.append(val)
                else:
                    fields[key] = val

        promoted_to = fields.get("升维至", "")
        promoted_at_str = fields.get("升维时间", "")
        promotion_strategy = fields.get("升维策略", "")
        source_session = fields.get("来源会话", "")
        original_case = "\n".join(original_case_lines).strip()

        # 处理压缩条目的标题前缀"（已压缩）"
        clean_title = title
        compressed_prefix = "（已压缩）"
        if clean_title.startswith(compressed_prefix):
            clean_title = clean_title[len(compressed_prefix):].strip()

        return ArchiveEntry(
            entry_id=entry_id,
            title=clean_title,
            promoted_to=promoted_to,
            promoted_at=self._parse_date(promoted_at_str),
            original_case=original_case,
            promotion_strategy=promotion_strategy,
            source_session=source_session,
        )

    def _parse_case_body(
        self, case_id: str, body: str
    ) -> TriggerFailureCase | None:
        """解析单条触发失败案例。"""
        lines = body.split("\n")
        if not lines:
            return None

        # 首行为标题（如"规则 R003 未触发"），实际字段从 - 行读取
        fields: dict[str, str] = {}
        for line in lines[1:]:
            m = _FIELD_RE.match(line)
            if m:
                fields[m.group(1)] = m.group(2).strip()

        rule_id = fields.get("对应规则", "")
        reason = fields.get("未触发原因", "")
        occurred_at_str = fields.get("发生时间", "")
        fix_action = fields.get("修补动作", "")

        return TriggerFailureCase(
            case_id=case_id,
            rule_id=rule_id,
            reason=reason,
            occurred_at=self._parse_date(occurred_at_str),
            fix_action=fix_action,
        )

    def _extract_section(self, text: str, section_header: str) -> str:
        """提取指定段落的内容（不含段落标题本身）。"""
        header_pattern = re.escape(section_header)
        match = re.search(rf"^{header_pattern}\s*$", text, re.MULTILINE)
        if not match:
            return ""

        section_start = match.end()

        next_match = re.search(r"^##\s+\S", text[section_start:], re.MULTILINE)
        if next_match:
            section_end = section_start + next_match.start()
        else:
            section_end = len(text)

        return text[section_start:section_end]

    def _parse_date(self, s: str) -> datetime:
        """解析日期字符串，失败返回 datetime.min。"""
        s = s.strip()
        if not s:
            return datetime.min
        # 尝试 ISO 格式（Python 3.11+ 支持日期only 字符串）
        try:
            return datetime.fromisoformat(s)
        except ValueError:
            pass
        # 尝试常见格式
        for fmt in ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(s, fmt)
            except ValueError:
                continue
        return datetime.min

    def _format_date(self, dt: datetime) -> str:
        """格式化日期为 YYYY-MM-DD；datetime.min 返回空串。"""
        if not dt or dt == datetime.min:
            return ""
        return dt.strftime("%Y-%m-%d")

    # ------------------------------------------------------------------
    # 内部：压缩辅助
    # ------------------------------------------------------------------

    def _count_references(
        self, entries: list[ArchiveEntry]
    ) -> dict[str, int]:
        """统计每条经验被规则文件引用的次数。

        扫描 project_rules_dir 和 global_rules_dir 下的规则文件，
        检查每个 entry_id（如 E001）在规则文件正文中出现的次数。
        """
        counts: dict[str, int] = {e.entry_id: 0 for e in entries}
        if not entries:
            return counts

        rule_files = self._collect_rule_files()
        for rule_file in rule_files:
            try:
                content = rule_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            for entry_id in counts:
                # 使用单词边界匹配，避免 E001 匹配到 E0010
                if re.search(rf"\b{re.escape(entry_id)}\b", content):
                    counts[entry_id] += 1

        return counts

    def _rule_exists(self, rule_id: str) -> bool:
        """检查指定 ID 的规则是否存在于规则文件中。"""
        if not rule_id:
            return False

        rule_files = self._collect_rule_files()
        for rule_file in rule_files:
            try:
                content = rule_file.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            if re.search(rf"\b{re.escape(rule_id)}\b", content):
                return True
        return False

    def _collect_rule_files(self) -> list[Path]:
        """收集所有规则文件路径（项目级 + 用户全局）。"""
        files: list[Path] = []
        ext = self._layout.rules_file_ext or ".md"

        for rule_dir in (
            self._layout.project_rules_dir,
            self._layout.global_rules_dir,
        ):
            if not rule_dir.exists():
                continue
            for f in rule_dir.rglob(f"*{ext}"):
                if f.is_file():
                    files.append(f)

        return files

    def _build_compressed_content(
        self,
        keep_entries: list[ArchiveEntry],
        compress_entries: list[ArchiveEntry],
        cases: list[TriggerFailureCase],
    ) -> str:
        """构建压缩后的 archive.md 完整内容。"""
        lines: list[str] = [_ARCHIVE_TITLE, "", _EXPERIENCE_SECTION, ""]

        for entry in keep_entries:
            lines.append(self._format_entry_block(entry))
            lines.append("")

        for entry in compress_entries:
            lines.append(self._format_compressed_entry_block(entry))
            lines.append("")

        lines.append(_FAILURE_SECTION)
        lines.append("")

        for case in cases:
            lines.append(self._format_case_block(case))
            lines.append("")

        # 末尾保留单个换行
        return "\n".join(lines).rstrip() + "\n"

    def _count_lines(self, text: str) -> int:
        """统计文本行数。"""
        if not text:
            return 0
        return text.count("\n") + (0 if text.endswith("\n") else 1)


__all__ = ["ArchiveManager", "CompressionReport"]
