# Agent 5: binding（Agent 装配服务）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 2（AssetIndex）

## 职责
- agent_binding 表 CRUD（fixed/on-demand 类型）
- 调度索引表（task_routing + auto_bind）
- category 自动推断（PR Review 时 LLM 推荐 3 候选）
- category 受控词汇表校验（两级 <type>-<module>，<module> 须 INDEX.md 登记）
- 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）
- 角色模板（builder/reviewer/scout 默认装配）
- 装配失效同事务级联更新（webhook 删除资产时 enabled=false）
- 装配更新写时复制（新版本新行，旧版本 10 分钟清理）
- tool 资产 PR Review 强制 CODEOWNERS + 签名验证
- API 鉴权（API Key 颁发/轮换，agent_id 反查）

**含缺陷修复**：4.2 category 推广降阻、8.2 tool 执行安全

## 占位 API 契约

### 本 Agent 提供的 API
```
BindingService:
  POST /v1/binding/create (agent_id, asset_id, type, priority)
  POST /v1/binding/auto (category, task_type) → 自动匹配并绑定
  GET /v1/binding/list (agent_id) → [bindings]
  POST /v1/category/suggest (content, module_path) → [3 candidates]
  POST /v1/auth/apikey (member_id) → {api_key, agent_id}
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 2 提供：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  ```

## SubTask 列表
- [ ] Task 5: Agent 装配服务
  - [ ] SubTask 5.1: agent_binding 表 CRUD（fixed/on-demand）
  - [ ] SubTask 5.2: 调度索引表（task_routing + auto_bind 匹配）
  - [ ] SubTask 5.3: category 自动推断（LLM 推荐 3 候选，一键采纳）
  - [ ] SubTask 5.4: category 校验（两级 <type>-<module>，<module> 须 INDEX.md 登记）
  - [ ] SubTask 5.5: 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）
  - [ ] SubTask 5.6: 角色模板（builder/reviewer/scout 默认装配）
  - [ ] SubTask 5.7: 装配失效同事务级联更新（webhook 删除时 enabled=false）
  - [ ] SubTask 5.8: 装配更新写时复制（新版本新行，旧版本 10 分钟清理）
  - [ ] SubTask 5.9: tool PR Review 强制 CODEOWNERS + 签名验证
  - [ ] SubTask 5.10: API 鉴权（API Key 颁发/轮换，agent_id 反查）
  - [ ] SubTask 5.11: 域内测试（自动绑定 + 手动绑定 + 失效清理 + 鉴权 + tool 安全）

## 域内验证点
- [ ] agent_binding 表 CRUD（fixed/on-demand 类型）
- [ ] 调度索引表 auto_bind 按 category 匹配自动绑定
- [ ] category 自动推断（LLM 推荐 3 候选，一键采纳）
- [ ] category 校验：两级 <type>-<module>，<module> 须 INDEX.md 登记
- [ ] 快速模式 push main 后 post-hoc 校验，未登记自动创建 pending + 告警
- [ ] 角色模板（builder/reviewer/scout）默认装配可继承
- [ ] webhook 删除资产时同事务级联更新 agent_binding.enabled=false
- [ ] 装配更新写时复制：新版本新行，旧版本 10 分钟清理
- [ ] tool 资产 PR Review 强制 CODEOWNERS（至少一名 trusted reviewer）
- [ ] tool 文件支持签名，客户端执行前验签
- [ ] API Key 颁发/轮换可用，agent_id 从 API Key 反查

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
