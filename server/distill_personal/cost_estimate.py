"""cost estimate 命令实现（SubTask 7.11）。

对应：
- teamharness cost-estimate 命令依赖 Agent 7 LLMProvider
- 切换前 ClientCLI._cmd_cost_estimate 用本地占位估算
- 切换后由本模块通过 GET /v1/llm/budget 精确查询

设计要点：
- CostEstimator 接受 LLMProviderClient（可选）
  - 有 client → 调 GET /v1/llm/budget 查询真实预算 + 估算
  - 无 client → 用本地占位算法（与 ClientCLI._cmd_cost_estimate 一致）
- 估算公式（对齐 ClientCLI 占位）：
  - total_tokens = sessions * avg_tokens_per_session
  - Light 0.2x / REM 0.3x / Deep 1.5x
  - input:output = 3:1
- 输出 CostEstimateResult，与 ClientCLI CliResult.data 格式对齐
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from server.distill_personal.llm_provider import LLMProviderClient

logger = logging.getLogger(__name__)


# 模型单价（每百万 token，美元）
DEFAULT_PRICING: dict[str, dict[str, float]] = {
    "gpt-4o-mini": {"input": 0.15, "output": 0.60},
    "gpt-4o": {"input": 2.50, "output": 10.00},
    "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
    "deepseek-chat": {"input": 0.14, "output": 0.28},
    "qwen-plus": {"input": 0.40, "output": 1.20},
}

# 三阶段 token 系数（对齐技术方案 8.6 成本控制）
LIGHT_STAGE_RATIO = 0.2
REM_STAGE_RATIO = 0.3
DEEP_STAGE_RATIO = 1.5
# input:output 比例（提炼场景 input 多于 output）
INPUT_RATIO = 0.75
OUTPUT_RATIO = 0.25


@dataclass
class CostEstimateResult:
    """成本估算结果。"""

    model: str = ""
    sessions: int = 0
    avg_tokens_per_session: int = 0
    stages: dict[str, int] = field(default_factory=dict)
    total_tokens: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    estimated_cost_usd: float = 0.0
    pricing_per_million: dict[str, float] = field(default_factory=dict)
    source: str = "local-estimate"  # local-estimate / remote-budget
    budget_info: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "model": self.model,
            "sessions": self.sessions,
            "avg_tokens_per_session": self.avg_tokens_per_session,
            "stages": dict(self.stages),
            "total_tokens": self.total_tokens,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 4),
            "pricing_per_million": dict(self.pricing_per_million),
            "source": self.source,
            "budget_info": dict(self.budget_info),
        }


class CostEstimator:
    """成本估算器。

    使用：
        estimator = CostEstimator(llm_client=client)  # 有 client → 精确查询
        # 或
        estimator = CostEstimator()  # 无 client → 本地占位

        result = estimator.estimate(
            sessions=10,
            avg_tokens=2000,
            model="gpt-4o-mini",
            member_id="alice",
        )
    """

    def __init__(
        self,
        *,
        llm_client: LLMProviderClient | None = None,
        pricing: dict[str, dict[str, float]] | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.pricing = pricing or DEFAULT_PRICING

    def estimate(
        self,
        *,
        sessions: int,
        avg_tokens: int,
        model: str = "gpt-4o-mini",
        member_id: str = "",
    ) -> CostEstimateResult:
        """估算一级提炼 LLM 成本。"""
        # 三阶段 token 计算
        total_tokens = sessions * avg_tokens
        light_tokens = int(total_tokens * LIGHT_STAGE_RATIO)
        rem_tokens = int(total_tokens * REM_STAGE_RATIO)
        deep_tokens = int(total_tokens * DEEP_STAGE_RATIO)
        all_stages_tokens = light_tokens + rem_tokens + deep_tokens

        # input/output 拆分
        input_tokens = int(all_stages_tokens * INPUT_RATIO)
        output_tokens = all_stages_tokens - input_tokens

        # 单价
        unit = self.pricing.get(model, {"input": 1.0, "output": 5.0})
        cost_usd = (
            input_tokens / 1_000_000 * unit["input"]
            + output_tokens / 1_000_000 * unit["output"]
        )

        result = CostEstimateResult(
            model=model,
            sessions=sessions,
            avg_tokens_per_session=avg_tokens,
            stages={"light": light_tokens, "rem": rem_tokens, "deep": deep_tokens},
            total_tokens=all_stages_tokens,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            estimated_cost_usd=cost_usd,
            pricing_per_million=dict(unit),
            source="local-estimate",
        )

        # 若有 LLMProviderClient，调 GET /v1/llm/budget 查询真实预算
        if self.llm_client is not None and member_id:
            try:
                budget = self.llm_client.get_budget(member_id=member_id)
                result.budget_info = budget.to_dict()
                result.source = "remote-budget"
                # 若已用 budget，实际剩余成本按剩余 token 估算
                remaining = budget.remaining
                if remaining > 0:
                    # 剩余预算能覆盖的会话数
                    covered_sessions = remaining // max(1, all_stages_tokens // max(1, sessions))
                    result.budget_info["covered_sessions_by_remaining"] = int(covered_sessions)
                else:
                    result.budget_info["covered_sessions_by_remaining"] = 0
            except Exception as exc:  # noqa: BLE001
                logger.warning("查询 /v1/llm/budget 失败，回退本地估算: %s", exc)
                result.budget_info = {"error": str(exc)}

        return result


__all__ = [
    "CostEstimateResult",
    "CostEstimator",
    "DEFAULT_PRICING",
    "DEEP_STAGE_RATIO",
    "INPUT_RATIO",
    "LIGHT_STAGE_RATIO",
    "OUTPUT_RATIO",
    "REM_STAGE_RATIO",
]
