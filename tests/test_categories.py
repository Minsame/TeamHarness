"""categories.yaml 受控词汇表 + PR 校验测试。

对应 SubTask 1.6：
- 两级 `<type>-<module>` 命名规范校验
- type 须为 rule/memory/skill/tool/prompt 之一
- categories.yaml 列表式 / 映射式两种格式解析
- PR 校验：未登记 / 命名不合法 / module 未在 INDEX.md 登记 → 违规
"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.infra_git.categories import (
    CATEGORIES_DIR,
    CATEGORIES_FILENAME,
    CATEGORY_PATTERN,
    VALID_TYPES,
    CategoriesRegistry,
    CategoryEntry,
    CategoryViolation,
    add_category,
    check_module_registered,
    load_categories,
    parse_categories_yaml,
    serialize_categories_yaml,
    validate_category_format,
    validate_pr_categories,
)
from server.infra_git.index_manager import IndexDoc
from server.common.models import INDEXLevel


# ---------------------------------------------------------------------------
# 命名规范
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cat,expected",
    [
        ("rule-backend", True),
        ("memory-api-conventions", True),
        ("skill-db", True),
        ("tool-lint-runner", True),
        ("prompt-review-template", True),
        ("", False),
        ("rule", False),  # 缺少 -<module>
        ("Rule-backend", False),  # 大写不合法
        ("unknown-backend", False),  # type 非法
        ("rule-", False),  # 缺 module
        ("rule-Backend", False),  # module 大写不合法
    ],
)
def test_validate_category_format(cat, expected):
    assert validate_category_format(cat) is expected


def test_valid_types_aligns_with_asset_type():
    assert VALID_TYPES == frozenset(
        {"rule", "memory", "skill", "tool", "prompt"}
    )


def test_category_pattern_matches_module_with_dashes():
    m = CATEGORY_PATTERN.match("memory-api-conventions")
    assert m is not None
    assert m.group("type") == "memory"
    assert m.group("module") == "api-conventions"


# ---------------------------------------------------------------------------
# 解析与序列化
# ---------------------------------------------------------------------------


def test_parse_categories_list_format():
    yaml_text = """
categories:
  - name: rule-backend
    description: 后端规则
    modules: [backend]
  - name: skill-db
    description: DB 技能
"""
    reg = parse_categories_yaml(yaml_text)
    assert "rule-backend" in reg.categories
    assert reg.categories["rule-backend"].description == "后端规则"
    assert reg.categories["rule-backend"].modules == ["backend"]
    assert "skill-db" in reg.categories
    assert reg.is_registered("rule-backend")
    assert not reg.is_registered("rule-frontend")


def test_parse_categories_map_format():
    yaml_text = """
categories:
  rule-backend:
    description: 后端规则
    modules: [backend]
  skill-db: DB 技能
