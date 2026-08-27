"""Agent 10 集成测试包。

包含跨模块全链路联通测试、公共 API 契约验证、角色权限跨越测试、
治理看板视觉验证、可行性缺陷 checklist 验证、Bug 触发测试回归用例。

测试基础设施：
- SQLite 内存库（StaticPool）替代 PG，避免外部依赖
- InMemoryVectorStore 替代 Qdrant/PGVector
- mock GitProvider / RestrictedReader 避免真实 git 操作
- mock LLMChatLike 避免真实 LLM 调用
- 真实 SyncService / RecallService / TeamDistill / DashboardService 实例

对应 SubTask 10.1 ~ 10.6。
"""
