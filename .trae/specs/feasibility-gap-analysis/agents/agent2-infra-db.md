# Agent 2: infra-db（DB 派生索引层基础）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
无

## 职责
- PG schema（asset_index/agent_binding/module_stats/recall_log/embedding_task_queue/index_sync_state/adoption_event）
- 向量库 Provider 抽象（Qdrant/PGVector）
- webhook 同步处理（读 INDEX.md 增量扫描 + embedding 计算）
- reconciliation cron（每 5 分钟补偿 webhook 丢失）
- outbox 模式（asset_index + embedding_task_queue 同事务，异步 worker 写向量库）
- 对账任务（每小时补偿 embedding_id IS NULL）
- embedding 模型双写过渡机制
- Alembic 迁移框架
- recall_log 按月分区 + asset_recall_stats 物化视图

**含缺陷修复**：1.1 一致性窗口、1.2 双存储原子性、1.3 webhook 补偿、2.4 embedding 迁移、8.1 counts 校验

## 占位 API 契约

### 本 Agent 提供的 API
```
AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
EmbeddingService: embed(text) / embed_batch(texts) / get_active_version()
SyncService: trigger_sync(commit_sha) / get_sync_status() / reconcile()
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
无

## SubTask 列表
- [ ] Task 2: DB 派生索引层基础 + 同步可靠性
  - [ ] SubTask 2.1: PG schema 全表（asset_index含module_path/category/status + agent_binding + module_stats + recall_log分区 + embedding_task_queue + index_sync_state + adoption_event + asset_recall_stats物化视图）
  - [ ] SubTask 2.2: VectorStore Provider 抽象（Qdrant/PGVector 两实现）
  - [ ] SubTask 2.3: webhook 同步处理（读 INDEX.md 增量扫描 + embedding 计算）
  - [ ] SubTask 2.4: outbox 模式（asset_index + embedding_task_queue 同事务，异步 worker 写向量库）
  - [ ] SubTask 2.5: reconciliation cron（每5分钟 git fetch 比对 last_synced_commit，主动补同步，commit SHA 幂等）
  - [ ] SubTask 2.6: 对账任务（每小时补偿 embedding_id IS NULL）
  - [ ] SubTask 2.7: embedding 模型双写过渡（新旧两表 + 后台补齐 + active_embedding_version 控制）
  - [ ] SubTask 2.8: Alembic 迁移框架 + 初始 migration
  - [ ] SubTask 2.9: recall_log 按月分区 + 6个月 TTL
  - [ ] SubTask 2.10: INDEX.md counts 服务端校验（不一致告警，不阻断）
  - [ ] SubTask 2.11: 域内测试（同步流程 + outbox 一致性 + reconciliation 补偿 + 分区表）

## 域内验证点
- [ ] PG schema 全表创建（asset_index 含 module_path/category/status + agent_binding + module_stats + recall_log 分区 + embedding_task_queue + index_sync_state + adoption_event + asset_recall_stats 物化视图）
- [ ] VectorStore Provider 两实现（Qdrant/PGVector）均可 upsert/search/delete
- [ ] webhook 同步：读 INDEX.md 增量扫描，不全仓库扫描
- [ ] outbox 模式：asset_index + embedding_task_queue 同事务写入
- [ ] 异步 worker 消费队列写向量库，成功回写 embedding_id
- [ ] embedding 服务超时时，asset_index 已提交但 embedding 延迟补偿（1 小时内）
- [ ] embedding_id 为空时记 metric 告警（不静默降级）
- [ ] reconciliation cron 每 5 分钟运行，webhook 全部丢失时 5 分钟内补同步
- [ ] reconciliation 连续 3 周期滞后触发告警
- [ ] commit SHA 幂等去重（同一 commit 多次触发只处理一次）
- [ ] embedding 模型切换时新旧两套双写
- [ ] 过渡期召回融合两套向量结果（RRF）
- [ ] 全量迁移完成后 drop 旧表
- [ ] Alembic 迁移框架可用，初始 migration 可执行
- [ ] recall_log 按月分区，6 个月 TTL 生效
- [ ] INDEX.md counts 与实际资产数不一致时告警（不阻断）

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
