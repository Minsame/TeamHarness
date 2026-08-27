"""四类资产子 Prompt 模板测试（SubTask 7.6）。"""

from __future__ import annotations

import pytest

from server.distill_personal.prompts import (
    ASSET_PROMPT_TEMPLATES,
    MEMORY_SYSTEM_PROMPT,
    RULE_SYSTEM_PROMPT,
    SKILL_SCHEMA,
    SKILL_SYSTEM_PROMPT,
    TOOL_SCHEMA,
    TOOL_SYSTEM_PROMPT,
    AssetPromptTemplate,
    get_prompt_template,
    render_system_prompt,
)


# ---------------------------------------------------------------------------
# 模板注册表
# ---------------------------------------------------------------------------


def test_registry_has_four_asset_types() -> None:
    """ASSET_PROMPT_TEMPLATES 应包含 rule/memory/skill/tool 四类。"""
    assert set(ASSET_PROMPT_TEMPLATES.keys()) == {"rule", "memory", "skill", "tool"}


def test_each_template_has_system_prompt_and_schema() -> None:
    """每类模板必须有 system_prompt 与 schema。"""
    for asset_type, template in ASSET_PROMPT_TEMPLATES.items():
        assert isinstance(template, AssetPromptTemplate)
        assert template.asset_type == asset_type
        assert isinstance(template.system_prompt, str)
        assert len(template.system_prompt) > 0
        assert isinstance(template.schema, dict)
        assert template.schema["type"] == "object"


def test_get_prompt_template_returns_correct_type() -> None:
    """get_prompt_template 按类型返回对应模板。"""
    assert get_prompt_template("rule").asset_type == "rule"
    assert get_prompt_template("memory").asset_type == "memory"
    assert get_prompt_template("skill").asset_type == "skill"
    assert get_prompt_template("tool").asset_type == "tool"


def test_get_prompt_template_case_insensitive() -> None:
    """asset_type 大小写不敏感。"""
    assert get_prompt_template("RULE").asset_type == "rule"
    assert get_prompt_template("Skill").asset_type == "skill"


def test_get_prompt_template_unknown_raises() -> None:
    """未知类型应抛 ValueError。"""
    with pytest.raises(ValueError, match="未知资产类型"):
        get_prompt_template("unknown")


def test_rule_memory_share_base_schema() -> None:
    """rule/memory 共用 _BASE_ASSET_SCHEMA（无 steps / invocation）。"""
    rule_schema = ASSET_PROMPT_TEMPLATES["rule"].schema
    memory_schema = ASSET_PROMPT_TEMPLATES["memory"].schema
    asset_props = rule_schema["properties"]["asset"]["properties"]
    assert "steps" not in asset_props
    assert "invocation" not in asset_props
    assert set(asset_props.keys()) == {"title", "content", "tags", "rationale"}
    # memory schema 与 rule 一致
    assert set(memory_schema["properties"]["asset"]["properties"].keys()) == {
        "title",
        "content",
        "tags",
        "rationale",
    }


def test_skill_schema_has_steps() -> None:
    """SKILL_SCHEMA 的 asset 应追加 steps 字段。"""
    asset_props = SKILL_SCHEMA["properties"]["asset"]["properties"]
    assert "steps" in asset_props
    assert asset_props["steps"]["type"] == "array"
    # steps 应在 required 中
    assert "steps" in SKILL_SCHEMA["properties"]["asset"]["required"]


def test_tool_schema_has_invocation() -> None:
    """TOOL_SCHEMA 的 asset 应追加 invocation 字段（含 command/args/notes）。"""
    asset_props = TOOL_SCHEMA["properties"]["asset"]["properties"]
    assert "invocation" in asset_props
    inv = asset_props["invocation"]
    assert inv["type"] == "object"
    assert set(inv["properties"].keys()) == {"command", "args", "notes"}
    assert set(inv["required"]) == {"command", "args", "notes"}
    assert "invocation" in TOOL_SCHEMA["properties"]["asset"]["required"]


