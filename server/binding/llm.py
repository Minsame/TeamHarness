"""LLM Provider 抽象（Agent 5 装配服务用）。

对应 SubTask 5.3：category 自动推断（LLM 推荐 3 候选）。

依赖方：Agent 7 的 LLMProvider（POST /v1/llm/chat），未就绪时用占位实现。
切换真实调用：注入 LLMProvider 实例后 BindingService.category_suggest 自动走真实路径。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any, Protocol

logger = logging.getLogger(__name__)


class LLMChatProtocol(Protocol):
    """LLMProvider 协议（Agent 7 提供 /v1/llm/chat）。

    切换真实调用：构造满足此协议的对象注入 CategorySuggestService。
    """

    def chat(self, messages: list[dict[str, str]], *, schema: dict | None = None) -> dict[str, Any]:
        """返回 {"content": str, "usage": {...}}。"""
        ...


@dataclass
class CategoryCandidate:
    """category 候选结果。"""

    category: str
    confidence: float
    rationale: str


@dataclass
class SuggestResult:
    """category 推断结果（3 候选）。"""

    candidates: list[CategoryCandidate] = field(default_factory=list)
    used_fallback: bool = False
    error: str = ""


# category 推断 prompt（强制 JSON schema 输出，3 候选）
# 注意：prompt 内含 JSON 大括号示例，不能用 str.format()（会误解析 {candidates}），
# 改用 .replace() 注入动态字段（见 call_llm_for_category_suggestions）
CATEGORY_SUGGEST_SYSTEM_PROMPT_TEMPLATE = """你是 TeamHarness 资产分类助手。
根据资产内容与 module_path，推荐 3 个最相关的 category 候选。

规则：
1. category 必须符合两级命名 `<type>-<module>`，如 rule-backend、memory-api
2. <type> 限小写字母/数字，取值之一：rule / memory / skill / tool / prompt
3. <module> 限小写字母数字与连字符，应与 module_path 或资产主题相关
4. 返回 3 个候选，按 confidence 降序，confidence 取 0.0-1.0
5. 必须返回严格 JSON，schema:
   {"candidates": [{"category": str, "confidence": float, "rationale": str}, ...]}

资产信息：
- module_path: __MODULE_PATH__
- content 摘要（前 800 字）:
__CONTENT_EXCERPT__
"""

# 兼容旧名（避免外部 import 断裂）
CATEGORY_SUGGEST_SYSTEM_PROMPT = CATEGORY_SUGGEST_SYSTEM_PROMPT_TEMPLATE


def _parse_llm_json(content: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块围栏）。"""
    text = content.strip()
    # 剥离 ```json ... ``` 围栏
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回非合法 JSON: {exc}") from exc


def call_llm_for_category_suggestions(
    llm: LLMChatProtocol | None,
    *,
    content: str,
    module_path: str,
) -> SuggestResult:
    """调用 LLM 推荐 3 个 category 候选。

    - llm=None → 退化为关键词启发式（fallback，标记 used_fallback=True）
    - llm 给出非合法 JSON → 退化为启发式 + 记录 error
    """
    if llm is None:
        return _heuristic_suggest(content, module_path, error="llm 未注入，使用启发式")

    messages = [
        {
            "role": "system",
            "content": CATEGORY_SUGGEST_SYSTEM_PROMPT_TEMPLATE.replace(
                "__MODULE_PATH__", module_path or "(空)"
            ).replace(
                "__CONTENT_EXCERPT__", (content or "")[:800]
            ),
        },
        {"role": "user", "content": "请返回 3 个候选。"},
    ]
    schema = {
        "type": "object",
        "properties": {
            "candidates": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "category": {"type": "string"},
                        "confidence": {"type": "number"},
                        "rationale": {"type": "string"},
                    },
                    "required": ["category", "confidence", "rationale"],
                },
            }
        },
        "required": ["candidates"],
    }
    try:
        resp = llm.chat(messages, schema=schema)
        data = _parse_llm_json(str(resp.get("content", "")))
        candidates_raw = data.get("candidates", [])
        if not isinstance(candidates_raw, list) or not candidates_raw:
            raise ValueError("candidates 字段缺失或为空")
        candidates: list[CategoryCandidate] = []
        for item in candidates_raw[:3]:
            if not isinstance(item, dict):
                continue
            cat = str(item.get("category", "")).strip()
            if not cat:
                continue
            try:
                conf = float(item.get("confidence", 0.0))
            except (TypeError, ValueError):
                conf = 0.0
            candidates.append(
                CategoryCandidate(
                    category=cat,
                    confidence=max(0.0, min(1.0, conf)),
                    rationale=str(item.get("rationale", "")),
                )
            )
        if not candidates:
            raise ValueError("无有效候选")
        return SuggestResult(candidates=candidates, used_fallback=False)
    except Exception as exc:
        logger.warning("LLM category 推断失败，退化为启发式: %s", exc)
        result = _heuristic_suggest(content, module_path)
        result.error = f"llm 调用失败：{exc}"
        result.used_fallback = True
        return result


# ---------------------------------------------------------------------------
# 启发式 fallback（不依赖 LLM，根据内容关键词推断 type）
# ---------------------------------------------------------------------------

_TYPE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "rule": ("rule", "lint", "规范", "禁止", "应当"),
    "memory": ("memory", "记忆", "经验", "教训", "踩坑"),
    "skill": ("skill", "技能", "流程", "步骤"),
    "tool": ("tool", "工具", "脚本", "命令"),
    "prompt": ("prompt", "模板", "提示词"),
}


def _heuristic_suggest(content: str, module_path: str, *, error: str = "") -> SuggestResult:
    """启发式推断：根据内容关键词 + module_path 推荐 3 候选。"""
    text = (content or "").lower()
    # module 取 module_path 末段，或 "general"
    if module_path:
        module = module_path.rstrip("/").rsplit("/", 1)[-1]
    else:
        module = "general"
    # 关键词命中计数
    scores: list[tuple[str, int]] = []
    for type_name, keywords in _TYPE_KEYWORDS.items():
        score = sum(1 for kw in keywords if kw in text)
        scores.append((type_name, score))
    scores.sort(key=lambda x: x[1], reverse=True)
    # 取前 3 个 type 作为候选
    candidates: list[CategoryCandidate] = []
    for type_name, score in scores[:3]:
        confidence = 0.5 if score == 0 else min(0.9, 0.5 + score * 0.1)
        candidates.append(
            CategoryCandidate(
                category=f"{type_name}-{module}",
                confidence=confidence,
                rationale=f"启发式：关键词命中 {score} 个（{type_name}）",
            )
        )
    return SuggestResult(candidates=candidates, used_fallback=True, error=error)


__all__ = [
    "CATEGORY_SUGGEST_SYSTEM_PROMPT",
    "CategoryCandidate",
    "LLMChatProtocol",
    "SuggestResult",
    "call_llm_for_category_suggestions",
]
