# TeamHarness 多 Agent 执行方案 Spec

## Why
TeamHarness 是团队 harness 资产共享系统，涉及 10+ 独立功能域（仓库管理、DB 索引、召回、装配、两级提炼、治理、客户端、部署），预计代码量 > 5000 行、文件 > 50 个。单 Agent 串行开发周期长且上下文易爆。采用多 Agent 协作可并行推进、隔离上下文、加速交付。

## What Changes
- 将 TeamHarness 完整方案拆解为 10 个专职 Agent，分 4 波推进
- 定义 Agent 间占位 API 契约，解耦依赖
- 引入主 Agent 协调机制 + 三层测试体系
- 整合可行性缺陷修复任务到各 Agent 职责中

## Impact
- 全部模块按 Agent 划分重新组织
- 每个 Agent 负责其域内的后端 + 域内测试
- 主 Agent 只协调分发，不直接写业务代码

## Agent 划分

### 第一波：基础设施（可并行，无相互依赖）

#### Agent 1: infra-git（仓库与 Git Provider）
**职责**：
- Git Provider 抽象层（GitLab/Gitea/libgit2 切换，含 VectorStore Provider）
- 分层仓库结构（项目级/模块级/子模块级递归）
- INDEX.md 规范与防孤岛 CI 校验
- webhook 接收端点（secret 签名校验）
- Trae 深度适配（frontmatter 双区设计、会话路径自动探测）
- categories.yaml 受控词汇表管理
- DREAMS.md 按月切分与归档
- shallow clone 支持

**含缺陷修复**：4.1 多软件适配收敛、4.3 restricted 读权限（git-crypt/独立仓库）、2.2 仓库 GC

**占位 API 契约（本 Agent 提供）**：
```
GitProvider: fetch(repo) / show(sha, path) / diff(sha_a, sha_b) / ls_tree(sha, path)
WebhookReceiver: POST /v1/webhook/git (接收 GitLab/Gitea webhook)
```

#### Agent 2: infra-db（DB 派生索引层基础）
**职责**：
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

**占位 API 契约（本 Agent 提供）**：
```
AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
EmbeddingService: embed(text) / embed_batch(texts) / get_active_version()
SyncService: trigger_sync(commit_sha) / get_sync_status() / reconcile()
```

#### Agent 3: deploy（部署与升级）
**职责**：
- All-in-One 单二进制（内嵌 SQLite + PGVector + libgit2）
- docker-compose 一键部署（PG + Qdrant + 三服务 + Gitea）
- 单机模式每日 cron 备份
- 升级流程文档与迁移脚本框架
- API 语义化版本（/v1/ 锁定，/v2/ 破坏性）
- frontmatter schema_version 兼容解析

**含缺陷修复**：7.1 单机部署、7.3 升级策略

**占位 API 契约（本 Agent 提供）**：
```
DeployConfig: get_mode() / get_storage_backend() / get_version()
```

### 第二波：核心服务（依赖第一波，可并行）

#### Agent 4: recall（召回服务）
**依赖**：Agent 2（AssetIndex/EmbeddingService）
**职责**：
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

**占位 API 契约（本 Agent 提供）**：
```
RecallService:
  POST /v1/recall/list (agent_id, query?, module_path?, consistency?)
    → [{asset_id, type, title, tags, relevance_score, git_path, module_path}], as_of_commit, sync_lag_seconds, degraded
  POST /v1/recall/read (agent_id, asset_id)
    → {content, frontmatter} | 410 Gone
  GET /v1/sync/status
    → {last_synced_commit, lag_seconds, sync_source}
```

#### Agent 5: binding（Agent 装配服务）
**依赖**：Agent 2（AssetIndex）
**职责**：
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

**占位 API 契约（本 Agent 提供）**：
```
BindingService:
  POST /v1/binding/create (agent_id, asset_id, type, priority)
  POST /v1/binding/auto (category, task_type) → 自动匹配并绑定
  GET /v1/binding/list (agent_id) → [bindings]
  POST /v1/category/suggest (content, module_path) → [3 candidates]
  POST /v1/auth/apikey (member_id) → {api_key, agent_id}
```

