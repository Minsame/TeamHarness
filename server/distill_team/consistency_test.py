"""模型一致性测试集（SubTask 8.10）。

20 条标准资产簇，覆盖：
- 不同 category（rule/memory/skill/tool/prompt）
- 不同规模（2-10 资产/簇）
- 不同质量（高/中/低 SNR）
- 跨成员 / 单成员 / is_convention
- 反例场景（应 SKIP）

用途：LLM 模型升级 / Prompt 模板变更时，用同一测试集回归，
保证输出决策（PROMOTE/SKIP）一致性。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class TestClusterFixture:
    """测试簇 fixture。"""

    fixture_id: str
    description: str
    expected_decision: str  # PROMOTE / SKIP
    expected_skip_reason: str = ""
    # 簇内资产（id, owner, module_path, category, content, tags）
    assets: list[dict[str, Any]] = field(default_factory=list)
    is_convention: bool = False
    expected_confidence: str = "medium"  # high / medium / low
    expected_cold_start: bool = False


def _make_asset(
    aid: str,
    owner: str,
    module: str,
    category: str,
    content: str,
    tags: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "id": aid,
        "owner": owner,
        "module_path": module,
        "category": category,
        "content": content,
        "tags": tags or [],
    }


# ---------------------------------------------------------------------------
# 20 条标准资产簇
# ---------------------------------------------------------------------------

_FIXTURES: list[TestClusterFixture] = [
    # 1-4: 高质量跨成员规则（应 PROMOTE）
    TestClusterFixture(
        fixture_id="TC-001",
        description="3 成员重复 PR Review 检查清单，高 SNR",
        expected_decision="PROMOTE",
        expected_confidence="high",
        assets=[
            _make_asset("a1", "alice", "modules/backend", "rule-backend",
                        "# PR Review\n应当检查测试覆盖\n禁止硬编码密钥", ["pr-review"]),
            _make_asset("a2", "bob", "modules/frontend", "rule-frontend",
                        "# PR Review\n应当检查测试覆盖\n禁止硬编码密钥", ["pr-review"]),
            _make_asset("a3", "carol", "modules/api", "rule-api",
                        "# PR Review\n应当检查测试覆盖\n禁止硬编码密钥", ["pr-review"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-002",
        description="4 成员重复 commit message 规范",
        expected_decision="PROMOTE",
        expected_confidence="high",
        assets=[
            _make_asset("a4", "alice", "modules/backend", "rule-git",
                        "# Commit 规范\nsubject 不超 50 字\n禁止 'update'", ["git"]),
            _make_asset("a5", "bob", "modules/frontend", "rule-git",
                        "# Commit 规范\nsubject 不超 50 字\n禁止 'update'", ["git"]),
            _make_asset("a6", "carol", "modules/api", "rule-git",
                        "# Commit 规范\nsubject 不超 50 字\n禁止 'update'", ["git"]),
            _make_asset("a7", "dave", "modules/infra", "rule-git",
                        "# Commit 规范\nsubject 不超 50 字\n禁止 'update'", ["git"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-003",
        description="3 成员重复调试经验模板",
        expected_decision="PROMOTE",
        expected_confidence="high",
        assets=[
            _make_asset("a8", "alice", "modules/backend", "memory-debug",
                        "# 调试模板\n## 现象\n## 根因\n## 修复\n## 经验", ["debug"]),
            _make_asset("a9", "bob", "modules/frontend", "memory-debug",
                        "# 调试模板\n## 现象\n## 根因\n## 修复\n## 经验", ["debug"]),
            _make_asset("a10", "carol", "modules/api", "memory-debug",
                        "# 调试模板\n## 现象\n## 根因\n## 修复\n## 经验", ["debug"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-004",
        description="3 成员重复 API 设计规范",
        expected_decision="PROMOTE",
        expected_confidence="high",
        assets=[
            _make_asset("a11", "alice", "modules/api", "rule-api",
                        "# API 设计\n应当 RESTful\n禁止动词在 URL", ["api"]),
            _make_asset("a12", "bob", "modules/api", "rule-api",
                        "# API 设计\n应当 RESTful\n禁止动词在 URL", ["api"]),
            _make_asset("a13", "carol", "modules/api", "rule-api",
                        "# API 设计\n应当 RESTful\n禁止动词在 URL", ["api"]),
        ],
    ),
    # 5-8: 中等质量（应 PROMOTE medium）
    TestClusterFixture(
        fixture_id="TC-005",
        description="2 成员重复，但 SNR 中等",
        expected_decision="PROMOTE",
        expected_confidence="medium",
        assets=[
            _make_asset("a14", "alice", "modules/backend", "rule-backend",
                        "# Lint\n应当使用 black\n---\nid: a14\n", ["lint"]),
            _make_asset("a15", "bob", "modules/backend", "rule-backend",
                        "# Lint\n应当使用 black\n---\nid: a15\n", ["lint"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-006",
        description="2 成员重复，跨模块（泛化性信号）",
        expected_decision="PROMOTE",
        expected_confidence="medium",
        assets=[
            _make_asset("a16", "alice", "modules/backend", "rule-test",
                        "# 测试\n应当覆盖边界值", ["test"]),
            _make_asset("a17", "bob", "modules/frontend", "rule-test",
                        "# 测试\n应当覆盖边界值", ["test"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-007",
        description="3 成员重复，但内容简短",
        expected_decision="PROMOTE",
        expected_confidence="medium",
        assets=[
            _make_asset("a18", "alice", "modules/backend", "rule-deploy",
                        "# 部署\n必须先跑测试", ["deploy"]),
            _make_asset("a19", "bob", "modules/backend", "rule-deploy",
                        "# 部署\n必须先跑测试", ["deploy"]),
            _make_asset("a20", "carol", "modules/backend", "rule-deploy",
                        "# 部署\n必须先跑测试", ["deploy"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-008",
        description="2 成员重复，含可操作性关键词",
        expected_decision="PROMOTE",
        expected_confidence="medium",
        assets=[
            _make_asset("a21", "alice", "modules/backend", "skill-debug",
                        "# 调试步骤\n1. 复现\n2. 定位\n3. 修复", ["debug"]),
            _make_asset("a22", "bob", "modules/api", "skill-debug",
                        "# 调试步骤\n1. 复现\n2. 定位\n3. 修复", ["debug"]),
        ],
    ),
    # 9-12: 低质量 / 应 SKIP
    TestClusterFixture(
        fixture_id="TC-009",
        description="单成员资产，不满足跨成员门禁",
        expected_decision="SKIP",
        expected_skip_reason="low_source_diversity",
        assets=[
            _make_asset("a23", "alice", "modules/backend", "rule-backend",
                        "# 单一规则\n仅 alice 写的", ["lint"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-010",
        description="2 成员但内容互相矛盾（反例检验应失败）",
        expected_decision="SKIP",
        expected_skip_reason="counter_example_failed",
        assets=[
            _make_asset("a24", "alice", "modules/backend", "rule-backend",
                        "# 缩进\n应当用 tab", ["indent"]),
            _make_asset("a25", "bob", "modules/backend", "rule-backend",
                        "# 缩进\n应当用 space", ["indent"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-011",
        description="低 SNR，全是 frontmatter 无实质内容",
        expected_decision="SKIP",
        expected_skip_reason="low_snr",
        assets=[
            _make_asset("a26", "alice", "modules/backend", "rule-backend",
                        "---\nid: a26\ntype: rule\n---\n", ["empty"]),
            _make_asset("a27", "bob", "modules/backend", "rule-backend",
                        "---\nid: a27\ntype: rule\n---\n", ["empty"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-012",
        description="过拟合到单一模块（仅 modules/legacy）",
        expected_decision="SKIP",
        expected_skip_reason="overfit_to_module",
        assets=[
            _make_asset("a28", "alice", "modules/legacy/coa", "rule-legacy",
                        "# Legacy COA\n仅适用于 COA 模块的特定命名", ["legacy"]),
            _make_asset("a29", "bob", "modules/legacy/coa", "rule-legacy",
                        "# Legacy COA\n仅适用于 COA 模块的特定命名", ["legacy"]),
        ],
    ),
    # 13-16: is_convention 旁路
    TestClusterFixture(
        fixture_id="TC-013",
        description="is_convention=true 单成员，应 PROMOTE",
        expected_decision="PROMOTE",
        is_convention=True,
        expected_confidence="medium",
        assets=[
            _make_asset("a30", "alice", "modules/backend", "rule-convention",
                        "# 团队约定\n变量命名用 snake_case", ["convention"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-014",
        description="is_convention=true 3 成员同 category",
        expected_decision="PROMOTE",
        is_convention=True,
        expected_confidence="high",
        assets=[
            _make_asset("a31", "alice", "modules/backend", "rule-convention",
                        "# 团队约定\n禁止 magic number", ["convention"]),
            _make_asset("a32", "bob", "modules/frontend", "rule-convention",
                        "# 团队约定\n禁止 magic number", ["convention"]),
            _make_asset("a33", "carol", "modules/api", "rule-convention",
                        "# 团队约定\n禁止 magic number", ["convention"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-015",
        description="is_convention=true 但反例检验失败（矛盾约定）",
        expected_decision="SKIP",
        is_convention=True,
        expected_skip_reason="counter_example_failed",
        assets=[
            _make_asset("a34", "alice", "modules/backend", "rule-convention",
                        "# 约定\n必须用 tab 缩进", ["convention"]),
            _make_asset("a35", "bob", "modules/frontend", "rule-convention",
                        "# 约定\n必须用 space 缩进", ["convention"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-016",
        description="is_convention=true 单成员低质量",
        expected_decision="SKIP",
        is_convention=True,
        expected_skip_reason="low_snr",
        assets=[
            _make_asset("a36", "alice", "modules/backend", "rule-convention",
                        "---\nid: a36\n---\n", ["empty"]),
        ],
    ),
    # 17-20: 冷启动期场景
    TestClusterFixture(
        fixture_id="TC-017",
        description="冷启动期 2 成员，应 PROMOTE 但 confidence=low + cold_start=true",
        expected_decision="PROMOTE",
        expected_confidence="low",
        expected_cold_start=True,
        assets=[
            _make_asset("a37", "alice", "modules/backend", "rule-backend",
                        "# 冷启动规则\n应当检查日志", ["cold-start"]),
            _make_asset("a38", "bob", "modules/api", "rule-backend",
                        "# 冷启动规则\n应当检查日志", ["cold-start"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-018",
        description="冷启动期 2 成员低质量，应 SKIP",
        expected_decision="SKIP",
        expected_skip_reason="low_snr",
        expected_cold_start=True,
        expected_confidence="low",
        assets=[
            _make_asset("a39", "alice", "modules/backend", "rule-backend",
                        "---\nid: a39\n---\n", ["empty"]),
            _make_asset("a40", "bob", "modules/api", "rule-backend",
                        "---\nid: a40\n---\n", ["empty"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-019",
        description="冷启动期 is_convention 单成员",
        expected_decision="PROMOTE",
        is_convention=True,
        expected_confidence="low",
        expected_cold_start=True,
        assets=[
            _make_asset("a41", "alice", "modules/backend", "rule-convention",
                        "# 冷启动约定\n必须用 git commit -m", ["convention"]),
        ],
    ),
    TestClusterFixture(
        fixture_id="TC-020",
        description="冷启动期 2 成员反例检验失败",
        expected_decision="SKIP",
        expected_skip_reason="counter_example_failed",
        expected_cold_start=True,
        expected_confidence="low",
        assets=[
            _make_asset("a42", "alice", "modules/backend", "rule-convention",
                        "# 约定\n用 tab", ["convention"]),
            _make_asset("a43", "bob", "modules/frontend", "rule-convention",
                        "# 约定\n用 space", ["convention"]),
        ],
    ),
]


class ConsistencyTestSet:
    """模型一致性测试集（20 条标准资产簇）。

    用法：
        ts = ConsistencyTestSet()
        for fixture in ts.list_fixtures():
            # 跑 LLM 提炼 → 比对 expected_decision
            ...
    """

    def list_fixtures(self) -> list[TestClusterFixture]:
        """返回全部 20 条 fixture。"""
        return list(_FIXTURES)

    def get_fixture(self, fixture_id: str) -> TestClusterFixture | None:
        """按 fixture_id 查 fixture。"""
        for f in _FIXTURES:
            if f.fixture_id == fixture_id:
                return f
        return None

    def count(self) -> int:
        """fixture 总数。"""
        return len(_FIXTURES)

    def by_decision(self, decision: str) -> list[TestClusterFixture]:
        """按预期决策过滤。"""
        return [f for f in _FIXTURES if f.expected_decision == decision]

    def by_cold_start(self) -> list[TestClusterFixture]:
        """返回冷启动场景 fixture。"""
        return [f for f in _FIXTURES if f.expected_cold_start]


__all__ = ["ConsistencyTestSet", "TestClusterFixture"]
