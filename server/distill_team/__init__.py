"""distill_team 领域包：二级提炼（团队侧）。

对应 Agent 8 职责：
- Light 阶段增量聚类（只处理新增/修改资产，全量聚类每周日 cron）
- REM 阶段跨成员模式识别
- Deep 阶段六维评分（频率/来源多样性/泛化性/稳定性/可操作性/信噪比）+ 晋升门禁
- 冷启动旁路（资产 < 50 时门禁降为 ≥ 2，标记 confidence:low + cold_start:true）
- 种子 Prompt 库（prompts/seeds/）
- is_convention=true 单成员旁路
- 二级提炼 Prompt 模板（6 步推理链 + SKIP + 反例检验）
- LLM 强制 JSON schema + SKIP 审查区写 DREAMS.md
- 模型一致性测试集 / 反向验证基线
- job 快照隔离（启动快照 commit SHA，完成增量 delta）
- 采纳率降级（近 30 天 recall<1）
- DREAMS.md 审查界面数据接口

对外提供 API 契约（依赖方：Agent 9 治理、Agent 10 集成测试）：
- TeamDistill.trigger_incremental() → job_id
- TeamDistill.trigger_full() → job_id
- TeamDistill.get_job_status(job_id) → {status, snapshot_sha, progress}
- TeamDistill.get_cold_start_progress() → {assets_needed, current_count}
"""

from server.distill_team.service import TeamDistill

__all__ = ["TeamDistill"]
