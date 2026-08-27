# Agent 4: recall（召回服务）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 2（AssetIndex/EmbeddingService）

## 职责
- /v1/recall/list（索引下钻 + 权限过滤 + 向量精排）
- /v1/recall/read（从 git 读取内容，含 restricted 鉴权网关）
- /v1/sync/status（查询同步滞后）
- consistency: eventual|strict 参数（strict 模式 git fetch 实时读）
- 响应体 as_of_commit / sync_lag_seconds / degraded 标记
- DB 故障降级（内存 LRU 缓存 + 模块范围 BM25，强制 module_path）
- 离线降级（本地 git working copy 读取）
- module_path 获取方式（客户端推断/显式/LLM 推断/回退）
- 装配失效双重过滤（JOIN asset_index WHERE status='active'）
- recall/read 对已删除资产返回 410 Gone + 替代建议
- OpenTelemetry trace_id 注入

**含缺陷修复**：3.1 降级可用性、3.2 装配失效窗口、5.4 召回事务一致性

## 占位 API 契约

### 本 Agent 提供的 API
```
RecallService:
  POST /v1/recall/list (agent_id, query?, module_path?, consistency?)
    → [{asset_id, type, title, tags, relevance_score, git_path, module_path}], as_of_commit, sync_lag_seconds, degraded
  POST /v1/recall/read (agent_id, asset_id)
    → {content, frontmatter} | 410 Gone
  GET /v1/sync/status
    → {last_synced_commit, lag_seconds, sync_source}
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 2 提供：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  EmbeddingService: embed(text) / embed_batch(texts) / get_active_version()
  SyncService: trigger_sync(commit_sha) / get_sync_status() / reconcile()
  ```
- Agent 1 提供（recall/read 从 git 读取内容）：
  ```
  GitProvider: fetch(repo) / show(sha, path) / diff(sha_a, sha_b) / ls_tree(sha, path)
  ```

## SubTask 列表
- [ ] Task 4: 召回服务
  - [ ] SubTask 4.1: /v1/recall/list（索引下钻 + 权限过滤 + 向量+BM25+RRF 精排）
  - [ ] SubTask 4.2: /v1/recall/read（从 git 读取，restricted 鉴权网关）
  - [ ] SubTask 4.3: /v1/sync/status（查询同步滞后）
  - [ ] SubTask 4.4: consistency: eventual|strict 参数（strict 模式 git fetch 实时读）
  - [ ] SubTask 4.5: 响应体 as_of_commit / sync_lag_seconds / degraded 标记
  - [ ] SubTask 4.6: DB 故障降级（内存 LRU 缓存 + 模块范围 BM25，强制 module_path，未传 503）
  - [ ] SubTask 4.7: 离线降级（本地 git working copy 读取）
  - [ ] SubTask 4.8: 装配失效双重过滤（JOIN asset_index WHERE status='active'）
  - [ ] SubTask 4.9: recall/read 对已删除资产返回 410 Gone + 替代建议
  - [ ] SubTask 4.10: OpenTelemetry trace_id 注入 + recall_log 写入
  - [ ] SubTask 4.11: 域内测试（正常召回 + strict 模式 + 降级模式 + 失效资产 + 权限边界）

## 域内验证点
- [ ] /v1/recall/list 索引下钻（module_path 缩小候选集）+ 权限过滤 + 向量+BM25+RRF 精排
- [ ] /v1/recall/read 从 git 读取内容，restricted 资产走鉴权网关
- [ ] /v1/sync/status 返回 last_synced_commit + lag_seconds + sync_source
- [ ] consistency=strict 模式下 git fetch 实时读，返回最新内容
- [ ] 响应体含 as_of_commit / sync_lag_seconds / degraded 标记
- [ ] 向量库不可达时，带 module_path 的召回 2 秒内返回降级结果（degraded: true）
- [ ] 向量库不可达且未传 module_path 时返回 503
- [ ] 降级模式从内存 LRU 缓存读取，不每次 git show
- [ ] 离线时 /recall/read 从本地 git working copy 读取
- [ ] 装配失效双重过滤（JOIN asset_index WHERE status='active'）
- [ ] recall/read 对已删除资产返回 410 Gone + 替代建议
- [ ] OpenTelemetry trace_id 全链路透传
- [ ] recall_log 每次召回写入

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
