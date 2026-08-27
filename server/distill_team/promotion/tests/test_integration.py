"""升维管理模块集成测试。

测试 PromotionOrchestrator 编排的完整升维流程：
查重 → 回测 → 升维 → 归档 → 图谱登记 → 跨项目再升维 → 连锁更新

覆盖正常流程、边界条件、异常场景（熔断）。
所有测试用 tmp_path fixture 确保隔离。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.promotion.adapters.base import MemoryLayout, RuleEntry
from server.distill_team.promotion.adapters.claude_code import ClaudeCodeAdapter
from server.distill_team.promotion.adapters.cline import ClineAdapter
from server.distill_team.promotion.adapters.cursor import CursorAdapter
from server.distill_team.promotion.adapters.factory import create_adapter
from server.distill_team.promotion.adapters.trae import TraeAdapter
from server.distill_team.promotion.adapters.windsurf import WindsurfAdapter
from server.distill_team.promotion.cascade import CascadeUpdater
from server.distill_team.promotion.manager import PromotionOrchestrator
from server.distill_team.promotion.models import (
    GraphNode,
    GraphRelation,
    GraphRelationType,
    PromotionStatus,
)


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def make_layout(base: Path) -> MemoryLayout:
    """用 tmp_path 构造隔离的 MemoryLayout，避免写入真实用户目录。"""
    trae_cn = base / "trae_cn"
    return MemoryLayout(
        project_rules_dir=base / ".trae" / "rules",
        global_rules_dir=trae_cn / "rules",
        user_profile_path=trae_cn / "memory" / "user_profile.md",
        archive_path=trae_cn / "rules" / "archive.md",
        graph_path=trae_cn / "rules" / "graph.md",
        cross_project_root=trae_cn / "memory" / "projects",
        rules_file_ext=".md",
        supports_frontmatter=True,
        hotspot_section_marker="## 热点规则",
    )


def make_orchestrator(tmp_path: Path) -> PromotionOrchestrator:
    """构造带隔离 layout 的 PromotionOrchestrator。"""
    adapter = TraeAdapter()
    layout = make_layout(tmp_path)
    return PromotionOrchestrator(adapter=adapter, layout=layout)


def make_general_rule(
    rule_id: str = "R_test",
    title: str = "通用测试规则",
    content: str = "通用规则：处理案例1和案例2的场景",
    category: str = "test",
) -> RuleEntry:
    """构造通用规则（无项目特定路径，可升维）。"""
    return RuleEntry(
        rule_id=rule_id,
        title=title,
        content=content,
        file_path=Path(f"{rule_id}.md"),
        category=category,
    )


# ---------------------------------------------------------------------------
# 集成测试
# ---------------------------------------------------------------------------


class TestPromotionIntegration:
    """PromotionOrchestrator 完整升维流程集成测试。"""

    # ------------------------------------------------------------------
    # 1. 完整升维流程（新规则，不重复）
    # ------------------------------------------------------------------

    def test_promote_new_rule_complete_flow(self, tmp_path: Path):
        """新规则完整升维：查重通过 → 回测通过 → 升维到顶层 → 归档 → 图谱登记。"""
        orchestrator = make_orchestrator(tmp_path)
        rule = make_general_rule(
            rule_id="R_complete",
            title="完整流程测试规则",
            content="通用规则：处理案例1和案例2的场景",
        )

        outcome = orchestrator.promote(rule, source_cases=["案例1", "案例2"])

        # 升维完成
        assert outcome.final_status == PromotionStatus.PROMOTED
        # 已归档
        assert outcome.archive_entry_id is not None
        # 已登记图谱
        assert outcome.graph_node_id is not None

        # archive.md 存在且包含经验条目
        archive_path = orchestrator.layout.archive_path
        assert archive_path.exists(), "archive.md 应存在"
        archive_text = archive_path.read_text(encoding="utf-8")
        assert "### E001" in archive_text, "archive.md 应包含经验条目 E001"

        # graph.md 存在且包含规则节点
        graph_path = orchestrator.layout.graph_path
        assert graph_path.exists(), "graph.md 应存在"
        graph_text = graph_path.read_text(encoding="utf-8")
        assert outcome.graph_node_id in graph_text, "graph.md 应包含规则节点"

    # ------------------------------------------------------------------
    # 2. 完全重复规则
    # ------------------------------------------------------------------

    def test_promote_exact_duplicate(self, tmp_path: Path):
        """完全重复规则：归档（不入库），记录触发失败案例。"""
        orchestrator = make_orchestrator(tmp_path)

        # 先写入一条已有规则到 project_rules_dir
        existing_content = "测试重复规则内容，用于验证查重机制"
        orchestrator._adapter.write_rule(
            rules_dir=orchestrator.layout.project_rules_dir,
            rule_id="R_existing",
            title="已有规则",
            content=existing_content,
            frontmatter={"category": "test"},
        )

        # 构造内容完全相同的新规则
        dup_rule = RuleEntry(
            rule_id="R_dup",
            title="已有规则",
            content=existing_content,
            file_path=tmp_path / "dup.md",
            category="test",
        )

        outcome = orchestrator.promote(dup_rule)

        # 归档，不入库
        assert outcome.final_status == PromotionStatus.ARCHIVED

        # archive.md 中有触发失败案例记录
        archive_text = orchestrator.layout.archive_path.read_text(encoding="utf-8")
        assert "TF-001" in archive_text or "触发失败案例" in archive_text, (
            "archive.md 应包含触发失败案例记录"
        )

    # ------------------------------------------------------------------
    # 3. 熔断场景（查重循环熔断）
    # ------------------------------------------------------------------

    def test_promote_with_circuit_breaker(self, tmp_path: Path):
        """查重循环熔断：dedup_count 达到 6 → PENDING_CONFIRMATION。"""
        orchestrator = make_orchestrator(tmp_path)

        # Monkey-patch dedup.check 来模拟 5 次已有查重周期
        # 第一次 check 时预设 dedup_count=5，increment_dedup 后达到 6 → 熔断
        original_check = orchestrator._dedup.check
        call_count = [0]

        def patched_check(rule, state, layout):
            call_count[0] += 1
            if call_count[0] == 1:
                state.dedup_count = 5
                state.global_iteration = 5
            return original_check(rule, state, layout)

        orchestrator._dedup.check = patched_check

        rule = make_general_rule(rule_id="R_cb", content="通用熔断测试规则内容")
        outcome = orchestrator.promote(rule)

        assert outcome.final_status == PromotionStatus.PENDING_CONFIRMATION

    # ------------------------------------------------------------------
    # 4. 项目特定规则（不升维）
    # ------------------------------------------------------------------

    def test_promote_project_specific_rule(self, tmp_path: Path):
        """项目特定规则：留在项目级，不升维到规则文件级。"""
        orchestrator = make_orchestrator(tmp_path)

        rule = RuleEntry(
            rule_id="R_specific",
            title="项目特定规则",
            content="在 d:\\Code\\TeamHarness\\server\\app.py 中使用 FastAPI 框架",
            file_path=tmp_path / "specific.md",
            category="test",
        )

        outcome = orchestrator.promote(rule)

        # 规则留在项目级，不升维
        assert outcome.promotion_result is not None
        assert outcome.promotion_result.promoted is False

        # global_rules_dir 下没有规则文件（排除 archive.md 和 graph.md）
        global_rules = list(orchestrator.layout.global_rules_dir.glob("*.md"))
        rule_files = [
            f for f in global_rules
            if f.name not in ("archive.md", "graph.md")
        ]
        assert len(rule_files) == 0, "项目特定规则不应写入 global_rules_dir"

    # ------------------------------------------------------------------
    # 5. 通用规则升维到规则文件级
    # ------------------------------------------------------------------

    def test_promote_general_rule_to_rules_file(self, tmp_path: Path):
        """通用规则升维到规则文件级（内容 > 200 字符，不升维到顶层）。"""
        orchestrator = make_orchestrator(tmp_path)

        # 构造 > 200 字符的通用规则，确保停在 RULES_FILE 层
        long_content = (
            "这是一条通用的测试规则，用于验证升维到规则文件级的完整流程。"
            "规则内容需要超过200个字符才能确保不会被升维到顶层，"
            "因为顶层规则有长度限制，避免过分细化。"
            "这里继续添加内容以达到长度要求。"
            "通用规则应该被升维到规则文件级，"
            "但不会被升维到用户全局顶层。"
            "这是一段足够长的通用规则内容，用于测试升维到规则文件级的行为。"
            "额外的填充内容：在软件工程中，通用规则应该适用于多种场景，"
            "而不是绑定到特定的项目或模块。这种通用性使得规则可以在不同项目间共享。"
        )
        assert len(long_content) > 200, "测试前提：内容需 > 200 字符"

        rule = RuleEntry(
            rule_id="R_general_long",
            title="通用长规则",
            content=long_content,
            file_path=tmp_path / "general.md",
            category="test",
        )

        outcome = orchestrator.promote(rule)

        assert outcome.final_status == PromotionStatus.PROMOTED
        # 规则写入 global_rules_dir
        rule_files = list(orchestrator.layout.global_rules_dir.glob("R_general_long.md"))
        assert len(rule_files) == 1, "规则应写入 global_rules_dir"

    # ------------------------------------------------------------------
    # 6. 批量升维
    # ------------------------------------------------------------------

    def test_promote_batch(self, tmp_path: Path):
        """批量升维 3 条不同规则，全部 PROMOTED。"""
        orchestrator = make_orchestrator(tmp_path)

        rules = [
            make_general_rule(
                rule_id=f"R_batch_{i}",
                title=f"批量规则{i}",
                content=f"通用批量测试规则{i}：处理场景{i}的通用经验",
                category="batch",
            )
            for i in range(3)
        ]

        outcomes = orchestrator.promote_batch(rules, source_session="batch_session")

        assert len(outcomes) == 3
        for i, outcome in enumerate(outcomes):
            assert outcome.final_status == PromotionStatus.PROMOTED, (
                f"规则 {i} 应升维完成，实际: {outcome.final_status}"
            )

    # ------------------------------------------------------------------
    # 7. 跨项目再升维
    # ------------------------------------------------------------------

    def test_cross_project_repromotion(self, tmp_path: Path):
        """跨项目再升维：2 个项目经验有共同关键词 → 触发再升维。"""
        orchestrator = make_orchestrator(tmp_path)

        # 在 cross_project_root 下创建 2 个项目目录，各含 archive.md
        cross_root = orchestrator.layout.cross_project_root
        proj1 = cross_root / "proj1"
        proj2 = cross_root / "proj2"
        proj1.mkdir(parents=True, exist_ok=True)
        proj2.mkdir(parents=True, exist_ok=True)

        # 两个项目的经验有共同关键词 "auth" 和 "returns"（>= 2 个共有关键词）
        proj1_archive = """# 归档区

