"""四类资产子 Prompt 模板（rule / memory / skill / tool）。

对应 SubTask 7.6 + 技术方案 3.3.2「个人提炼 Prompt 设计要点」：
- 显式区分四类资产目标，分别用不同子 prompt 提炼
  - rule："用户反复强调的约束/规范是什么？"
  - memory："用户提及的项目事实/决策是什么？"
  - skill："用户展示了什么可复用的操作流程？"
  - tool："用户使用了什么工具/脚本，且对其效果认可？"
- 任务是"从对话中提炼经验"，不是"总结对话"——聚焦可复用知识，非流水账
- 先标注候选轮次再提炼，避免对整段对话做摘要导致信息稀释
- SKIP 机制：纯闲聊、一次性调试细节、无明确结论的讨论不产出

每个模板：
- system_prompt：注入到 LLM messages[0]
- schema：强制 JSON 输出 schema（用于 SubTask 7.10 schema 校验）
- placeholder：占位符（__TITLE__ / __CONTENT_EXCERPT__ / __MODULE_PATH__ 等）
  使用 .replace() 注入，避免 .format() 与 JSON 大括号冲突
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


# ---------------------------------------------------------------------------
# Prompt 模板数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AssetPromptTemplate:
    """单类资产提炼 Prompt 模板。"""

    asset_type: str  # rule / memory / skill / tool
    system_prompt: str
    schema: dict[str, Any]
    # user message 固定为"请返回 JSON"，可由调用方追加上下文


# ---------------------------------------------------------------------------
# 四类资产子 Prompt
# ---------------------------------------------------------------------------


# rule 提炼：用户反复强调的约束/规范
RULE_SYSTEM_PROMPT = """你是 TeamHarness 个人经验提炼助手，专注提炼"规则类"资产。

任务：从下面的对话片段中，提炼用户反复强调的编码规范、风格约束、团队约定。
不是总结对话，而是抽取"可复用的约束规则"。

判定要点：
1. 用户多次强调、纠正、要求的约束（如"提交前必须跑 lint"）
2. 客观约束，非主观偏好（"我喜欢 TypeScript" 不算规则）
3. 能表述为清晰可执行的条目（"代码必须格式化"而非"要注意格式"）
4. 可在未来 coding 中复用，非一次性任务上下文

SKIP 条件（输出 skip=true）：
- 仅主观偏好无客观内核
- 一次性任务上下文（如"这个 bug 改成 X"）
- 模糊感悟无法落地为具体条目
- 对话片段无任何规则信号

资产信息：
- module_path: __MODULE_PATH__
- 候选标题（参考）: __TITLE__

对话片段（已标注为 rule 候选）:
__CONTENT_EXCERPT__

请返回严格 JSON，schema:
{"skip": bool, "asset": {"title": str, "content": str, "tags": [str], "rationale": str}, "confidence": float}

字段说明：
- skip：是否跳过（true 时 asset 可为空对象）
- asset.title：规则标题（简短，<= 50 字）
- asset.content：规则正文（清晰可执行，markdown 格式）
- asset.tags：标签数组（如 ["lint", "git"]）
- asset.rationale：为什么值得沉淀（引用对话中的证据）
- confidence：0.0-1.0，提炼置信度
"""


# memory 提炼：用户提及的项目事实/决策
MEMORY_SYSTEM_PROMPT = """你是 TeamHarness 个人经验提炼助手，专注提炼"记忆类"资产。

任务：从下面的对话片段中，提炼用户提及的项目事实、决策记录、踩坑笔记。
不是总结对话，而是抽取"可复用的项目知识"。

判定要点：
1. 项目事实（如"X 模块用 Y 协议"）
2. 决策记录（如"选择 A 而非 B 的原因"）
3. 踩坑笔记（如"路径含空格会导致 X 错误"）
4. 客观可验证，非主观推测

SKIP 条件：
- 一次性调试细节（如"刚才那个 typo 改了就好了"）
- 主观推测无证据
- 已是常识（如"Python 用缩进"）
- 对话片段无任何记忆信号

资产信息：
- module_path: __MODULE_PATH__
- 候选标题（参考）: __TITLE__

对话片段（已标注为 memory 候选）:
__CONTENT_EXCERPT__

请返回严格 JSON，schema:
{"skip": bool, "asset": {"title": str, "content": str, "tags": [str], "rationale": str}, "confidence": float}
"""


# skill 提炼：可复用的操作流程
SKILL_SYSTEM_PROMPT = """你是 TeamHarness 个人经验提炼助手，专注提炼"技能类"资产。

任务：从下面的对话片段中，提炼用户展示的可复用操作流程、工作方法、技能包。
不是总结对话，而是抽取"可复用的步骤化技能"。

判定要点：
1. 用户展示了明确的操作步骤（如"DB 迁移的 5 步流程"）
2. 流程可复用，非一次性脚本
3. 步骤清晰可执行，非模糊感悟
4. 包含必要的工具/命令引用

SKIP 条件：
- 一次性脚本无复用价值
- 步骤模糊无法落地
- 纯概念讨论无操作步骤
- 对话片段无任何 skill 信号

资产信息：
- module_path: __MODULE_PATH__
- 候选标题（参考）: __TITLE__

对话片段（已标注为 skill 候选）:
__CONTENT_EXCERPT__

请返回严格 JSON，schema:
{"skip": bool, "asset": {"title": str, "content": str, "tags": [str], "rationale": str, "steps": [str]}, "confidence": float}

