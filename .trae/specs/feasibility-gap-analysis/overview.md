# TeamHarness 多 Agent 协调总览（主 Agent 操作手册）

> 本文件是主 Agent 的协调操作手册。读完本文件即可：判断子 Agent 是否按预期推进、发现死循环/卡住/跑偏、引导纠错、必要时重启任务。
> 技术实现细节在各 `agents/agentX-xxx.md`，本文件不重复，只写协调决策所需信息。

## 0. 项目背景与文件地址

TeamHarness 是团队 harness 资产共享系统，涉及 10 个独立功能域。完整技术方案见 `d:\Code\TeamHarness\TECH_PROPOSAL.md`。

**方案文件绝对路径前缀**：`d:\Code\TeamHarness\.trae\specs\feasibility-gap-analysis\`
- 各 Agent 方案文件：`agents\agentN-名称.md`（含 SubTask 清单与域内验证点，**主 Agent 不深入阅读**，只在子 Agent 卡住时按需查询对应卡片引用的 SubTask）
- 原 spec：`spec.md`（含完整架构说明，可选读）

### Agent 列表

| Agent | 名称 | 方案文件 | 依赖 | 波次 |
|-------|------|----------|------|------|
| 1 | infra-git | `agents/agent1-infra-git.md` | 无 | 第一波 |
| 2 | infra-db | `agents/agent2-infra-db.md` | 无 | 第一波 |
| 3 | deploy | `agents/agent3-deploy.md` | 无 | 第一波 |
| 4 | recall | `agents/agent4-recall.md` | Agent 2 | 第二波 |
| 5 | binding | `agents/agent5-binding.md` | Agent 2 | 第二波 |
| 6 | client | `agents/agent6-client.md` | Agent 1 | 第二波 |
| 7 | distill-personal | `agents/agent7-distill-personal.md` | Agent 6, Agent 2 | 第三波 |
| 8 | distill-team | `agents/agent8-distill-team.md` | Agent 2, Agent 4 | 第三波 |
| 9 | governance | `agents/agent9-governance.md` | Agent 2, Agent 8 | 第三波（Agent 8 完成后） |
| 10 | integration-test | `agents/agent10-integration-test.md` | 全部 | 第四波 |

### 启动顺序与依赖图

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

> **关键路径**：Agent 2 是最大依赖源（Agent 4/5/7/8/9 全依赖它），Agent 2 卡住会阻塞整条链路 → 优先关注 Agent 2 进度。

## 1. 占位 API 契约汇总（主 Agent 判断依赖就绪的依据）

主 Agent 在启动第一波时即向所有 Agent 宣示全部公共 API 契约。判断"依赖是否就绪"看下表对应 Agent 是否回报完成。

| 提供 Agent | 提供的 API | 依赖方 |
|------------|-----------|--------|
| Agent 1 | `GitProvider: fetch/show/diff/ls_tree`、`WebhookReceiver: POST /v1/webhook/git` | Agent 4、Agent 6 |
| Agent 2 | `AssetIndex: upsert/delete/query/get_status`、`EmbeddingService: embed/embed_batch/get_active_version`、`SyncService: trigger_sync/get_sync_status/reconcile` | Agent 4、5、7、8、9 |
| Agent 3 | `DeployConfig: get_mode/get_storage_backend/get_version` | Agent 10 |
| Agent 4 | `RecallService: /v1/recall/list`、`/v1/recall/read`、`/v1/sync/status` | Agent 6、Agent 8、Agent 10 |
| Agent 5 | `BindingService: /v1/binding/*`、`/v1/category/suggest`、`/v1/auth/apikey` | Agent 6、Agent 10 |
| Agent 6 | `ClientCLI`、`ClientDaemon` | Agent 7、Agent 10 |
| Agent 7 | `PersonalDistill`、`LLMProvider: /v1/llm/chat`、`/v1/llm/budget` | Agent 10 |
| Agent 8 | `TeamDistill: trigger_incremental/trigger_full/get_job_status/get_cold_start_progress` | Agent 9、Agent 10 |
| Agent 9 | `GovernanceService: /v1/review/dedup`、`/v1/governance/dashboard`、`/v1/metrics`、`/v1/metrics/dashboard` | Agent 6（上报）、Agent 10 |

**契约流转规则**：
1. 提供 Agent 完成 → 回报主 Agent → 主 Agent 通知依赖方"切换真实调用"
2. 切换前依赖方用占位函数（返回 mock，格式准确）开发自己的其余部分
3. 占位不得阻塞依赖方自己的其他 API 和域内测试
4. 最终由 Agent 10 验证全部契约（SubTask 10.2）

## 2. 主 Agent 协调流程规范

### 2.1 启动前检查清单（每启动一个子 Agent 前过一遍）

- [ ] 依赖的 Agent 是否已回报"全部 SubTask 完成"？（查本文件第 5 节协调卡片对应行）
- [ ] 依赖的占位 API 是否已就绪？（查第 1 节契约表，提供 Agent 行已完成）
- [ ] 方案文件路径是否正确？（`agents/agentN-名称.md` 绝对路径）
- [ ] 该 Agent 是否在正确波次？（依赖未完成不得提前启动）
- [ ] TodoWrite 是否已为该 Agent 建立跟踪项？

### 2.2 启动指令模板（派发子 Agent 时使用）

```
读取 {方案文件绝对路径} 并执行全部 SubTask。

[依赖就绪通知]（仅当该 Agent 有依赖时填写）
- Agent X 已完成，其提供的 API {名称} 已就绪，可切换真实调用。
- Agent Y 提供的 API {名称} 暂未就绪，请先用占位函数（返回 mock，格式见方案文件契约）开发其余部分。

[层级] 第1层子agent，可再启动1层子子agent（不得再向下）。

[规则摘要]
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析（文件:行数 — 问题）
- 代码注释用中文
- 每完成一个 SubTask 回报一次：SubTask 编号 + 完成状态 + 关键验证点结果
```

> **禁止**：将方案全文内联到任务描述（会导致主 Agent 上下文膨胀、子 Agent 缺少持久化参考）。只传路径 + 一句话摘要。

### 2.3 进度跟踪规则

- 主 Agent 用 TodoWrite 为每个 Agent 建一项，状态随回报更新（pending → in_progress → completed）
- **子 Agent 每完成一个 SubTask 回报一次**，主 Agent 不主动轮询、不 ping、不查文件 mtime
- 超时由编排器 timeout 触发，主 Agent 不手动计时催办
- 回报内容须含：SubTask 编号 + 完成状态 + 关键验证点结果

### 2.4 收到子 Agent 回报后的处理流程

1. **判断归属**：是我直接处理还是转发？（见第 8 节自检清单）
2. **核对预期**：回报的 SubTask 是否符合该 Agent 的里程碑顺序？（查第 5 节协调卡片"关键里程碑检查点"）
3. **检测异常**：回报内容是否有死循环/跑偏特征？（查第 6 节）
4. **更新跟踪**：TodoWrite 标记对应 SubTask/Agent 状态
5. **转发依赖**：若该 Agent 完成且他人依赖其 API → 通知依赖方切换真实调用
6. **推进波次**：若该 Agent 是本波最后一个完成的 → 检查第 7 节衔接规则启动下一波

### 2.5 完成判定标准（标记某 Agent 完成的三个条件，缺一不可）

1. 方案文件中该 Agent 的 **全部 SubTask 已勾选完成**
2. 该 Agent 的 **域内验证点全部通过**（回报中体现 PASS）
3. 该 Agent 提供的 **占位 API 契约已实现**（若它是提供方）且格式符合契约

> 三条件之一不满足 → 不得标记完成，不得启动依赖方。

## 3. 主 Agent 红线（不可违反）

1. **禁止直接修改业务代码** — 只协调、汇报、分发
2. **禁止深入阅读子 Agent 方案文件细节** — 只读本 overview.md 的协调卡片；需信息时通过子 Agent 询问
3. **禁止替子 Agent 做技术决策** — 不指定算法/库/数据结构
4. **收到用户报错时** — 识别错误所属领域 → 找到负责 Agent → 转发完整错误信息（不截断、不总结），不自行排查
5. **禁止 git 提交与推送** — 未经用户允许不得执行

## 4. 通用协作规则

### 4.1 子 Agent 汇报规范（必须含根因分析）

```
## 根因
[文件:行数 — 一句话说明问题]

## 修复
- [file_path] — [具体改动]

## 验证
- 构建: PASS/FAIL
- 测试: N PASS, M FAIL
```

没有根因说明的汇报视为不合格，主 Agent 应要求重报。

### 4.2 递归层数控制

- 主对话 → 子 Agent（第1层，可再启动1层子子 Agent）
- 子 Agent → 子子 Agent（第2层，不得再启动）
- 集成测试 Agent 启动的验证任务为第2层

### 4.3 三层测试体系

| 层级 | 负责方 | 内容 |
|------|--------|------|
| 第一层：域内测试 | 各 Agent 自己（最后一个 SubTask） | 后端 API 测试（正常+异常+权限边界）、数据测试 |
| 第二层：集成测试 | Agent 10 | 跨模块全链路联通、公共 API 契约验证、角色权限跨越 |
| 第三层：视觉验证 | Agent 10 | 治理看板截图对比、构建无 console error |

### 4.4 Bug 触发测试规则（用户报 bug 时主 Agent 必须双派发）

1. 派给功能 Agent 修复
2. 派给 Agent 10 添加对应回归测试用例（标注来源：用户报告 + 修复 Agent）

直到该测试通过，bug 才能从"待修复"列表移除。

## 5. 各 Agent 协调卡片

> 每张卡片含：预期产出物 / 里程碑检查点 / 正常推进信号 / 异常信号 / 死循环检测特征 / 复杂度与风险点。
> 主 Agent 据此判断该 Agent 是否按预期推进、何时介入。

---

### 卡片 1：Agent 1 infra-git（仓库与 Git Provider）

**预期产出物清单**：
- GitProvider 接口 + GitLab/Gitea/libgit2 三实现（fetch/show/diff/ls_tree）
- 分层仓库结构（项目级/模块级/子模块级递归）+ INDEX.md 规范
- 防孤岛 CI 校验脚本（资产存在但 INDEX.md 未登记 → 阻断）
- webhook 接收端点 `POST /v1/webhook/git`（secret 签名校验）
- Trae 适配（frontmatter 双区设计 + 会话路径自动探测）
- categories.yaml 受控词汇表 + PR 校验
- DREAMS.md 按月切分 + 归档压缩
- restricted 读权限（git-crypt 或独立仓库）
- shallow clone + 仓库 500MB 告警
- 域内测试

**关键里程碑检查点**：
- SubTask 1.1 GitProvider 接口 → 应先完成（Agent 4 recall/read、Agent 6 client 都依赖它）
- SubTask 1.3 防孤岛校验 → 完成后可验证仓库规范落地
- SubTask 1.4 webhook 签名校验 → 完成后 Agent 2 同步流程才有触发源
- SubTask 1.5 Trae 适配 → 是 Agent 6 client 依赖前置

**正常推进信号**：
- "SubTask 1.1 完成，GitProvider 三实现 fetch/show/diff/ls_tree 均通过"
- "SubTask 1.3 完成，防孤岛校验：未登记资产阻断合入验证通过"
- "SubTask 1.4 完成，webhook secret 签名校验拒绝未签名请求"

**异常信号（需介入）**：
- 反复报"Trae 会话路径探测失败"且未给出根因
- webhook 签名校验一直不通过但说不清原因
- 产出物里缺 GitProvider 三实现之一却声称完成

**死循环检测特征**：
- SubTask 1.5 Trae 适配重试 ≥ 3 次
- 会话路径探测跨 OS（Win/Mac/Linux）反复失败
- git-crypt restricted 读权限环境搭建反复失败

**复杂度与风险点**：
- 🔴 Trae 会话路径自动探测（`discover_sessions_root` 按 OS 查找，路径差异大）— 易踩坑
- 🔴 frontmatter 双区设计（coding 字段与 teamharness 字段分离，互不干扰）— 易污染既有字段
- 🟡 git-crypt restricted 读权限依赖环境，本地难复现
- 🟡 categories.yaml 两级 `<type>-<module>` 校验，`<module>` 须 INDEX.md 登记

---

### 卡片 2：Agent 2 infra-db（DB 派生索引层基础）★ 关键路径，最大依赖源

**预期产出物清单**：
- PG schema 全表（asset_index/agent_binding/module_stats/recall_log 分区/embedding_task_queue/index_sync_state/adoption_event/asset_recall_stats 物化视图）
- VectorStore Provider 抽象 + Qdrant/PGVector 两实现
- webhook 同步处理（读 INDEX.md 增量扫描 + embedding 计算）
- outbox 模式（asset_index + embedding_task_queue 同事务，异步 worker 写向量库）
- reconciliation cron（每 5 分钟补偿 webhook 丢失）
- 对账任务（每小时补偿 embedding_id IS NULL）
- embedding 模型双写过渡（新旧两表 + active_embedding_version）
- Alembic 迁移框架 + 初始 migration
- recall_log 按月分区 + 6 个月 TTL
- INDEX.md counts 服务端校验（不一致告警不阻断）
- 域内测试

**关键里程碑检查点**：
- SubTask 2.1 PG schema 全表 → 必须最先完成（2.3/2.4/2.5/2.6 全依赖表结构）
- SubTask 2.4 outbox 模式 → 完成是双存储原子性的关键验证点
- SubTask 2.5 reconciliation → 完成后 webhook 丢失才有补偿
- SubTask 2.7 embedding 双写 → 完成后召回融合 RRF 才能验证

**正常推进信号**：
- "SubTask 2.1 完成，schema 全表创建，asset_index 含 module_path/category/status"
- "SubTask 2.4 完成，outbox 同事务写入验证通过，worker 成功回写 embedding_id"
- "SubTask 2.5 完成，reconciliation 每 5 分钟运行，webhook 全丢失 5 分钟内补同步"

**异常信号（需介入）**：
- outbox 事务回滚后向量库出现孤儿数据（asset_index 回滚但 embedding 已写入）
- reconciliation 连续 3 周期滞后却未告警
- embedding 双写过渡期未融合两套向量结果（RRF 缺失）
- commit SHA 幂等去重失效（同一 commit 被处理多次）

**死循环检测特征**：
- SubTask 2.4 outbox 重试 ≥ 3 次
- embedding 双写过渡反复失败
- reconciliation 幂等性测试反复失败

**复杂度与风险点**：
- 🔴 **outbox 模式事务回滚后向量库孤儿数据**（asset_index 回滚但 worker 已写向量库 → 需补偿删除；这是缺陷 1.2 双存储原子性核心）
- 🔴 **embedding 模型双写过渡**（新旧两表 + RRF 融合 + active_embedding_version 控制 + 全量迁移后 drop 旧表；过渡期召回须融合两套）
- 🟡 reconciliation 幂等（commit SHA 去重）
- 🟡 recall_log 分区 + 6 个月 TTL + 物化视图
- ⚠️ **本 Agent 是最大依赖源（5 个 Agent 依赖），卡住会阻塞整条链路 → 优先关注**

---

### 卡片 3：Agent 3 deploy（部署与升级）

**预期产出物清单**：
- All-in-One 单二进制（内嵌 SQLite + PGVector + libgit2）
- docker-compose 一键部署（PG + Qdrant + 三服务 + Gitea）
- 单机模式每日 cron 备份（SQLite + git repo → tar.gz）
- API 语义化版本（/v1/ 锁定，/v2/ 破坏性）
- frontmatter schema_version 兼容解析
- 升级流程文档 + 迁移脚本框架
- 域内测试

**关键里程碑检查点**：
- SubTask 3.1 单二进制 → 先完成（内嵌三件套编译链接是基础）
- SubTask 3.2 docker-compose → 完成可验证多服务部署
- SubTask 3.5 schema_version 兼容 → 完成可验证旧版本可读

**正常推进信号**：
- "SubTask 3.1 完成，单二进制独立运行，5 人团队无需外部依赖"
- "SubTask 3.2 完成，docker-compose 一键部署脚本可用"

**异常信号（需介入）**：
- 单二进制无法独立运行（仍依赖外部 PG/Qdrant）
- docker-compose 启动失败却说不清是哪个服务
- schema_version 兼容解析丢字段

**死循环检测特征**：
- 单二进制打包/编译链接反复失败 ≥ 3 次
- docker-compose 启动反复失败

**复杂度与风险点**：
- 🔴 All-in-One 内嵌 SQLite + PGVector + libgit2 编译链接复杂（三件套静态链接）
- 🟡 frontmatter schema_version 兼容解析（旧版本可读）
- 🟡 升级流程文档 + 迁移脚本框架（边界场景多）

---

### 卡片 4：Agent 4 recall（召回服务）

**预期产出物清单**：
- `POST /v1/recall/list`（索引下钻 + 权限过滤 + 向量+BM25+RRF 精排）
- `POST /v1/recall/read`（从 git 读取，restricted 鉴权网关）
- `GET /v1/sync/status`
- consistency: eventual|strict 参数
- 响应体 as_of_commit / sync_lag_seconds / degraded 标记
- DB 故障降级（内存 LRU + 模块 BM25，强制 module_path，未传 503）
- 离线降级（本地 git working copy）
- 装配失效双重过滤（JOIN asset_index WHERE status='active'）
- recall/read 已删除资产 410 Gone + 替代建议
- OpenTelemetry trace_id 注入 + recall_log 写入
- 域内测试

**关键里程碑检查点**：
- SubTask 4.1 list 接口 → 先完成（核心召回能力）
- SubTask 4.6 DB 故障降级 → 完成可验证降级性能（2 秒内）
- SubTask 4.8 装配失效双重过滤 → 完成可验证失效窗口

**正常推进信号**：
- "SubTask 4.1 完成，recall/list 索引下钻 + 权限过滤 + RRF 精排通过"
- "SubTask 4.6 完成，DB 故障降级带 module_path 2 秒内返回 degraded:true"
- "SubTask 4.9 完成，已删除资产返回 410 Gone + 替代建议"

**异常信号（需介入）**：
- 降级模式每次 git show（未用 LRU 缓存，性能不达标）
- strict 模式未做 git fetch 实时读
- 装配失效过滤遗漏（返回已失效资产）
- 向量库不可达且未传 module_path 时未返回 503

**死循环检测特征**：
- SubTask 4.1 精排反复失败 ≥ 3 次
- 依赖 Agent 2 已回报完成，却反复报"等待 Agent 2"（依赖死锁误判）
- 降级性能测试反复不达标

**复杂度与风险点**：
- 🔴 DB 故障降级性能（带 module_path 2 秒内返回，须 LRU 缓存非每次 git show）
- 🟡 strict 模式 git fetch 实时读
- 🟡 装配失效双重过滤窗口（JOIN asset_index WHERE status='active'）
- 🟡 module_path 获取方式多分支（客户端推断/显式/LLM/回退）

---

### 卡片 5：Agent 5 binding（Agent 装配服务）

**预期产出物清单**：
- agent_binding 表 CRUD（fixed/on-demand）
- 调度索引表（task_routing + auto_bind）
- category 自动推断（LLM 推荐 3 候选，一键采纳）
- category 校验（两级 `<type>-<module>`，`<module>` 须 INDEX.md 登记）
- 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）
- 角色模板（builder/reviewer/scout 默认装配）
- 装配失效同事务级联更新（webhook 删除资产时 enabled=false）
- 装配更新写时复制（新版本新行，旧版本 10 分钟清理）
- tool PR Review 强制 CODEOWNERS + 签名验证
- API 鉴权（API Key 颁发/轮换，agent_id 反查）
- 域内测试

**关键里程碑检查点**：
- SubTask 5.1 agent_binding CRUD → 先完成
- SubTask 5.4 category 校验 → 完成可验证受控词汇表
- SubTask 5.7 级联更新 → 完成可验证无孤儿绑定
- SubTask 5.9 tool CODEOWNERS → 完成可验证 tool 安全

**正常推进信号**：
- "SubTask 5.7 完成，webhook 删除资产时同事务级联 enabled=false 验证通过"
- "SubTask 5.9 完成，tool PR 强制 CODEOWNERS + 签名验证通过"

**异常信号（需介入）**：
- 级联更新非同事务（出现孤儿绑定）
- 写时复制旧版本超 10 分钟未清理
- category 校验放行未在 INDEX.md 登记的 module

**死循环检测特征**：
- category LLM 推断反复失败 ≥ 3 次
- auto_bind 匹配反复不命中

**复杂度与风险点**：
- 🔴 装配失效同事务级联（webhook 删除资产时 enabled=false 须同事务，否则孤儿绑定）
- 🟡 写时复制旧版本 10 分钟清理（竞态：清理时正好有读）
- 🟡 tool CODEOWNERS + 签名验证（安全敏感）
- 🟡 API Key 颁发/轮换 + agent_id 反查

---

### 卡片 6：Agent 6 client（客户端）

**预期产出物清单**：
- 本地记忆文件夹读写（git working copy 管理）
- git 同步封装（sync: pull --rebase + push / pr / 冲突 diff 辅助）
- mapping.yaml 目录映射（两层适配模型）
- 召回客户端（调 /v1/recall/* API / 离线降级本地文件）
- module_path 上下文推断（从 coding 软件路径反查）
- CLI 6 命令（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）
- 守护进程（定时一级提炼调度 / 网络检测 / 离线召回代理）
- 私有资产隔离（.teamharness/private/ + .gitignore）
- manifest.json 本地缓存索引（从 INDEX.md + 资产派生）
- 采纳率上报（本地缓存 + 联网批量 flush）
- 域内测试

**关键里程碑检查点**：
- SubTask 6.1 文件夹读写 → 先完成
- SubTask 6.6 CLI 6 命令 → 完成可验证客户端入口齐全
- SubTask 6.7 守护进程 → 完成可验证一级提炼调度（Agent 7 依赖）

**正常推进信号**：
- "SubTask 6.6 完成，CLI 6 命令齐全（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）"
- "SubTask 6.7 完成，守护进程定时一级提炼调度可用"

**异常信号（需介入）**：
- 离线降级未实现（联网断开时召回全失败）
- 私有资产未加入 .gitignore（泄露风险）
- module_path 跨软件路径推断失败

**死循环检测特征**：
- mapping.yaml 路径反查反复失败 ≥ 3 次
- git 同步冲突处理反复失败

**复杂度与风险点**：
- 🔴 module_path 跨软件路径推断（不同 coding 软件路径差异大）
- 🔴 私有资产隔离（.teamharness/private/ + .gitignore，泄露风险）
- 🟡 离线降级一致性（联网/离线切换召回结果差异）
- 🟡 采纳率上报本地缓存 + 联网批量 flush（丢数据风险）

---

### 卡片 7：Agent 7 distill-personal（一级提炼）

**预期产出物清单**：
- SessionProvider 抽象（Trae 适配 + 通用 JSONL 兜底 + discover_sessions_root）
- 对话记录增量采集
- Light/REM/Deep 三阶段（信号筛选 → 意图归纳 → 五维评分 + frontmatter 资产）
- 四类资产子 Prompt 模板（rule/memory/skill/tool）
- LLM 服务端代理接入（POST /v1/llm/chat + GET /v1/llm/budget）
- 每成员 daily_token_budget + 超限降级（Deep 跳过，候选入 pending）
- Light 阶段候选信号计数上报
- LLM 强制 JSON schema + 校验失败重试
- cost estimate 命令
- 隐私保护（对话不离开本机）
- 域内测试

**关键里程碑检查点**：
- SubTask 7.1 SessionProvider → 先完成（依赖 Agent 6 守护进程）
- SubTask 7.3-7.5 三阶段 → 完成可验证提炼产出
- SubTask 7.8 budget 超限降级 → 完成可验证成本控制

**正常推进信号**：
- "SubTask 7.5 完成，Deep 阶段五维评分产出带 frontmatter 资产"
- "SubTask 7.8 完成，超预算时 Deep 跳过，候选入 .dreams/pending/，次日预算恢复处理"

**异常信号（需介入）**：
- 对话记录离开本机（隐私违规，须立即介入）
- 超预算未降级（成本失控）
- schema 校验失败未重试直接报错
- pending 候选次日未处理

**死循环检测特征**：
- JSON schema 校验失败重试 ≥ 3 次
- SessionProvider 路径探测反复失败
- 三阶段提炼反复无产出

**复杂度与风险点**：
- 🔴 **隐私保护（对话不离开本机，只上传结构化资产）** — 违反须立即介入
- 🔴 LLM 强制 JSON schema + 校验失败重试（易卡在重试循环）
- 🟡 budget 超限降级 + pending 处理（次日恢复逻辑）
- 🟡 Light 阶段信号计数上报（预算动态调整）

---

### 卡片 8：Agent 8 distill-team（二级提炼）

**预期产出物清单**：
- Light 增量聚类（只处理新增/修改资产）
- 全量聚类（每周日 cron）
- REM 跨成员模式识别
- Deep 六维评分 + 晋升门禁
- 冷启动旁路（资产 < 50 时门禁 ≥ 2，标记 confidence: low + cold_start: true）
- 种子 Prompt 库（prompts/seeds/）
- is_convention=true 单成员旁路
- 二级提炼 Prompt 模板（6 步推理链 + SKIP + 反例检验）
- LLM 强制 JSON schema + SKIP 审查区写 DREAMS.md
- 模型一致性测试集（20 条标准资产簇）
- 反向验证基线（冷启动用公开 Prompt 数据集）
- job 快照隔离（启动快照 commit SHA，完成增量 delta）
- distillation_job 表 trigger_source/cluster_fingerprint
- 采纳率降级（近 30 天 recall < 1 → 降级）
- DREAMS.md 审查界面数据接口
- 域内测试

**关键里程碑检查点**：
- SubTask 8.1 增量聚类 → 先完成（只处理新增/修改）
- SubTask 8.4 Deep 六维评分 + 门禁 → 完成可验证提炼质量
- SubTask 8.5 冷启动旁路 → 完成可验证冷启动期门禁动态化
- SubTask 8.12 job 快照隔离 → 完成可验证并发安全（Agent 9 依赖）

**正常推进信号**：
- "SubTask 8.12 完成，job 启动快照 commit SHA，完成后比对 HEAD 与快照，有新 commit 触发增量 job"
- "SubTask 8.5 完成，冷启动期（资产 < 50）门禁降为 ≥ 2，产出标记 cold_start: true"

**异常信号（需介入）**：
- job 未快照直接读 HEAD（并发提交读到不一致状态 → 竞态）
- 冷启动期未标记 confidence: low
- 增量聚类重复处理已处理资产（非增量）
- 近 30 天 recall < 1 未触发降级

**死循环检测特征**：
- SubTask 8.12 快照隔离反复失败 ≥ 3 次
- 增量聚类反复处理同一批资产（去重失效）
- LLM JSON schema 反复失败

**复杂度与风险点**：
- 🔴 **job 快照隔离（启动快照 commit SHA，完成后增量 delta；否则并发提交导致读到不一致状态 → 竞态）** — 这是缺陷 5.3 提炼 job 竞态核心
- 🔴 冷启动期门禁动态化（资产 < 50 时来源多样性 ≥ 2，正常期 ≥ 3）
- 🟡 增量聚类去重（cluster_fingerprint）
- 🟡 采纳率降级（近 30 天 recall < 1）
- 🟡 SKIP 审查区每周人工抽查 10%

---

### 卡片 9：Agent 9 governance（治理与可观测性）

**预期产出物清单**：
- PR Review 语义去重（≥0.92 相似度，LLM 判断归并 vs 独立）
- 语义归并（移入 archive/<date>/，6 个月后删除）
- 治理看板（模块资产数/拆分建议/未登记告警/召回命中率/采纳率）
- 拆分判定（基于 asset_index 实时查询，非人维护 counts）
- 指标采集（Prometheus + Grafana）
- 客户端 /v1/metrics 批量上报端点
- 指标文档化（10 个核心指标）
- 采纳率服务端可采（recall + read 次数，客户端辅助）
- adoption_rate stale 标记（连续 7 天无上报）
- 过期归档 + Owner 接管流程
- module_stats 从 asset_index 实时派生
- teamharness index reconcile 命令
- 域内测试

**关键里程碑检查点**：
- SubTask 9.5 指标采集 → 先完成（Prometheus + Grafana）
- SubTask 9.7 指标文档化 → 完成可验证 10 个核心指标
- SubTask 9.11 module_stats 实时派生 → 完成可验证不依赖人维护 counts

**正常推进信号**：
- "SubTask 9.11 完成，module_stats 从 asset_index 实时派生，teamharness index reconcile 自动重算"
- "SubTask 9.7 完成，10 个核心指标文档化定义齐全"

**异常信号（需介入）**：
- module_stats 依赖人维护 counts（违反缺陷修复 8.1）
- 采纳率仅靠客户端上报（违反缺陷修复 6.3，须服务端可采）
- 语义归并直接删除文件（应先移入 archive/<date>/，6 个月后才删）

**死循环检测特征**：
- 语义去重 LLM 判断反复失败 ≥ 3 次
- Grafana 看板配置反复失败

**复杂度与风险点**：
- 🔴 module_stats 实时派生（不得依赖人维护 counts，否则 counts 失准）— 缺陷 8.1
- 🔴 采纳率服务端可采信号（recall + read 次数，客户端上报仅辅助）— 缺陷 6.3
- 🟡 语义归并 6 个月删除（误删风险，须先 archive）
- 🟡 PR Review 语义去重 LLM 判断准确率

---

### 卡片 10：Agent 10 integration-test（集成测试）

**预期产出物清单**：
- 跨模块全链路联通测试（入库→同步→召回→提炼→发布）
- 公共 API 契约验证（全部占位 API 契约）
- 角色权限跨越测试（private/team/restricted/public + agent_binding）
- 治理看板视觉验证（截图对比 + 无 console error）
- 可行性缺陷 checklist 验证
- Bug 触发测试回归用例

**关键里程碑检查点**：
- SubTask 10.2 契约验证 → 先完成（全部占位 API 契约）
- SubTask 10.1 全链路 → 完成可验证端到端
- SubTask 10.5 缺陷 checklist → 完成可验证全部缺陷修复

**正常推进信号**：
- "SubTask 10.2 完成，全部占位 API 契约验证通过"
- "SubTask 10.1 完成，入库→webhook→DB→召回→一级→push→二级→发布全链路联通"

**异常信号（需介入）**：
- 跳过契约验证直接做全链路
- 契约验证失败但未回报给对应 Agent（须双派发：定位 Agent + 修复）
- 视觉验证有 console error 却声称通过

**死循环检测特征**：
- 全链路反复失败但未定位到具体 Agent（应逐段定位）
- 契约验证反复失败同一项

**复杂度与风险点**：
- 🔴 契约验证依赖全部 Agent 完成（启动前提：第四波）
- 🔴 Bug 触发测试须主 Agent 双派发（修复 Agent + Agent 10 回归用例）
- 🟡 全链路逐段定位（失败时须定位到具体 Agent，不能笼统报"集成失败"）
- 🟡 视觉验证截图对比基准维护

---

## 6. 死循环检测规则（可操作阈值）

主 Agent 收到回报时按下表判定，命中任一即介入。

| 检测维度 | 阈值/特征 | 主 Agent 动作 |
|---------|----------|--------------|
| **重试计数** | 同一 SubTask 失败重试 ≥ 3 次 | 介入：要求根因分析，评估是否重启 |
| **时间阈值** | 单 SubTask 超过预期时长 2 倍（基础设施类 30min、服务类 45min、提炼类 60min、集成测试 90min） | 询问进度，要求回报当前状态 |
| **循环特征** | 回报内容重复出现相同错误信息 / 相同文件路径 | 判定死循环，启动纠错流程（第 8 节） |
| **依赖死锁** | A 等 B、B 等 A（检查依赖图是否有环） | 立即介入：依赖图无环，必有一方误判就绪状态；要求双方核对占位 API 是否真的就绪 |
| **无进展判定** | 连续 2 次回报无新 SubTask 完成 | 介入：要求说明卡点 + 根因 |
| **产出物不符** | 回报"完成"但产出物清单不齐 / API 契约格式不符 | 介入：要求补齐，不得标记完成 |
| **跨波次阻塞** | 本波已完成但下一波启动后某 Agent 反复报依赖未就绪 | 介入：核对占位 API 是否真的就绪（可能依赖方未切换真实调用） |

**依赖图环检查**：本方案依赖图为 DAG（无环）：
```
1 → 6 → 7
2 → 4 → 8 → 9
2 → 5
2 → 7
2 → 8
4 → 8
8 → 9
全部 → 10
```
若子 Agent 报"等 Agent X"但 Agent X 已回报完成 → 必有一方误判（占位 API 未真就绪 / 未通知切换）。

## 7. 波次间衔接规则

### 7.1 第一波 → 第二波启动前检查清单
- [ ] Agent 1 / 2 / 3 全部回报"全部 SubTask 完成 + 域内验证 PASS + 占位 API 已实现"
- [ ] Agent 2 的 AssetIndex / EmbeddingService / SyncService 契约就绪（Agent 4/5 依赖）
- [ ] Agent 1 的 GitProvider 契约就绪（Agent 4 recall/read、Agent 6 client 依赖）
- [ ] Agent 3 的 DeployConfig 契约就绪（无第二波依赖，但记录）
- [ ] TodoWrite 第一波三项标记 completed
→ 启动第二波：Agent 4、5、6 并行

### 7.2 第二波 → 第三波启动前检查清单
- [ ] Agent 4 / 5 / 6 全部回报完成
- [ ] Agent 4 的 RecallService 契约就绪（Agent 6 召回客户端、Agent 8 recall_log 统计依赖）
- [ ] Agent 5 的 BindingService 契约就绪（Agent 6 category-suggest/鉴权依赖）
- [ ] Agent 6 的 ClientCLI / ClientDaemon 契约就绪（Agent 7 守护进程调度依赖）
- [ ] Agent 2 的 AssetIndex 仍就绪（Agent 7/8 写入资产依赖）
→ 启动第三波：Agent 7、8 并行（Agent 9 待 Agent 8 完成）

### 7.3 第三波内部衔接
- Agent 7、8 可并行启动
- **Agent 9 须等 Agent 8 完成**（依赖 distillation_job 表 + TeamDistill.get_job_status）
- Agent 8 回报完成 → 检查清单 → 启动 Agent 9

### 7.4 第三波 → 第四波启动前检查清单
- [ ] Agent 7 / 8 / 9 全部回报完成
- [ ] Agent 7 的 PersonalDistill / LLMProvider 契约就绪
- [ ] Agent 8 的 TeamDistill 契约就绪
- [ ] Agent 9 的 GovernanceService 契约就绪
- [ ] 第一/二/三波全部 Agent 完成且占位 API 全部实现
→ 启动第四波：Agent 10 集成测试

### 7.5 第四波完成判定
- Agent 10 回报：全链路联通 + 契约验证 + 权限跨越 + 视觉验证 + 缺陷 checklist + Bug 回归全部通过
→ 全部完成，向用户汇报

### 7.6 经验管理注入规则

主 Agent 启动子 Agent 时，须在 prompt 的 `[规则摘要]` 段之后**直接粘贴**以下经验管理规则，确保子 Agent 即使是纯净上下文也能执行经验提炼与固化：

```
[经验管理规则]
- 修复错误后必须提炼经验，写入两个位置：
  1. 域内经验 → 写入本方案文件末尾「## 领域经验」段
  2. 通用规则 → 写入 .trae/rules/gotchas.md（按主题分类追加，禁止覆盖已有）
- 经验格式：标题 + 反例 + 正例 + 适用范围 + `[来源: Agent N / SubTask X.Y / 第Z波]`
- 不能提炼的回报视为不合格
- 详细规则见：.trae/specs/feasibility-gap-analysis/经验管理规则.md（项目本地的注入模板）
- 完整规范见 skill：`collab-experience-mgmt`（主 Agent 调用此 skill 获取 L1 聚合流程/Bug 双派发/gotchas 写入规则）
```

**注入原则**：
- **粘贴不引用**：不要写"请阅读经验管理规则.md"，直接把规则文本粘进去。子 Agent 可能在纯净上下文中启动，缺少项目目录导航能力。
- **精简不冗长**：只注入子 Agent 需要的核心规则（三层体系/格式/固化位置），不注入方法论原理。

**本波次结束后的经验聚合**：
- 每波次全部子 Agent 回报完成后，主 Agent 执行 L1 多源聚合（详见 `经验管理规则.md` §五）
- 聚合产物写入 `.trae/rules/gotchas.md`（追加不覆盖）
- 遗漏「## 领域经验」段的 Agent 须补登记追溯链

## 8. 纠错引导流程

当第 6 节检测到异常 / 用户报错时，主 Agent 按以下步骤处理：

### 8.1 识别错误领域
- 根据错误内容（文件路径 / API 名称 / SubTask 编号）判断属于哪个 Agent
- 查第 5 节协调卡片确认归属
- 模糊时按错误信息中的关键词匹配 Agent 职责（如"embedding"→Agent 2，"recall"→Agent 4，"distill"→Agent 7/8）

### 8.2 转发完整错误（不截断、不总结）
- 原样转发给负责 Agent，**禁止自行总结/截断**
- 附加：错误发生场景、相关 SubTask 编号、依赖状态

### 8.3 要求根因分析
要求子 Agent 按"根因 / 修复 / 验证"格式回报：
```
## 根因
[文件:行数 — 一句话说明问题]

## 修复
- [file_path] — [具体改动]

## 验证
- 构建: PASS/FAIL
- 测试: N PASS, M FAIL
```
没有根因的回报视为不合格，要求重报。

### 8.4 评估修复方案
- 方案合理（针对根因，不绕过问题）→ 允许执行
- 方案跑偏（只治标不治本 / 改测试让它通过）→ 引导修正：指出根因方向，要求重新方案
- 方案影响其他 Agent（改了公共 API 契约）→ 评估影响范围，通知受影响 Agent

### 8.5 验证修复
- 子 Agent 回报修复后，确认对应验证点通过（查协调卡片"关键里程碑检查点"）
- 若是公共 API 变更 → 通知依赖方重新验证
- 若是用户报 bug → 同步派 Agent 10 添加回归用例（第 4.4 节）

## 9. 任务重启策略

### 9.1 重启触发条件（满足任一即重启）
- 同一问题纠错 ≥ 2 次未解决
- 子 Agent 明显跑偏且无法引导（产出物与预期严重不符）
- 上下文爆满（子 Agent 回报"上下文不足/超出"）
- 死循环无法打破（重试 ≥ 3 次 + 纠错 ≥ 2 次仍循环）

### 9.2 重启上下文传递（新子 Agent 必须知道）
1. **已完成的 SubTask**（不重做）：列出已勾选的 SubTask 编号 + 验证点 PASS 状态
2. **失败的 SubTask 及失败原因**：附上次根因分析（如有）+ 最后一次错误信息原文
3. **已产出但可能有问题的文件**（需审查）：列出文件路径，标注"需确认可用"
4. **依赖状态**：哪些占位 API 已就绪 / 未就绪（查第 1 节契约表 + 已回报完成的 Agent）

### 9.3 重启指令模板
```
读取 {方案文件绝对路径} 执行 SubTask。

[重启上下文]
- 本任务为重启，上一个子 Agent 因 {原因} 已终止。
- 已完成 SubTask（勿重做）：{X.Y, X.Z}，验证点已 PASS。
- 失败 SubTask（从此继续）：{X.N}，失败原因：{根因/错误原文}。
- 已产出待审查文件：{path1, path2}，请先确认可用再继续。
- 依赖就绪状态：Agent {A} 的 {API} 已就绪；Agent {B} 的 {API} 未就绪，用占位。

[层级] 第1层子agent，可再启动1层子子agent。

[规则摘要]
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
- 每完成一个 SubTask 回报一次

[重启后首要动作] 先确认已产出物可用（构建+测试），再继续未完成 SubTask {X.N}。
```

### 9.4 重启后验证
- 新子 Agent 须先确认已产出物可用（构建 PASS + 已有测试 PASS），再继续未完成 SubTask
- 若已产出物有问题 → 新子 Agent 须回报，由主 Agent 决定是否回退已"完成"的 SubTask
- 重启后第 1 次回报若仍是同一错误 → 视为重启失败，上报用户

## 10. 主 Agent 自检清单（每次收到子 Agent 回报时自问）

- [ ] 这是我该处理的（进度汇总/转述用户）还是转发给子 Agent（技术问题）？
- [ ] 子 Agent 的进展是否符合预期里程碑顺序？（查协调卡片"关键里程碑检查点"）
- [ ] 回报的 SubTask 是否在方案文件 SubTask 清单内？（跑偏检测）
- [ ] 有没有死循环特征？（查第 6 节阈值表）
- [ ] 依赖关系是否正确？（该 Agent 报"等 X"时，X 是否真未完成？X 完成时该 Agent 是否被通知切换真实调用？）
- [ ] 产出物是否齐全？（查协调卡片"预期产出物清单"）
- [ ] 回报是否含经验提炼？（「根因/修复/经验提炼/验证」四件套，缺一视为不合格）
- [ ] 经验提炼是否含正反例代码？（反例/正例必须是代码片段，不能是自然语言描述）
- [ ] 是否需要更新 TodoWrite？
- [ ] 该 Agent 完成后，谁是依赖方？是否需要通知切换真实调用？
- [ ] 该 Agent 完成后，本波是否全部完成？是否触发下一波？
- [ ] 本波是否全部完成？→ 是则执行 L1 经验聚合（见 `经验管理规则.md` §五）

## 11. 全局参考

- 技术方案：`d:\Code\TeamHarness\TECH_PROPOSAL.md`
- 经验管理规则（项目文档）：`.trae/specs/feasibility-gap-analysis/经验管理规则.md`（注入模板）
- 经验管理规则（skill）：`collab-experience-mgmt`（主 Agent 调用，完整 L1 聚合/Bug 双派发/gotchas 写入规范）
- 通用规则持久化：`.trae/rules/gotchas.md`（TRAE 自动加载，按主题分类）
- 原 spec（含完整架构说明，可选读）：`d:\Code\TeamHarness\.trae\specs\feasibility-gap-analysis\spec.md`
- 各 Agent 方案文件（主 Agent 不深入阅读，子 Agent 卡住时按协调卡片引用的 SubTask 查询）：`agents\agentN-名称.md`
- 协调规范来源：`collab-multi-agent` skill（主 Agent 红线 / 子 Agent 汇报规范 / 递归层数 / 占位 API 契约 / 三层测试）
