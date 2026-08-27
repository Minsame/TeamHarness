"""二级提炼 Prompt 模板（SubTask 8.8 + 8.9）。

6 步推理链（强制 LLM 按此链路推理）：
1. 识别共同主题：从簇内资产提取共同问题/经验
2. 抽象通用模式：去除具体细节，提炼可复用模式
3. 生成 Prompt 草稿：基于通用模式生成候选 Prompt
4. 反例检验：构造冲突场景测试 Prompt 是否过度泛化
5. 稳定性检查：检验是否过拟合到某成员/某模块
6. 决策：PROMOTE（晋升）/ SKIP（跳过 + 原因）

SKIP 机制：
- 反例检验失败 → SKIP（reason: counter_example_failed）
- 稳定性不足 → SKIP（reason: overfit_to_member / overfit_to_module）
- 总分过低 → SKIP（reason: low_score）
- SKIP 候选写入 DREAMS.md SKIP 审查区，每周人工抽查 10%

注意：prompt 模板含 JSON 大括号，禁用 str.format()（按 gotchas.md 规则），
改用 .replace() + 自定义占位符 __X__。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 6 步推理链 system prompt（含 JSON schema 输出约束）
# ---------------------------------------------------------------------------

# 占位符：__CLUSTER_INFO__ / __ASSETS_EXCERPT__ / __SCORE_INFO__
# 注意：含 JSON 大括号，禁用 str.format()
DISTILL_SYSTEM_PROMPT_TEMPLATE = """你是 TeamHarness 二级提炼引擎。

任务：基于一组跨成员重复出现的资产簇，提炼出可复用的团队级 Prompt。

# 6 步推理链（必须按序执行）

## 步骤 1：识别共同主题
分析簇内资产，提取共同问题/经验/模式。回答：
- 这些资产解决的核心问题是什么？
- 跨成员重复出现的主题是什么？

## 步骤 2：抽象通用模式
去除具体成员/模块细节，提炼可复用模式。回答：
- 通用模式是什么？
- 适用范围（哪些场景/任务）？

## 步骤 3：生成 Prompt 草稿
基于通用模式生成候选 Prompt。要求：
- 标题简洁明确
- 内容含明确指令（应当/禁止/必须）
- 避免成员特定细节

## 步骤 4：反例检验
构造至少 2 个冲突场景测试 Prompt 是否过度泛化：
- 场景 A：Prompt 适用但实际会误导的情况
- 场景 B：Prompt 不适用但被错误触发的情况
若任一场景命中 → counter_example_pass=false

## 步骤 5：稳定性检查
- 是否过拟合到某成员？（如引用了成员私有路径/命名）
- 是否过拟合到某模块？（如仅适用于单一技术栈）
- 若过拟合 → 标记 overfit=true

## 步骤 6：决策
基于步骤 4/5 结果决策：
- counter_example_pass=true 且 overfit=false → decision=PROMOTE
- 否则 → decision=SKIP，填写 skip_reason

# 输入

## 簇信息
__CLUSTER_INFO__

## 资产摘要（前 1500 字）
__ASSETS_EXCERPT__

## Deep 评分
__SCORE_INFO__

# 输出（严格 JSON schema）

```json
{
  "step1_topic": "string，共同主题",
  "step2_pattern": "string，通用模式",
  "step3_draft": {
    "title": "string，Prompt 标题",
    "content": "string，Prompt 内容"
  },
  "step4_counter_examples": [
    {"scenario": "string", "hit": false}
  ],
  "step4_counter_example_pass": true,
  "step5_overfit": false,
  "step5_overfit_reason": "string，若 overfit=true 说明原因",
  "step6_decision": "PROMOTE 或 SKIP",
  "skip_reason": "string，SKIP 时填写"
}
```

