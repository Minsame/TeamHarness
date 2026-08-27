"""冷启动旁路（SubTask 8.5）。

冷启动期：active 资产 < 50 时
- 门禁降级：来源多样性 ≥ 2（正常期 ≥ 3）、被召回 ≥ 2 次（正常期 ≥ 3）
- 产出标记：confidence=low + cold_start=true
- 总分门禁跳过（冷启动期资产稀疏，分数不稳定）

冷启动产出可在 RecallService 召回时被降权显示，提醒用户人工复核。
"""

from __future__ import annotations

import logging

from server.distill_team.deep import PromotionGate
from server.distill_team.models import DistilledPrompt

logger = logging.getLogger(__name__)


class ColdStartBypass:
    """冷启动旁路管理器。

    用法：
        bypass = ColdStartBypass(promotion_gate)
        if bypass.is_cold_start():
            prompt = bypass.apply_cold_start_marking(prompt)
    """

    # 冷启动期 confidence 等级（强制 low）
    COLD_START_CONFIDENCE = "low"

    def __init__(self, promotion_gate: PromotionGate) -> None:
        self._gate = promotion_gate

    def is_cold_start(self) -> bool:
        """判定是否处于冷启动期。"""
        return self._gate.is_cold_start()

    def get_progress(self) -> tuple[int, int, bool]:
        """返回 (assets_needed, current_count, is_cold_start)。"""
        return self._gate.get_cold_start_progress()

    def apply_cold_start_marking(self, prompt: DistilledPrompt) -> DistilledPrompt:
        """对冷启动期产出的 Prompt 强制标记。

        - confidence=low（即使门禁通过也降为 low）
        - cold_start=true
        - gate.cold_start=true（若未设置）
        """
        prompt.cold_start = True
        prompt.confidence = self.COLD_START_CONFIDENCE
        prompt.gate.cold_start = True
        # 冷启动期产出建议进入 SKIP 审查区（人工抽查）
        # 但不强制（避免所有冷启动产出都进审查区，10% 抽样在 LLM 层做）
        logger.info(
            "冷启动期产出标记 prompt_id=%s cluster=%s confidence=low",
            prompt.prompt_id,
            prompt.cluster_id,
        )
        return prompt


__all__ = ["ColdStartBypass"]