def test_schema_required_fields() -> None:
    """所有 schema 顶层必含 skip / asset / confidence。"""
    for template in ASSET_PROMPT_TEMPLATES.values():
        assert set(template.schema["required"]) == {"skip", "asset", "confidence"}


# ---------------------------------------------------------------------------
# render_system_prompt
# ---------------------------------------------------------------------------


def test_render_system_prompt_replaces_placeholders() -> None:
    """render_system_prompt 应注入 __MODULE_PATH__ / __TITLE__ / __CONTENT_EXCERPT__。"""
    template = get_prompt_template("rule")
    rendered = render_system_prompt(
        template,
        content_excerpt="提交前必须跑 lint",
        module_path="modules/backend",
        title="lint 规则",
    )
    assert "modules/backend" in rendered
    assert "lint 规则" in rendered
    assert "提交前必须跑 lint" in rendered
    # 占位符应被替换
    assert "__MODULE_PATH__" not in rendered
    assert "__TITLE__" not in rendered
    assert "__CONTENT_EXCERPT__" not in rendered


def test_render_system_prompt_defaults_empty_fields() -> None:
    """空字段注入占位符（空 module_path → "(空)"，空 title → "(待提炼)"）。"""
    template = get_prompt_template("memory")
    rendered = render_system_prompt(
        template,
        content_excerpt="",
        module_path="",
        title="",
    )
    assert "(空)" in rendered
    assert "(待提炼)" in rendered


def test_render_system_prompt_truncates_long_excerpt() -> None:
    """超长 content_excerpt 应被截断到 max_excerpt_chars。"""
    template = get_prompt_template("rule")
    long_text = "x" * 5000
    rendered = render_system_prompt(
        template,
        content_excerpt=long_text,
        module_path="m",
        title="t",
        max_excerpt_chars=100,
    )
    # 截断后长度 <= 100
    assert "x" * 100 in rendered
    assert "x" * 101 not in rendered


def test_render_does_not_break_json_braces() -> None:
    """render 用 .replace()，不应破坏 system_prompt 中的 JSON 大括号。"""
    template = get_prompt_template("rule")
    rendered = render_system_prompt(
        template,
        content_excerpt="abc",
        module_path="m",
        title="t",
    )
    # system_prompt 含 JSON schema 字面量 {"skip": bool, ...}，应保留
    assert '{"skip": bool' in rendered or '"skip": bool' in rendered


# ---------------------------------------------------------------------------
# 模板内容（task-specific 文案）
# ---------------------------------------------------------------------------


def test_rule_prompt_mentions_constraints() -> None:
    """rule 模板应提及"约束/规范"。"""
    assert "约束" in RULE_SYSTEM_PROMPT or "规范" in RULE_SYSTEM_PROMPT


def test_memory_prompt_mentions_facts() -> None:
    """memory 模板应提及"事实/决策"。"""
    assert "事实" in MEMORY_SYSTEM_PROMPT or "决策" in MEMORY_SYSTEM_PROMPT


def test_skill_prompt_mentions_steps() -> None:
    """skill 模板应提及"步骤/流程"。"""
    assert "步骤" in SKILL_SYSTEM_PROMPT or "流程" in SKILL_SYSTEM_PROMPT


def test_tool_prompt_mentions_invocation() -> None:
    """tool 模板应提及"工具/命令"。"""
    assert "工具" in TOOL_SYSTEM_PROMPT or "命令" in TOOL_SYSTEM_PROMPT


def test_all_templates_have_skip_condition() -> None:
    """每个模板都应说明 SKIP 条件（让 LLM 知道何时输出 skip=true）。"""
    for prompt in (
        RULE_SYSTEM_PROMPT,
        MEMORY_SYSTEM_PROMPT,
        SKILL_SYSTEM_PROMPT,
        TOOL_SYSTEM_PROMPT,
    ):
        assert "SKIP" in prompt or "skip" in prompt