约束：
- 必须返回严格 JSON，无额外文本
- decision 取值仅 PROMOTE / SKIP
- counter_example_pass=false 时 decision 必须 SKIP
- overfit=true 时 decision 必须 SKIP
"""


# ---------------------------------------------------------------------------
# LLM 调用 + JSON 解析
# ---------------------------------------------------------------------------


# 复用 binding/llm.py 的 LLMChatProtocol 协议（避免重复定义）
# 但为减少跨域依赖，本域独立定义 Protocol（与 binding.llm.LLMChatProtocol 结构一致）
class LLMChatProtocol:
    """LLM Provider 协议（Agent 7 提供 /v1/llm/chat）。

    切换真实调用：构造满足此协议的对象注入 DistillPromptRunner。
    """

    def chat(
        self, messages: list[dict[str, str]], *, schema: dict | None = None
    ) -> dict[str, Any]:
        """返回 {"content": str, "usage": {...}}。"""
        raise NotImplementedError


@dataclass
class DistillLLMResult:
    """LLM 二级提炼结果。"""

    step1_topic: str = ""
    step2_pattern: str = ""
    draft_title: str = ""
    draft_content: str = ""
    counter_examples: list[dict[str, Any]] = field(default_factory=list)
    counter_example_pass: bool = True
    overfit: bool = False
    overfit_reason: str = ""
    decision: str = "SKIP"  # PROMOTE / SKIP
    skip_reason: str = ""
    used_fallback: bool = False
    error: str = ""

    @property
    def is_skip(self) -> bool:
        return self.decision == "SKIP"


def _parse_llm_json(content: str) -> dict[str, Any]:
    """解析 LLM 返回的 JSON（容错：剥离 markdown 代码块围栏）。"""
    text = content.strip()
    fence = re.match(r"^```(?:json)?\s*\n(.*?)\n```\s*$", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM 返回非合法 JSON: {exc}") from exc


def build_cluster_info(cluster_dict: dict[str, Any]) -> str:
    """构造簇信息文本（用于 prompt 注入）。"""
    lines = [
        f"- cluster_id: {cluster_dict.get('cluster_id', '')}",
        f"- category: {cluster_dict.get('category', '(无)')}",
        f"- asset_count: {cluster_dict.get('size', 0)}",
        f"- owners: {', '.join(cluster_dict.get('owners', []))}",
        f"- module_paths: {', '.join(cluster_dict.get('module_paths', []))}",
        f"- is_convention: {cluster_dict.get('is_convention', False)}",
    ]
    return "\n".join(lines)


def build_assets_excerpt(assets: list[dict[str, Any]], *, max_chars: int = 1500) -> str:
    """构造资产摘要文本（截断到 max_chars）。"""
    parts: list[str] = []
    total = 0
    for asset in assets:
        excerpt = (
            f"### {asset.get('id', '')} (owner={asset.get('owner', '')}, "
            f"module={asset.get('module_path', '')})\n"
            f"{(asset.get('content', '') or '')[:500]}\n"
        )
        if total + len(excerpt) > max_chars:
            break
        parts.append(excerpt)
        total += len(excerpt)
    return "\n".join(parts) if parts else "(无资产)"


def build_score_info(score_dict: dict[str, Any]) -> str:
    """构造 Deep 评分文本。"""
    return (
        f"- frequency: {score_dict.get('frequency', 0):.2f}\n"
        f"- source_diversity: {score_dict.get('source_diversity', 0):.2f}\n"
        f"- generalizability: {score_dict.get('generalizability', 0):.2f}\n"
        f"- stability: {score_dict.get('stability', 0):.2f}\n"
        f"- actionability: {score_dict.get('actionability', 0):.2f}\n"
        f"- snr: {score_dict.get('snr', 0):.2f}\n"
        f"- total: {score_dict.get('total', 0):.2f}"
    )


# JSON schema（传给 LLM 强制结构化输出）
DISTILL_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "step1_topic": {"type": "string"},
        "step2_pattern": {"type": "string"},
        "step3_draft": {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "content": {"type": "string"},
            },
            "required": ["title", "content"],
        },
        "step4_counter_examples": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "scenario": {"type": "string"},
                    "hit": {"type": "boolean"},
                },
            },
        },
        "step4_counter_example_pass": {"type": "boolean"},
        "step5_overfit": {"type": "boolean"},
        "step5_overfit_reason": {"type": "string"},
        "step6_decision": {"type": "string", "enum": ["PROMOTE", "SKIP"]},
        "skip_reason": {"type": "string"},
    },
    "required": [
        "step1_topic",
        "step2_pattern",
        "step3_draft",
        "step4_counter_example_pass",
        "step5_overfit",
        "step6_decision",
        "skip_reason",
    ],
}


def run_distill_llm(
    llm: LLMChatProtocol | None,
    *,
    cluster_info: str,
    assets_excerpt: str,
    score_info: str,
) -> DistillLLMResult:
    """调用 LLM 执行 6 步推理链。

    - llm=None → 退化为启发式 fallback（标记 used_fallback=True）
    - LLM 返回非合法 JSON → 退化为启发式 + 记录 error
    """
    if llm is None:
        return _heuristic_distill(
            cluster_info, assets_excerpt, score_info,
            error="llm 未注入，使用启发式"
        )

    system_prompt = (
        DISTILL_SYSTEM_PROMPT_TEMPLATE
        .replace("__CLUSTER_INFO__", cluster_info)
        .replace("__ASSETS_EXCERPT__", assets_excerpt)
        .replace("__SCORE_INFO__", score_info)
    )
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": "请按 6 步推理链执行二级提炼，返回严格 JSON。"},
    ]
    try:
        resp = llm.chat(messages, schema=DISTILL_OUTPUT_SCHEMA)
        data = _parse_llm_json(str(resp.get("content", "")))
        return _parse_distill_result(data)
    except Exception as exc:
        logger.warning("LLM 二级提炼失败，退化为启发式: %s", exc)
        result = _heuristic_distill(cluster_info, assets_excerpt, score_info)
        result.error = f"llm 调用失败：{exc}"
        result.used_fallback = True
        return result


def _parse_distill_result(data: dict[str, Any]) -> DistillLLMResult:
    """解析 LLM 返回的 JSON 为 DistillLLMResult。"""
    draft = data.get("step3_draft", {}) or {}
    decision = str(data.get("step6_decision", "SKIP")).upper()
    if decision not in ("PROMOTE", "SKIP"):
        decision = "SKIP"

    counter_examples = data.get("step4_counter_examples", []) or []
    if not isinstance(counter_examples, list):
        counter_examples = []

    counter_pass = bool(data.get("step4_counter_example_pass", True))
    overfit = bool(data.get("step5_overfit", False))

    # 强制一致性：counter_pass=false 或 overfit=true 时 decision 必须 SKIP
    if (not counter_pass or overfit) and decision == "PROMOTE":
        decision = "SKIP"

    skip_reason = str(data.get("skip_reason", ""))
    if decision == "SKIP" and not skip_reason:
        reasons = []
        if not counter_pass:
            reasons.append("counter_example_failed")
        if overfit:
            reasons.append("overfit")
        skip_reason = "; ".join(reasons) if reasons else "low_quality"

    return DistillLLMResult(
        step1_topic=str(data.get("step1_topic", "")),
        step2_pattern=str(data.get("step2_pattern", "")),
        draft_title=str(draft.get("title", "")),
        draft_content=str(draft.get("content", "")),
        counter_examples=[
            {"scenario": str(ce.get("scenario", "")), "hit": bool(ce.get("hit", False))}
            for ce in counter_examples
            if isinstance(ce, dict)
        ],
        counter_example_pass=counter_pass,
        overfit=overfit,
        overfit_reason=str(data.get("step5_overfit_reason", "")),
        decision=decision,
        skip_reason=skip_reason,
    )


# ---------------------------------------------------------------------------
# 启发式 fallback（不依赖 LLM，根据评分简单决策）
# ---------------------------------------------------------------------------


def _heuristic_distill(
    cluster_info: str,
    assets_excerpt: str,
    score_info: str,
    *,
    error: str = "",
) -> DistillLLMResult:
    """启发式 fallback：基于 score_info 中的 total 简单决策。

    - total >= 0.6 → PROMOTE
    - total < 0.6 → SKIP (low_score)
    - 无反例检验能力 → counter_example_pass=True（乐观）
    - 无过拟合检测能力 → overfit=False（乐观）
    """
    # 从 score_info 解析 total
    total = 0.0
    for line in score_info.splitlines():
        if line.startswith("- total:"):
            try:
                total = float(line.split(":", 1)[1].strip())
            except ValueError:
                pass
            break

    decision = "PROMOTE" if total >= 0.6 else "SKIP"
    skip_reason = "" if decision == "PROMOTE" else f"low_score (total={total:.2f})"

    # 从 cluster_info 提取简单标题
    title = "（启发式产出）"
    for line in cluster_info.splitlines():
        if line.startswith("- category:"):
            cat = line.split(":", 1)[1].strip()
            if cat and cat != "(无)":
                title = f"{cat} 通用规范"
            break

    return DistillLLMResult(
        step1_topic="（启发式：未调用 LLM）",
        step2_pattern="（启发式：基于评分阈值决策）",
        draft_title=title,
        draft_content=f"# {title}\n\n（启发式产出，需人工审查）\n",
        counter_example_pass=True,
        overfit=False,
        decision=decision,
        skip_reason=skip_reason,
        used_fallback=True,
        error=error,
    )


__all__ = [
    "DISTILL_OUTPUT_SCHEMA",
    "DISTILL_SYSTEM_PROMPT_TEMPLATE",
    "DistillLLMResult",
    "LLMChatProtocol",
    "build_assets_excerpt",
    "build_cluster_info",
    "build_score_info",
    "run_distill_llm",
]