#### Agent 6: client（客户端）
**依赖**：Agent 1（GitProvider/Trae 适配）
**职责**：
- 本地记忆文件夹读写（= git working copy）
- git 同步封装（sync/pr/冲突 diff 辅助）
- mapping.yaml 目录映射（两层适配模型）
- 召回客户端（调用 /v1/recall/* API / 离线降级本地文件）
- module_path 上下文推断（从 coding 软件当前路径反查）
- 配置管理（服务端地址/API Key/同步策略）
- CLI（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）
- 守护进程（定时一级提炼调度/网络检测/离线召回代理）
- 采纳率上报（尽力而为，本地缓存批量 flush）
- 私有资产隔离（.teamharness/private/ + .gitignore）
- manifest.json 本地缓存索引（从 INDEX.md + 资产派生）

**占位 API 契约（本 Agent 提供）**：
```
ClientCLI:
  teamharness sync / pr / recall / category-suggest / cost-estimate / index-reconcile
ClientDaemon:
  定时一级提炼调度 / 网络状态检测 / 采纳率批量上报
```

### 第三波：提炼引擎（依赖第二波，可并行）

#### Agent 7: distill-personal（一级提炼）
**依赖**：Agent 6（client）、Agent 2（LLM Provider 接入）
**职责**：
- SessionProvider 抽象（Trae 适配 + 通用 JSONL 兜底）
- 对话记录增量采集
- Light 阶段（信号筛选 + L0→L1 原子事实抽取）
- REM 阶段（意图归纳，区分一次性上下文 vs 可复用经验）
- Deep 阶段（五维评分 + 结构化固化，产出带 frontmatter 资产）
- 四类资产子 Prompt 模板（rule/memory/skill/tool）
- LLM 默认走服务端代理（统一计费与模型版本）
- 每成员 daily_token_budget + 超限降级（Deep 跳过，候选入 pending）
- Light 阶段候选信号计数上报（预算动态调整）
- 隐私保护（对话不离开本机，只上传结构化资产）
- LLM 强制 JSON schema 输出 + schema 校验失败重试
- cost estimate 命令实现

**含缺陷修复**：2.1 LLM 成本归属、5.2 提示词跨模型一致性（一级提炼部分）

**占位 API 契约（本 Agent 提供）**：
```
PersonalDistill:
  run_light(sessions) → signals
  run_rem(signals) → intents
  run_deep(intents, budget) → {assets, pending}
  report_metrics(member_id, signal_count, yield_ratio)
LLMProvider (服务端代理):
  POST /v1/llm/chat (messages, schema) → {content, usage}
  GET /v1/llm/budget (member_id) → {daily_token_budget, used}
```

#### Agent 8: distill-team（二级提炼）
**依赖**：Agent 2（AssetIndex）、Agent 4（recall_log 统计）
**职责**：
- Light 阶段增量聚类（只处理新增/修改资产，全量聚类每周 cron）
- REM 阶段跨成员模式识别
- Deep 阶段六维评分（频率/来源多样性/泛化性/稳定性/可操作性/信噪比）
- 晋升门禁（冷启动期动态化：资产 < 50 时来源多样性 ≥ 2）
- 冷启动产出标记 confidence: low + cold_start: true
- 种子 Prompt 库（prompts/seeds/）
- is_convention=true 单成员旁路
- 二级提炼 Prompt 模板（6 步推理链 + SKIP 机制 + 反例检验）
- LLM 强制 JSON schema + SKIP 审查区（每周人工抽查 10%）
- 模型一致性测试集（20 条标准资产簇）
- 反向验证基线（冷启动用公开 Prompt 数据集）
- job 快照隔离（启动时快照 commit SHA，完成后增量 delta）
- distillation_job 表 trigger_source/cluster_fingerprint
- 采纳率降级（近 30 天 recall < 1 → 自动降级）
- DREAMS.md 审查界面数据

**含缺陷修复**：2.3 增量聚类、5.1 冷启动旁路、5.2 提示词一致性（二级提炼部分）、5.3 提炼 job 竞态

**占位 API 契约（本 Agent 提供）**：
```
TeamDistill:
  trigger_incremental() → job_id
  trigger_full() → job_id (cron 周日)
  get_job_status(job_id) → {status, snapshot_sha, progress}
  get_cold_start_progress() → {assets_needed, current_count}
```

#### Agent 9: governance（治理与可观测性）
**依赖**：Agent 2（module_stats/recall_log）、Agent 8（distillation_job）
**职责**：
- PR Review 语义去重（≥0.92 相似度，LLM 判断归并 vs 独立）
- 语义归并（移入 archive/<date>/，6 个月后删除文件）
- 治理看板（模块资产数/拆分建议/未登记告警/召回命中率/采纳率）
- 拆分判定（基于 asset_index 实时查询，非人维护 counts）
- 指标采集（Prometheus + Grafana，客户端 /v1/metrics 批量上报）
- 指标文档化（指标名|采集组件|埋点位置|标签|聚合方式|告警阈值）
- 采纳率服务端可采（recall 次数 + read 次数，客户端上报作辅助）
- 过期归档（长期未引用资产）
- Owner 接管流程
- 仓库大小告警（500MB 阈值）
- module_stats 从 asset_index 实时派生（不依赖人维护 counts）
- teamharness index reconcile 命令

**含缺陷修复**：6.1 指标落地、6.3 采纳率服务端可采、8.1 counts 派生

**占位 API 契约（本 Agent 提供）**：
```
GovernanceService:
  POST /v1/review/dedup (pr_id, assets) → {duplicates, suggestions}
  GET /v1/governance/dashboard → {module_stats, split_suggestions, alerts}
  POST /v1/metrics (batch) → ack (客户端上报)
  GET /v1/metrics/dashboard → Grafana 嵌入
```

### 第四波：集成测试

#### Agent 10: integration-test（集成测试）
**依赖**：全部 Agent
**职责**：
- 跨模块全链路联通测试
- 公共 API 契约验证
- 角色权限跨越测试
- 三层测试体系执行（域内测试汇总 + 集成测试 + 视觉验证）
- Bug 触发测试（用户报告的 bug 添加回归用例）
- 可行性缺陷 checklist 验证

## 启动顺序与依赖关系

```
第一波（并行）:
  Agent 1 infra-git ─────┐
  Agent 2 infra-db ──────┼──► 第二波（并行）:
  Agent 3 deploy ────────┘    Agent 4 recall ────────┐
                              Agent 5 binding ───────┼──► 第三波（并行）:
                              Agent 6 client ────────┘    Agent 7 distill-personal ──┐
                                                          Agent 8 distill-team ──────┼──► 第四波:
                                                          Agent 9 governance ────────┘    Agent 10 integration-test
```

## 占位 API 契约汇总

主 Agent 在启动第一波时即定义全部公共 API 契约（见各 Agent 职责中的"占位 API 契约"），各 Agent：
1. 先实现自己提供的 API
2. 依赖其他 Agent 的 API 时先写占位函数（返回 mock 数据，格式准确）
3. 占位不影响自己的其他 API 和测试开发
4. 实现 Agent 完成后 → 通知主 Agent → 通知依赖方切换真实调用

## 协作规则

### 主 Agent 红线
1. 禁止直接修改业务代码，只协调、汇报、分发
2. 禁止深入阅读子 Agent 领域的文件
3. 禁止替子 Agent 做技术决策
4. 收到用户报错时识别错误所属领域 → 转发给对应 Agent

### 子 Agent 汇报规范
```
## 根因
[文件:行数 — 一句话说明问题]

## 修复
- [file_path] — [具体改动]

## 验证
- 构建: PASS/FAIL
- 测试: N PASS, M FAIL
```

### 递归层数控制
- 主对话 → 子 Agent（第 1 层，可再启动 1 层子子 Agent）
- 子 Agent → 子子 Agent（第 2 层，不得再启动）
- 集成测试 Agent 启动的验证任务为第 2 层

### 规则传递
派发子 Agent 时内联规则摘要：
```
[规则摘要]
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
```

## 三层测试体系

| 层级 | 负责方 | 内容 |
|------|--------|------|
| 第一层：域内测试 | 各 Agent 自己 | 后端 API 测试（正常+异常+权限边界）、数据测试 |
| 第二层：集成测试 | Agent 10 | 跨模块全链路联通、公共 API 契约验证、角色权限跨越 |
| 第三层：视觉验证 | Agent 10 | 治理看板截图对比、构建无 console error |

## MODIFIED Requirements

### Requirement: 里程碑（映射到 Agent 波次）
- **M1 = 第一波 + 第二波基础**：Agent 1/2/3 并行 → Agent 4/5/6 基础功能
- **M2 = 第二波完善**：Agent 4/5/6 完善功能 + 缺陷修复
- **M3 = 第三波个人提炼**：Agent 7 + Agent 6 客户端完善
- **M4 = 第三波团队提炼 + 治理**：Agent 8 + Agent 9
- **M5 = 装配增强 + 其他软件适配**：Agent 5 高级特性 + Cursor 适配
- **M6 = 集成测试 + 可观测性**：Agent 10 + Agent 9 指标落地