"""
    reg = parse_categories_yaml(yaml_text)
    assert reg.categories["rule-backend"].modules == ["backend"]
    assert reg.categories["skill-db"].description == "DB 技能"


def test_parse_categories_empty():
    reg = parse_categories_yaml("")
    assert reg.categories == {}


def test_parse_categories_invalid_returns_empty():
    reg = parse_categories_yaml("just a string")
    assert reg.categories == {}


def test_serialize_round_trip():
    reg = CategoriesRegistry()
    add_category(reg, "rule-backend", "后端规则", ["backend"])
    add_category(reg, "skill-db", "DB 技能")
    text = serialize_categories_yaml(reg)
    reg2 = parse_categories_yaml(text)
    assert "rule-backend" in reg2.categories
    assert reg2.categories["skill-db"].description == "DB 技能"


def test_add_category_invalid_raises():
    reg = CategoriesRegistry()
    with pytest.raises(ValueError):
        add_category(reg, "invalid-format")


def test_load_categories_from_file(tmp_path: Path):
    repo = tmp_path / "repo"
    (repo / CATEGORIES_DIR).mkdir(parents=True)
    (repo / CATEGORIES_DIR / CATEGORIES_FILENAME).write_text(
        "categories:\n  - name: rule-backend\n    description: x\n",
        encoding="utf-8",
    )
    reg = load_categories(repo)
    assert "rule-backend" in reg.categories
    assert reg.source_path is not None


def test_load_categories_missing_file_returns_empty(tmp_path: Path):
    reg = load_categories(tmp_path / "nope")
    assert reg.categories == {}


# ---------------------------------------------------------------------------
# check_module_registered
# ---------------------------------------------------------------------------


def test_check_module_registered_match():
    docs = [
        IndexDoc(level=INDEXLevel.PROJECT, module="teamharness-shared"),
        IndexDoc(level=INDEXLevel.MODULE, module="backend"),
    ]
    assert check_module_registered("rule-backend", docs) is True


def test_check_module_registered_no_match():
    docs = [IndexDoc(level=INDEXLevel.PROJECT, module="teamharness-shared")]
    assert check_module_registered("rule-ghost", docs) is False


def test_check_module_registered_invalid_category():
    docs = [IndexDoc(level=INDEXLevel.PROJECT, module="x")]
    assert check_module_registered("invalid", docs) is False


def test_check_module_registered_by_path_segment(tmp_path: Path):
    """module 名出现在 source_path 路径段中也算登记。"""
    doc = IndexDoc(level=INDEXLevel.MODULE, module="different")
    doc.source_path = tmp_path / "modules" / "api-conventions" / "INDEX.md"
    assert check_module_registered("memory-api-conventions", [doc]) is True


# ---------------------------------------------------------------------------
# validate_pr_categories
# ---------------------------------------------------------------------------


def test_validate_pr_categories_all_pass():
    reg = CategoriesRegistry()
    add_category(reg, "rule-backend", "x", ["backend"])
    docs = [IndexDoc(level=INDEXLevel.MODULE, module="backend")]
    violations = validate_pr_categories(
        [("rules/x.md", "rule-backend")], reg, docs
    )
    assert violations == []


def test_validate_pr_categories_unregistered():
    reg = CategoriesRegistry()
    docs = [IndexDoc(level=INDEXLevel.MODULE, module="backend")]
    violations = validate_pr_categories(
        [("rules/x.md", "rule-frontend")], reg, docs
    )
    assert len(violations) == 1
    assert "未在 categories.yaml 登记" in violations[0].reason


def test_validate_pr_categories_invalid_format():
    reg = CategoriesRegistry()
    docs: list[IndexDoc] = []
    violations = validate_pr_categories([("rules/x.md", "invalid")], reg, docs)
    assert len(violations) == 1
    assert "命名规范" in violations[0].reason


def test_validate_pr_categories_module_not_in_index():
    reg = CategoriesRegistry()
    add_category(reg, "rule-ghost", "x", ["ghost"])
    docs = [IndexDoc(level=INDEXLevel.MODULE, module="backend")]  # 没有 ghost
    violations = validate_pr_categories(
        [("rules/x.md", "rule-ghost")], reg, docs
    )
    assert len(violations) == 1
    assert "未在任一 INDEX.md 登记" in violations[0].reason


def test_validate_pr_categories_multiple_violations_continue():
    """多条资产出现违规时，全部报告而非短路。"""
    reg = CategoriesRegistry()
    add_category(reg, "rule-backend", "x", ["backend"])
    docs = [IndexDoc(level=INDEXLevel.MODULE, module="backend")]
    violations = validate_pr_categories(
        [
            ("a.md", "invalid"),
            ("b.md", "rule-ghost"),  # 命名合法但未登记
            ("c.md", "rule-backend"),  # 合法
            ("d.md", "rule-frontend"),  # 未登记
        ],
        reg,
        docs,
    )
    assert len(violations) == 3  # 3 条违规
    paths = [v.asset_path for v in violations]
    assert "a.md" in paths
    assert "b.md" in paths
    assert "d.md" in paths
