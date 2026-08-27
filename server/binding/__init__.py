"""binding 领域包：Agent 装配服务（Agent 5）。

对应技术方案 Agent 5 职责：
- agent_binding 表 CRUD（fixed/on-demand 类型）
- 调度索引表 task_routing + auto_bind 匹配
- category 自动推断（LLM 推荐 3 候选，一键采纳）
- category 校验（两级 <type>-<module>，<module> 须 INDEX.md 登记）
- 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）
- 角色模板（builder/reviewer/scout 默认装配）
- 装配失效同事务级联更新（webhook 删除资产时 enabled=false）
- 装配更新写时复制（新版本新行，旧版本 10 分钟清理）
- tool 资产 PR Review 强制 CODEOWNERS + 签名验证
- API 鉴权（API Key 颁发/轮换，agent_id 反查）

对外提供占位 API 契约（依赖方：Agent 6、Agent 10）：
- BindingService: /v1/binding/*、/v1/category/suggest、/v1/auth/apikey
"""

from server.binding.auth_service import AgentApiKeyService
from server.binding.binding_service import BindingService
from server.binding.category_suggest import CategorySuggestService
from server.binding.tool_review import ToolReviewService

__all__ = [
    "AgentApiKeyService",
    "BindingService",
    "CategorySuggestService",
    "ToolReviewService",
]
