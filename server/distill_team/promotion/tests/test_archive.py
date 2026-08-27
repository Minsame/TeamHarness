"""归档模块测试。

覆盖经验归档 / ID 递增 / 失败案例记录 / 解析归档 / 压缩 / 自动创建。
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import MemoryLayout
from server.distill_team.promotion.archive import ArchiveManager, CompressionReport
from server.distill_team.promotion.models import ArchiveEntry, TriggerFailureCase


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _make_layout(tmp_path: Path) -> MemoryLayout:
    """用 tmp_path 构造 MemoryLayout。"""
    return MemoryLayout(
        project_rules_dir=tmp_path / "project_rules",
        global_rules_dir=tmp_path / "global_rules",
        user_profile_path=tmp_path / "profile.md",
        archive_path=tmp_path / "archive.md",
        graph_path=tmp_path / "graph.md",
        cross_project_root=tmp_path / "cross",
        rules_file_ext=".md",
        supports_frontmatter=True,
        hotspot_section_marker="## 热点规则",
    )


def _make_entry(
    entry_id: str = "",
    title: str = "测试经验",
    promoted_to: str = "R005",
    original_case: str = "原始错误案例文本",
    promotion_strategy: str = "推翻重来",
    source_session: str = "session-abc",
) -> ArchiveEntry:
    return ArchiveEntry(
        entry_id=entry_id,
        title=title,
        promoted_to=promoted_to,
        promoted_at=datetime(2026, 8, 12),
        original_case=original_case,
        promotion_strategy=promotion_strategy,
        source_session=source_session,
    )


def _make_case(
    case_id: str = "",
    rule_id: str = "R003",
    reason: str = "规则未在代码中触发",
    fix_action: str = "更新规则的触发条件描述",
) -> TriggerFailureCase:
    return TriggerFailureCase(
        case_id=case_id,
        rule_id=rule_id,
        reason=reason,
        occurred_at=datetime(2026, 8, 12),
        fix_action=fix_action,
    )


# ---------------------------------------------------------------------------
# 文件不存在时自动创建
# ---------------------------------------------------------------------------


class TestFileNotExists:
    """archive.md 不存在时自动创建。"""

    def test_archive_experience_creates_file(self, tmp_path: Path):
        """归档时 archive.md 不存在 → 自动创建。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)

        assert not layout.archive_path.exists()
        entry_id = mgr.archive_experience(_make_entry())
        assert layout.archive_path.is_file()
        assert entry_id == "E001"

    def test_created_file_has_skeleton(self, tmp_path: Path):
        """自动创建的 archive.md 含标题和段落骨架。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry())

        text = layout.archive_path.read_text(encoding="utf-8")
        assert "# 归档区" in text
        assert "## 经验归档" in text
        assert "## 触发失败案例" in text

    def test_list_entries_empty_when_no_file(self, tmp_path: Path):
        """archive.md 不存在时 list_entries 返回空列表。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        assert mgr.list_entries() == []
        assert mgr.list_failure_cases() == []

    def test_record_failure_creates_file(self, tmp_path: Path):
        """记录失败案例时 archive.md 不存在 → 自动创建。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        case_id = mgr.record_trigger_failure(_make_case())
        assert layout.archive_path.is_file()
        assert case_id == "TF-001"


# ---------------------------------------------------------------------------
# 经验归档
# ---------------------------------------------------------------------------


class TestArchiveExperience:
    """archive_experience 归档经验。"""

    def test_first_entry_gets_E001(self, tmp_path: Path):
        """首条经验 → E001。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        entry_id = mgr.archive_experience(_make_entry())
        assert entry_id == "E001"

    def test_second_entry_gets_E002(self, tmp_path: Path):
        """第二条经验 → E002。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry(title="第一条"))
        entry_id = mgr.archive_experience(_make_entry(title="第二条"))
        assert entry_id == "E002"

    def test_explicit_id_preserved_if_unique(self, tmp_path: Path):
        """显式指定且唯一的 entry_id 被保留。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        entry_id = mgr.archive_experience(_make_entry(entry_id="E100"))
        assert entry_id == "E100"

    def test_conflicting_id_reassigned(self, tmp_path: Path):
        """entry_id 冲突时自动分配新 ID。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry(entry_id="E001"))
        # 再次用 E001 → 应被重新分配为 E002
        entry_id = mgr.archive_experience(_make_entry(entry_id="E001"))
        assert entry_id == "E002"

    def test_entry_block_format(self, tmp_path: Path):
        """归档后的条目含完整字段。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(
            _make_entry(
                title="DB session 泄漏",
                promoted_to="R005",
                original_case="服务端点返回 503",
                promotion_strategy="推翻重来",
                source_session="sess-xyz",
            )
        )
        text = layout.archive_path.read_text(encoding="utf-8")
        assert "### E001：DB session 泄漏" in text
        assert "- 升维至：R005" in text
        assert "- 升维时间：2026-08-12" in text
        assert "- 原始错误案例：服务端点返回 503" in text
        assert "- 升维策略：推翻重来" in text
        assert "- 来源会话：sess-xyz" in text

    def test_multiple_entries_appended(self, tmp_path: Path):
        """多条经验按顺序追加，互不影响。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        for i in range(3):
            mgr.archive_experience(_make_entry(title=f"经验{i}"))
        entries = mgr.list_entries()
        assert len(entries) == 3
        assert entries[0].entry_id == "E001"
        assert entries[1].entry_id == "E002"
        assert entries[2].entry_id == "E003"


# ---------------------------------------------------------------------------
# ID 自动递增
# ---------------------------------------------------------------------------


class TestGetNextEntryId:
    """get_next_entry_id ID 自动递增。"""

    def test_empty_returns_E001(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        assert mgr.get_next_entry_id() == "E001"

    def test_after_one_returns_E002(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry())
        assert mgr.get_next_entry_id() == "E002"

    def test_skips_gaps(self, tmp_path: Path):
        """ID 不连续时取 max+1。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        # 直接写入含 E001 和 E003 的 archive.md
        layout.archive_path.parent.mkdir(parents=True, exist_ok=True)
        layout.archive_path.write_text(
            "# 归档区\n\n## 经验归档\n\n"
            "### E001：A\n- 升维至：R1\n- 升维时间：\n- 原始错误案例：x\n- 升维策略：\n- 来源会话：\n\n"
            "### E003：B\n- 升维至：R3\n- 升维时间：\n- 原始错误案例：y\n- 升维策略：\n- 来源会话：\n\n"
            "## 触发失败案例\n",
            encoding="utf-8",
        )
        # 已有 E001, E003 → 下一个应是 E004
        assert mgr.get_next_entry_id() == "E004"

    def test_next_failure_case_id(self, tmp_path: Path):
        """触发失败案例 ID（TF-001）。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        assert mgr.get_next_failure_case_id() == "TF-001"
        mgr.record_trigger_failure(_make_case())
        assert mgr.get_next_failure_case_id() == "TF-002"


# ---------------------------------------------------------------------------
# 触发失败案例
# ---------------------------------------------------------------------------


class TestRecordTriggerFailure:
    """record_trigger_failure 记录触发失败案例。"""

    def test_first_case_gets_TF_001(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        case_id = mgr.record_trigger_failure(_make_case())
        assert case_id == "TF-001"

    def test_case_block_format(self, tmp_path: Path):
        """失败案例块格式正确。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.record_trigger_failure(
            _make_case(
                rule_id="R003",
                reason="规则未在代码中触发",
                fix_action="更新触发条件",
            )
        )
        text = layout.archive_path.read_text(encoding="utf-8")
        assert "### TF-001：规则 R003 未触发" in text
        assert "- 对应规则：R003" in text
        assert "- 未触发原因：规则未在代码中触发" in text
        assert "- 修补动作：更新触发条件" in text

    def test_multiple_cases_appended(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        for i in range(2):
            mgr.record_trigger_failure(_make_case(rule_id=f"R00{i}"))
        cases = mgr.list_failure_cases()
        assert len(cases) == 2
        assert cases[0].case_id == "TF-001"
        assert cases[1].case_id == "TF-002"


# ---------------------------------------------------------------------------
# 解析归档区
# ---------------------------------------------------------------------------


class TestListEntries:
    """list_entries 解析归档区。"""

    def test_list_empty(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        assert mgr.list_entries() == []

    def test_list_single_entry(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(
            _make_entry(
                title="测试经验",
                promoted_to="R005",
                original_case="案例文本",
                promotion_strategy="推翻重来",
                source_session="sess-1",
            )
        )
        entries = mgr.list_entries()
        assert len(entries) == 1
        e = entries[0]
        assert e.entry_id == "E001"
        assert e.title == "测试经验"
        assert e.promoted_to == "R005"
        assert e.original_case == "案例文本"
        assert e.promotion_strategy == "推翻重来"
        assert e.source_session == "sess-1"

    def test_list_multiple_entries(self, tmp_path: Path):
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry(title="A", promoted_to="R1"))
        mgr.archive_experience(_make_entry(title="B", promoted_to="R2"))
        mgr.archive_experience(_make_entry(title="C", promoted_to="R3"))
        entries = mgr.list_entries()
        assert len(entries) == 3
        titles = [e.title for e in entries]
        assert titles == ["A", "B", "C"]

    def test_list_failure_cases(self, tmp_path: Path):
        """list_failure_cases 解析失败案例。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.record_trigger_failure(_make_case(rule_id="R1", reason="r1"))
        mgr.record_trigger_failure(_make_case(rule_id="R2", reason="r2"))
        cases = mgr.list_failure_cases()
        assert len(cases) == 2
        assert cases[0].rule_id == "R1"
        assert cases[0].reason == "r1"
        assert cases[1].rule_id == "R2"

    def test_parse_original_case_multiline(self, tmp_path: Path):
        """原始错误案例跨多行时正确解析。"""
        layout = _make_layout(tmp_path)
        layout.archive_path.parent.mkdir(parents=True, exist_ok=True)
        layout.archive_path.write_text(
            "# 归档区\n\n## 经验归档\n\n"
            "### E001：标题\n"
            "- 升维至：R005\n"
            "- 升维时间：2026-08-12\n"
            "- 原始错误案例：第一行\n第二行\n第三行\n"
            "- 升维策略：推翻重来\n"
            "- 来源会话：sess\n\n"
            "## 触发失败案例\n",
            encoding="utf-8",
        )
        mgr = ArchiveManager(layout)
        entries = mgr.list_entries()
        assert len(entries) == 1
        assert "第一行" in entries[0].original_case
        assert "第二行" in entries[0].original_case
        assert "第三行" in entries[0].original_case


# ---------------------------------------------------------------------------
# 归档压缩
# ---------------------------------------------------------------------------


class TestCompressArchive:
    """compress_archive 归档区压缩。"""

    def test_compress_empty_archive(self, tmp_path: Path):
        """空归档压缩 → 空报告。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        # archive.md 不存在 → 返回空报告
        report = mgr.compress_archive()
        assert isinstance(report, CompressionReport)
        assert report.total_entries == 0

    def test_compress_keeps_multi_referenced(self, tmp_path: Path):
        """被 2+ 规则引用的经验 → 保留。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        # 写入 E001
        mgr.archive_experience(_make_entry(entry_id="E001", promoted_to="R001"))
        # 在 2 个规则文件中引用 E001
        layout.project_rules_dir.mkdir(parents=True)
        (layout.project_rules_dir / "R001.md").write_text(
            "---\nid: R001\n---\nE001 被引用", encoding="utf-8"
        )
        (layout.project_rules_dir / "R002.md").write_text(
            "---\nid: R002\n---\n引用 E001 经验", encoding="utf-8"
        )
        report = mgr.compress_archive()
        assert report.total_entries == 1
        # E001 被引用 2 次 → 保留（不压缩不删除）
        entries = mgr.list_entries()
        assert len(entries) == 1
        assert "（已压缩）" not in entries[0].title

    def test_compress_single_referenced(self, tmp_path: Path):
        """被 1 个规则引用且规则存在 → 压缩为摘要。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(
            _make_entry(entry_id="E001", promoted_to="R001", original_case="详细案例")
        )
        # 在 1 个规则文件中引用 E001
        layout.project_rules_dir.mkdir(parents=True)
        (layout.project_rules_dir / "R001.md").write_text(
            "---\nid: R001\n---\n引用 E001", encoding="utf-8"
        )
        report = mgr.compress_archive()
        assert report.compressed_entries == 1
        # 解析时 _parse_entry_body 会去除"（已压缩）"前缀，故验证写入文件的标题含前缀
        raw_text = layout.archive_path.read_text(encoding="utf-8")
        assert "（已压缩）" in raw_text
        # 解析后标题不含前缀（被 _parse_entry_body 清理）
        entries = mgr.list_entries()
        assert len(entries) == 1
        assert "（已压缩）" not in entries[0].title
        # 原始案例正文应被压缩（替换为占位符或清空）
        assert "详细案例" not in entries[0].original_case

    def test_compress_removes_unreferenced(self, tmp_path: Path):
        """无引用的经验 → 删除。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(
            _make_entry(entry_id="E001", promoted_to="R999", original_case="无引用案例")
        )
        # 不创建任何规则文件 → E001 无引用
        report = mgr.compress_archive()
        assert report.removed_entries == 1
        # 压缩后归档区无该条目
        entries = mgr.list_entries()
        assert len(entries) == 0

    def test_compress_report_has_line_counts(self, tmp_path: Path):
        """压缩报告含 before_lines / after_lines。"""
        layout = _make_layout(tmp_path)
        mgr = ArchiveManager(layout)
        mgr.archive_experience(_make_entry(entry_id="E001"))
        report = mgr.compress_archive()
        assert report.before_lines > 0
        assert report.after_lines > 0
        assert isinstance(report.before_lines, int)
        assert isinstance(report.after_lines, int)
