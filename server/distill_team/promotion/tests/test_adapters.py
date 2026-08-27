"""coding 软件适配器层测试。

覆盖 Trae / Cursor / Claude Code / Windsurf / Cline 五个适配器 + 工厂。
测试隔离：用 tmp_path 创建临时项目目录，不污染真实 home 目录。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import (
    MemoryLayout,
    RuleEntry,
)
from server.distill_team.promotion.adapters.claude_code import ClaudeCodeAdapter
from server.distill_team.promotion.adapters.cline import ClineAdapter
from server.distill_team.promotion.adapters.cursor import CursorAdapter
from server.distill_team.promotion.adapters.factory import (
    create_adapter,
    list_adapters,
)
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.adapters.windsurf import WindsurfAdapter


# ---------------------------------------------------------------------------
# TraeAdapter
# ---------------------------------------------------------------------------


class TestTraeAdapter:
    """Trae IDE 适配器测试。"""

    def test_detect_with_trae_dir(self, tmp_path: Path):
        """项目根有 .trae/ 目录 → 命中检测。"""
        (tmp_path / ".trae").mkdir()
        adapter = TraeAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_without_trae_dir(self, tmp_path: Path):
        """项目根无 .trae/ 目录 → 不命中。"""
        adapter = TraeAdapter()
        assert adapter.detect(tmp_path) is False

    def test_detect_with_file_not_dir(self, tmp_path: Path):
        """.trae 是文件不是目录 → 不命中（is_dir 为 False）。"""
        (tmp_path / ".trae").write_text("not a dir")
        adapter = TraeAdapter()
        assert adapter.detect(tmp_path) is False

    def test_get_layout(self, tmp_path: Path):
        """get_layout 返回 MemoryLayout，project_rules_dir 指向 .trae/rules。"""
        adapter = TraeAdapter()
        layout = adapter.get_layout(tmp_path)
        assert isinstance(layout, MemoryLayout)
        assert layout.project_rules_dir == tmp_path / ".trae" / "rules"
        assert layout.rules_file_ext == ".md"
        assert layout.supports_frontmatter is True
        assert layout.hotspot_section_marker == "## 热点规则"

    def test_parse_existing_rules_empty_dir(self, tmp_path: Path):
        """空目录 → 返回空列表。"""
        rules_dir = tmp_path / ".trae" / "rules"
        rules_dir.mkdir(parents=True)
        adapter = TraeAdapter()
        assert adapter.parse_existing_rules(rules_dir) == []

    def test_parse_existing_rules_nonexistent_dir(self, tmp_path: Path):
        """目录不存在 → 返回空列表（边界）。"""
        adapter = TraeAdapter()
        assert adapter.parse_existing_rules(tmp_path / "no-such-dir") == []

    def test_parse_existing_rules_with_frontmatter(self, tmp_path: Path):
        """解析含 frontmatter 的规则文件。"""
        rules_dir = tmp_path / ".trae" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "R001.md").write_text(
            "---\nid: R001\ntitle: 测试规则一\ncategory: 后端\n---\n# 规则体\n内容一",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        entries = adapter.parse_existing_rules(rules_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.rule_id == "R001"
        assert e.title == "测试规则一"
        assert e.category == "后端"
        assert "内容一" in e.content
        assert e.frontmatter.get("id") == "R001"

    def test_parse_existing_rules_without_frontmatter(self, tmp_path: Path):
        """无 frontmatter 时用文件名作 rule_id。"""
        rules_dir = tmp_path / ".trae" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "fallback.md").write_text(
            "# 由标题推导\n正文内容", encoding="utf-8"
        )
        adapter = TraeAdapter()
        entries = adapter.parse_existing_rules(rules_dir)
        assert len(entries) == 1
        e = entries[0]
        assert e.rule_id == "fallback"
        assert e.title == "由标题推导"

    def test_parse_existing_rules_skips_archive_and_graph(self, tmp_path: Path):
        """archive.md / graph.md 应被跳过。"""
        rules_dir = tmp_path / ".trae" / "rules"
        rules_dir.mkdir(parents=True)
        (rules_dir / "archive.md").write_text("# 归档", encoding="utf-8")
        (rules_dir / "graph.md").write_text("# 图谱", encoding="utf-8")
        (rules_dir / "R001.md").write_text("---\nid: R001\n---\nbody", encoding="utf-8")
        adapter = TraeAdapter()
        entries = adapter.parse_existing_rules(rules_dir)
        assert len(entries) == 1
        assert entries[0].rule_id == "R001"

    def test_write_rule_creates_file(self, tmp_path: Path):
        """write_rule 创建规则文件，返回路径。"""
        rules_dir = tmp_path / ".trae" / "rules"
        adapter = TraeAdapter()
        path = adapter.write_rule(
            rules_dir=rules_dir,
            rule_id="R100",
            title="新规则",
            content="正文内容",
        )
        assert path == rules_dir / "R100.md"
        assert path.is_file()
        text = path.read_text(encoding="utf-8")
        assert "id: R100" in text
        assert "title: 新规则" in text
        assert "正文内容" in text

    def test_write_rule_with_extra_frontmatter(self, tmp_path: Path):
        """write_rule 支持追加额外 frontmatter 字段。"""
        rules_dir = tmp_path / ".trae" / "rules"
        adapter = TraeAdapter()
        path = adapter.write_rule(
            rules_dir=rules_dir,
            rule_id="R101",
            title="扩展",
            content="c",
            frontmatter={"category": "前端", "priority": "high"},
        )
        text = path.read_text(encoding="utf-8")
        assert "category: 前端" in text
        assert "priority: high" in text

    def test_read_hotspot_rules_no_file(self, tmp_path: Path):
        """user_profile.md 不存在 → 空列表。"""
        adapter = TraeAdapter()
        assert adapter.read_hotspot_rules(tmp_path / "missing.md") == []

    def test_read_hotspot_rules_parses_entries(self, tmp_path: Path):
        """读取热点规则区的多条规则。"""
        profile = tmp_path / "user_profile.md"
        profile.write_text(
            "# 用户配置\n\n"
            "## 热点规则（2/5）\n\n"
            "- **R001**：第一条热点规则\n"
            "- **R002**：第二条热点规则\n\n"
            "## 其他段落\n",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        entries = adapter.read_hotspot_rules(profile)
        assert len(entries) == 2
        assert entries[0].rule_id == "R001"
        assert entries[0].content == "第一条热点规则"
        assert entries[1].rule_id == "R002"

    def test_read_hotspot_rules_stops_at_next_section(self, tmp_path: Path):
        """热点规则区遇到下一个 ## 段落应停止解析。"""
        profile = tmp_path / "user_profile.md"
        profile.write_text(
            "## 热点规则（1/5）\n\n"
            "- **R001**：规则一\n\n"
            "## 其他\n",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        entries = adapter.read_hotspot_rules(profile)
        assert len(entries) == 1

    def test_write_hotspot_rule_creates_section(self, tmp_path: Path):
        """文件无热点规则区 → 创建新区。"""
        profile = tmp_path / "sub" / "user_profile.md"
        adapter = TraeAdapter()
        adapter.write_hotspot_rule(
            user_profile_path=profile,
            rule_id="R001",
            title="T1",
            content="新热点",
        )
        assert profile.is_file()
        text = profile.read_text(encoding="utf-8")
        assert "## 热点规则" in text
        assert "- **R001**：新热点" in text

    def test_write_hotspot_rule_appends_to_existing_section(self, tmp_path: Path):
        """已有热点规则区 → 追加到区内末尾。"""
        profile = tmp_path / "user_profile.md"
        profile.write_text(
            "# 配置\n\n"
            "## 热点规则（1/5）\n\n"
            "- **R001**：第一条\n\n"
            "## 其他段落\n",
            encoding="utf-8",
        )
        adapter = TraeAdapter()
        adapter.write_hotspot_rule(
            user_profile_path=profile,
            rule_id="R002",
            title="T2",
            content="第二条",
        )
        text = profile.read_text(encoding="utf-8")
        assert "- **R001**：第一条" in text
        assert "- **R002**：第二条" in text
        # 新规则应在"## 其他段落"之前
        pos_new = text.index("- **R002**")
        pos_other = text.index("## 其他段落")
        assert pos_new < pos_other


# ---------------------------------------------------------------------------
# CursorAdapter
# ---------------------------------------------------------------------------


class TestCursorAdapter:
    """Cursor IDE 适配器测试。"""

    def test_detect_with_cursor_dir(self, tmp_path: Path):
        (tmp_path / ".cursor").mkdir()
        adapter = CursorAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_with_legacy_cursorrules_file(self, tmp_path: Path):
        """旧版 .cursorrules 文件也命中。"""
        (tmp_path / ".cursorrules").write_text("legacy", encoding="utf-8")
        adapter = CursorAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_without_cursor(self, tmp_path: Path):
        adapter = CursorAdapter()
        assert adapter.detect(tmp_path) is False

    def test_get_layout_uses_mdc_extension(self, tmp_path: Path):
        """Cursor 规则文件扩展名应为 .mdc。"""
        adapter = CursorAdapter()
        layout = adapter.get_layout(tmp_path)
        assert layout.project_rules_dir == tmp_path / ".cursor" / "rules"
        assert layout.rules_file_ext == ".mdc"
        assert layout.supports_frontmatter is True

    def test_get_layout_global_paths(self, tmp_path: Path):
        """验证 global_rules_dir / archive_path / graph_path 的结构。"""
        adapter = CursorAdapter()
        layout = adapter.get_layout(tmp_path)
        # 不依赖具体 home 路径，验证相对结构
        assert layout.global_rules_dir.name == "rules"
        assert layout.global_rules_dir.parent.name == ".cursor"
        assert layout.archive_path.name == "archive.md"
        assert layout.graph_path.name == "graph.md"


# ---------------------------------------------------------------------------
# ClaudeCodeAdapter
# ---------------------------------------------------------------------------


class TestClaudeCodeAdapter:
    """Claude Code CLI 适配器测试。"""

    def test_detect_with_claude_dir(self, tmp_path: Path):
        (tmp_path / ".claude").mkdir()
        adapter = ClaudeCodeAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_with_claude_md_file(self, tmp_path: Path):
        """CLAUDE.md 文件存在也命中。"""
        (tmp_path / "CLAUDE.md").write_text("# claude", encoding="utf-8")
        adapter = ClaudeCodeAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_without_claude(self, tmp_path: Path):
        adapter = ClaudeCodeAdapter()
        assert adapter.detect(tmp_path) is False

    def test_get_layout(self, tmp_path: Path):
        adapter = ClaudeCodeAdapter()
        layout = adapter.get_layout(tmp_path)
        assert layout.project_rules_dir == tmp_path / ".claude" / "rules"
        assert layout.rules_file_ext == ".md"
        assert layout.user_profile_path.name == "CLAUDE.md"


# ---------------------------------------------------------------------------
# WindsurfAdapter
# ---------------------------------------------------------------------------


class TestWindsurfAdapter:
    """Windsurf IDE 适配器测试。"""

    def test_detect_with_windsurf_dir(self, tmp_path: Path):
        (tmp_path / ".windsurf").mkdir()
        adapter = WindsurfAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_with_windsurfrules_file(self, tmp_path: Path):
        """.windsurfrules 单文件命中。"""
        (tmp_path / ".windsurfrules").write_text("rules", encoding="utf-8")
        adapter = WindsurfAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_without_windsurf(self, tmp_path: Path):
        adapter = WindsurfAdapter()
        assert adapter.detect(tmp_path) is False

    def test_get_layout(self, tmp_path: Path):
        adapter = WindsurfAdapter()
        layout = adapter.get_layout(tmp_path)
        assert layout.project_rules_dir == tmp_path / ".windsurf" / "rules"
        assert layout.rules_file_ext == ".md"
        assert layout.user_profile_path.name == "windsurf_global_rules.md"


# ---------------------------------------------------------------------------
# ClineAdapter
# ---------------------------------------------------------------------------


class TestClineAdapter:
    """Cline VS Code 扩展适配器测试。"""

    def test_detect_with_clinerules_dir(self, tmp_path: Path):
        (tmp_path / ".clinerules").mkdir()
        adapter = ClineAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_with_clinerules_file(self, tmp_path: Path):
        """.clinerules 单文件命中。"""
        (tmp_path / ".clinerules").write_text("rules", encoding="utf-8")
        adapter = ClineAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_with_memory_bank(self, tmp_path: Path):
        """memory-bank 目录也命中。"""
        (tmp_path / "memory-bank").mkdir()
        adapter = ClineAdapter()
        assert adapter.detect(tmp_path) is True

    def test_detect_without_cline(self, tmp_path: Path):
        adapter = ClineAdapter()
        assert adapter.detect(tmp_path) is False

    def test_get_layout(self, tmp_path: Path):
        adapter = ClineAdapter()
        layout = adapter.get_layout(tmp_path)
        assert layout.project_rules_dir == tmp_path / ".clinerules"
        assert layout.rules_file_ext == ".md"
        assert layout.user_profile_path.name == "cline_global_rules.md"

    def test_get_layout_global_rules_under_documents(self, tmp_path: Path):
        """验证 global_rules_dir 在 Documents/Cline/Rules 下。"""
        adapter = ClineAdapter()
        layout = adapter.get_layout(tmp_path)
        assert layout.global_rules_dir.parent.name == "Cline"
        assert layout.global_rules_dir.name == "Rules"


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


class TestFactory:
    """适配器工厂测试。"""

    def test_create_adapter_by_name_trae(self):
        """显式指定 trae → 返回 TraeAdapter。"""
        adapter = create_adapter(software="trae")
        assert isinstance(adapter, TraeAdapter)
        assert adapter.name == "trae"

    def test_create_adapter_by_name_cursor(self):
        adapter = create_adapter(software="cursor")
        assert isinstance(adapter, CursorAdapter)

    def test_create_adapter_name_case_insensitive(self):
        """软件名大小写不敏感。"""
        adapter = create_adapter(software="TRAE")
        assert isinstance(adapter, TraeAdapter)
        adapter = create_adapter(software="Cursor")
        assert isinstance(adapter, CursorAdapter)

    def test_create_adapter_name_strips_whitespace(self):
        """软件名前后空格被忽略。"""
        adapter = create_adapter(software="  trae  ")
        assert isinstance(adapter, TraeAdapter)

    def test_create_adapter_auto_detect_trae(self, tmp_path: Path):
        """software=None 自动检测，项目根有 .trae → 返回 TraeAdapter。"""
        (tmp_path / ".trae").mkdir()
        adapter = create_adapter(software=None, project_root=tmp_path)
        assert isinstance(adapter, TraeAdapter)

    def test_create_adapter_auto_detect_cursor(self, tmp_path: Path):
        """自动检测 Cursor。"""
        (tmp_path / ".cursor").mkdir()
        adapter = create_adapter(software=None, project_root=tmp_path)
        assert isinstance(adapter, CursorAdapter)

    def test_create_adapter_auto_detect_fallback_to_trae(self, tmp_path: Path):
        """无任何已知软件特征 → 默认回退到 TraeAdapter。"""
        adapter = create_adapter(software=None, project_root=tmp_path)
        assert isinstance(adapter, TraeAdapter)

    def test_create_adapter_unknown_software_raises(self):
        """未知软件名 → ValueError。"""
        with pytest.raises(ValueError, match="未知的 coding 软件"):
            create_adapter(software="unknown-ide")

    def test_create_adapter_unknown_software_lists_supported(self):
        """ValueError 信息中应列出支持的软件。"""
        with pytest.raises(ValueError) as exc_info:
            create_adapter(software="ghost")
        msg = str(exc_info.value)
        assert "trae" in msg
        assert "cursor" in msg

    def test_list_adapters_returns_all(self):
        """list_adapters 返回 5 个已注册适配器。"""
        adapters = list_adapters()
        assert len(adapters) == 5
        names = {a.name for a in adapters}
        assert names == {"trae", "cursor", "claude_code", "windsurf", "cline"}

    def test_list_adapters_returns_copies(self):
        """list_adapters 返回新列表，修改不影响内部注册表。"""
        adapters = list_adapters()
        adapters.clear()
        assert len(list_adapters()) == 5

    def test_registered_adapters_priority_trae_first(self):
        """Trae 应在注册表首位（检测优先级最高）。"""
        adapters = list_adapters()
        assert adapters[0].name == "trae"


# ---------------------------------------------------------------------------
# 边界场景：跨适配器一致性
# ---------------------------------------------------------------------------


class TestAdapterConsistency:
    """跨适配器行为一致性测试。"""

    def test_all_adapters_implement_required_methods(self):
        """所有适配器必须实现基类的抽象方法。"""
        adapters = list_adapters()
        for adapter in adapters:
            assert hasattr(adapter, "detect")
            assert hasattr(adapter, "get_layout")
            assert hasattr(adapter, "parse_existing_rules")
            assert hasattr(adapter, "write_rule")
            assert hasattr(adapter, "read_hotspot_rules")
            assert hasattr(adapter, "write_hotspot_rule")

    def test_all_adapters_have_distinct_names(self):
        """适配器 name 字段应唯一。"""
        adapters = list_adapters()
        names = [a.name for a in adapters]
        assert len(names) == len(set(names))

    def test_all_layouts_have_hotspot_marker(self, tmp_path: Path):
        """所有适配器的 MemoryLayout 都应配置 hotspot_section_marker。"""
        adapters = list_adapters()
        for adapter in adapters:
            layout = adapter.get_layout(tmp_path)
            assert layout.hotspot_section_marker == "## 热点规则"