字段说明：
- asset.steps：操作步骤数组（按顺序，每步一句话）
"""


# tool 提炼：用户使用了什么工具/脚本，且对其效果认可
TOOL_SYSTEM_PROMPT = """你是 TeamHarness 个人经验提炼助手，专注提炼"工具类"资产。

任务：从下面的对话片段中，提炼用户使用的工具、脚本、命令，且对其效果认可。
不是总结对话，而是抽取"可被 Agent 调用的工具资产"。

判定要点：
1. 用户明确使用了某工具/脚本/命令（如"用 ruff 检查"）
2. 用户对其效果认可（如"ruff 比 flake8 快"）
3. 工具可被其他 Agent 复用
4. 包含调用方式与参数说明

SKIP 条件：
- 用户仅提及但未实际使用
- 用户使用后否定（如"这个工具不好用"）
- 工具过于特定不可复用（如"某项目专用的 build 脚本"）
- 对话片段无任何 tool 信号

资产信息：
- module_path: __MODULE_PATH__
- 候选标题（参考）: __TITLE__

对话片段（已标注为 tool 候选）:
__CONTENT_EXCERPT__

请返回严格 JSON，schema:
{"skip": bool, "asset": {"title": str, "content": str, "tags": [str], "rationale": str, "invocation": {"command": str, "args": [str], "notes": str}}, "confidence": float}

字段说明：
- asset.invocation：工具调用信息
  - command：主命令（如 "ruff" / "python -m pytest"）
  - args：参数数组
  - notes：使用注意事项
"""


# ---------------------------------------------------------------------------
# 统一 schema（每类资产共用基础结构，仅 system_prompt 不同）
# ---------------------------------------------------------------------------


_BASE_ASSET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "skip": {"type": "boolean"},
        "asset": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
                "tags": {"type": "array", "items": {"type": "string"}},
                "rationale": {"type": "string"},
            },
            "required": ["title", "content", "tags", "rationale"],
        },
        "confidence": {"type": "number"},
    },
    "required": ["skip", "asset", "confidence"],
}


def _with_extra_asset_properties(extra: dict[str, Any]) -> dict[str, Any]:
    """在 _BASE_ASSET_SCHEMA 的 asset.properties 上追加额外字段。"""
    schema = json_deep_copy(_BASE_ASSET_SCHEMA)
    asset_props = schema["properties"]["asset"]["properties"]
    for k, v in extra.items():
        asset_props[k] = v
        schema["properties"]["asset"]["required"].append(k)
    return schema


def json_deep_copy(obj: Any) -> Any:
    """JSON 深拷贝（避免引用共享）。"""
    import json
    return json.loads(json.dumps(obj))


# skill schema 追加 steps
SKILL_SCHEMA = _with_extra_asset_properties(
    {"steps": {"type": "array", "items": {"type": "string"}}}
)

# tool schema 追加 invocation
TOOL_SCHEMA = _with_extra_asset_properties(
    {
        "invocation": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "args": {"type": "array", "items": {"type": "string"}},
                "notes": {"type": "string"},
            },
            "required": ["command", "args", "notes"],
        }
    }
)


# ---------------------------------------------------------------------------
# 模板注册表
# ---------------------------------------------------------------------------


ASSET_PROMPT_TEMPLATES: dict[str, AssetPromptTemplate] = {
    "rule": AssetPromptTemplate(
        asset_type="rule",
        system_prompt=RULE_SYSTEM_PROMPT,
        schema=json_deep_copy(_BASE_ASSET_SCHEMA),
    ),
    "memory": AssetPromptTemplate(
        asset_type="memory",
        system_prompt=MEMORY_SYSTEM_PROMPT,
        schema=json_deep_copy(_BASE_ASSET_SCHEMA),
    ),
    "skill": AssetPromptTemplate(
        asset_type="skill",
        system_prompt=SKILL_SYSTEM_PROMPT,
        schema=SKILL_SCHEMA,
    ),
    "tool": AssetPromptTemplate(
        asset_type="tool",
        system_prompt=TOOL_SYSTEM_PROMPT,
        schema=TOOL_SCHEMA,
    ),
}


def get_prompt_template(asset_type: str) -> AssetPromptTemplate:
    """按资产类型获取 Prompt 模板。"""
    key = asset_type.lower()
    if key not in ASSET_PROMPT_TEMPLATES:
        raise ValueError(f"未知资产类型: {asset_type}（支持: rule/memory/skill/tool）")
    return ASSET_PROMPT_TEMPLATES[key]


def render_system_prompt(
    template: AssetPromptTemplate,
    *,
    content_excerpt: str,
    module_path: str = "",
    title: str = "",
    max_excerpt_chars: int = 2000,
) -> str:
    """渲染 system_prompt，注入动态字段。

    使用 .replace() 避免 .format() 与 JSON 大括号冲突（对齐 binding/llm.py 模式）。
    """
    excerpt = (content_excerpt or "")[:max_excerpt_chars]
    return (
        template.system_prompt
        .replace("__MODULE_PATH__", module_path or "(空)")
        .replace("__TITLE__", title or "(待提炼)")
        .replace("__CONTENT_EXCERPT__", excerpt)
    )


__all__ = [
    "ASSET_PROMPT_TEMPLATES",
    "AssetPromptTemplate",
    "MEMORY_SYSTEM_PROMPT",
    "RULE_SYSTEM_PROMPT",
    "SKILL_SCHEMA",
    "SKILL_SYSTEM_PROMPT",
    "TOOL_SCHEMA",
    "TOOL_SYSTEM_PROMPT",
    "get_prompt_template",
    "render_system_prompt",
]
