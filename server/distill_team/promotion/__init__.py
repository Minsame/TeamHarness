"""升维管理模块（promotion）。

将 dream 的 2 层提炼扩展为符合 resource-harness 规则的 3 层结构：
- 第1层：项目级规则（.trae/rules / .cursor/rules 等，由适配器决定）
- 第2层：规则文件完整版（~/.xxx/rules/）
- 第3层：用户全局顶层（user_profile.md 热点区，不再升维）

完整流程：查重 → 回测 → 升维 → 归档 → 图谱登记 → 跨项目再升维 → 连锁更新

通过 coding 软件适配器层（adapters）支持 Trae / Cursor / Claude Code / Windsurf / Cline。
"""

from server.distill_team.promotion.archive import ArchiveManager
from server.distill_team.promotion.dedup import DedupChecker
from server.distill_team.promotion.graph import GraphRegistry
from server.distill_team.promotion.manager import PromotionOrchestrator
from server.distill_team.promotion.promote import PromotionManager
from server.distill_team.promotion.retest import RetestRunner

__all__ = [
    "ArchiveManager",
    "DedupChecker",
    "GraphRegistry",
    "PromotionManager",
    "PromotionOrchestrator",
    "RetestRunner",
]