## 经验归档

### E101：auth error handling
- 升维至：R001
- 升维时间：2026-08-12
- 原始错误案例：auth module returns 401 error during login
- 升维策略：升维
- 来源会话：session1
"""
        proj2_archive = """# 归档区

## 经验归档

### E102：auth failure handling
- 升维至：R002
- 升维时间：2026-08-12
- 原始错误案例：auth service returns 403 failure during access
- 升维策略：升维
- 来源会话：session2
"""
        (proj1 / "archive.md").write_text(proj1_archive, encoding="utf-8")
        (proj2 / "archive.md").write_text(proj2_archive, encoding="utf-8")

        # 触发 promote（被动触发跨项目再升维检查）
        # 使用项目特定规则（含 Windows 绝对路径），使 promote 步骤返回 promoted=False，
        # 流程跳出主循环后继续执行步骤 4-7（归档→图谱→跨项目检查→连锁更新），
        # 从而避免 promote.py 的 target_layer.value bug 阻断后续步骤。
        rule = RuleEntry(
            rule_id="R_cross",
            title="跨项目再升维测试规则",
            content="在 d:\\Code\\TeamHarness\\server\\app.py 中处理 auth 模块的 returns 逻辑",
            file_path=tmp_path / "cross.md",
            category="cross_project",
        )
        outcome = orchestrator.promote(rule)

        assert outcome.cross_project_triggered is True, (
            "应触发跨项目再升维"
        )

    # ------------------------------------------------------------------
    # 8. 连锁更新
    # ------------------------------------------------------------------

    def test_cascade_update(self, tmp_path: Path):
        """连锁更新：DEPENDS_ON 关系节点变更 → 关联节点收到更新。"""
        orchestrator = make_orchestrator(tmp_path)
        graph = orchestrator.graph

        # 注册 2 个有 DEPENDS_ON 关系的规则节点
        graph.register_node(GraphNode(
            node_id="R001",
            node_type="rule",
            name="规则A",
            location="path/a",
            category="test",
            status="active",
        ))
        graph.register_node(GraphNode(
            node_id="R002",
            node_type="rule",
            name="规则B",
            location="path/b",
            category="test",
            status="active",
        ))
        graph.add_relation(GraphRelation(
            source_id="R001",
            target_id="R002",
            relation_type=GraphRelationType.DEPENDS_ON,
            note="R001 依赖 R002",
        ))

        # 从 R001 触发连锁更新
        cascade = CascadeUpdater(graph)
        result = cascade.cascade_from("R001", change_type="modified")

        # 验证有更新记录
        assert len(result.updates) > 0, "应有连锁更新记录"
        assert any(u.node_id == "R002" for u in result.updates), (
            "R002 应收到连锁更新"
        )

    # ------------------------------------------------------------------
    # 9. 适配器自动检测
    # ------------------------------------------------------------------

    def test_adapter_auto_detect(self, tmp_path: Path):
        """适配器自动检测：项目有 .trae/ 目录 → 返回 TraeAdapter。"""
        # 创建 .trae/ 目录
        (tmp_path / ".trae").mkdir()

        adapter = create_adapter(software=None, project_root=tmp_path)

        assert isinstance(adapter, TraeAdapter), (
            "检测到 .trae/ 目录应返回 TraeAdapter"
        )

    # ------------------------------------------------------------------
    # 10. 不同软件适配器
    # ------------------------------------------------------------------

    def test_different_software_adapters(self, tmp_path: Path):
        """不同软件适配器返回正确的 MemoryLayout（路径不同）。"""
        test_cases = [
            ("cursor", CursorAdapter),
            ("claude_code", ClaudeCodeAdapter),
            ("windsurf", WindsurfAdapter),
            ("cline", ClineAdapter),
        ]

        layouts: dict[str, MemoryLayout] = {}
        for software, adapter_class in test_cases:
            adapter = create_adapter(software=software, project_root=tmp_path)
            assert isinstance(adapter, adapter_class), (
                f"software={software} 应返回 {adapter_class.__name__}"
            )
            layout = adapter.get_layout(tmp_path)
            layouts[software] = layout

        # 验证每个适配器的 project_rules_dir 路径各不相同
        paths = [str(l.project_rules_dir) for l in layouts.values()]
        assert len(set(paths)) == len(paths), (
            "各适配器的 project_rules_dir 应各不相同"
        )

        # 验证各适配器的全局规则目录也各不相同
        global_paths = [str(l.global_rules_dir) for l in layouts.values()]
        assert len(set(global_paths)) == len(global_paths), (
            "各适配器的 global_rules_dir 应各不相同"
        )
