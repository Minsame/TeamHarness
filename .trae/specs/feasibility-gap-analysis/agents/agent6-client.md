# Agent 6: client（客户端）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 1（GitProvider/Trae 适配）

## 职责
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

## 占位 API 契约

### 本 Agent 提供的 API
```
ClientCLI:
  teamharness sync / pr / recall / category-suggest / cost-estimate / index-reconcile
ClientDaemon:
  定时一级提炼调度 / 网络状态检测 / 采纳率批量上报
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 1 提供：
  ```
  GitProvider: fetch(repo) / show(sha, path) / diff(sha_a, sha_b) / ls_tree(sha, path)
  ```
- Agent 4 提供（召回客户端调用）：
  ```
  RecallService:
    POST /v1/recall/list (agent_id, query?, module_path?, consistency?)
      → [{asset_id, type, title, tags, relevance_score, git_path, module_path}], as_of_commit, sync_lag_seconds, degraded
    POST /v1/recall/read (agent_id, asset_id)
      → {content, frontmatter} | 410 Gone
    GET /v1/sync/status
      → {last_synced_commit, lag_seconds, sync_source}
  ```
- Agent 5 提供（category-suggest / 鉴权）：
  ```
  BindingService:
    POST /v1/category/suggest (content, module_path) → [3 candidates]
    POST /v1/auth/apikey (member_id) → {api_key, agent_id}
  ```
- Agent 9 提供（指标批量上报）：
  ```
  GovernanceService:
    POST /v1/metrics (batch) → ack (客户端上报)
  ```

## SubTask 列表
- [ ] Task 6: 客户端
  - [ ] SubTask 6.1: 本地记忆文件夹读写（git working copy 管理）
  - [ ] SubTask 6.2: git 同步封装（sync: pull --rebase + push / pr / 冲突 diff 辅助）
  - [ ] SubTask 6.3: mapping.yaml 目录映射（两层适配模型）
  - [ ] SubTask 6.4: 召回客户端（调用 /v1/recall/* / 离线降级本地文件）
  - [ ] SubTask 6.5: module_path 上下文推断（从 coding 软件路径反查 mapping.yaml）
  - [ ] SubTask 6.6: CLI 命令（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）
  - [ ] SubTask 6.7: 守护进程（定时一级提炼调度 / 网络检测 / 采纳率批量上报）
  - [ ] SubTask 6.8: 私有资产隔离（.teamharness/private/ + .gitignore）
  - [ ] SubTask 6.9: manifest.json 本地缓存索引（从 INDEX.md + 资产派生）
  - [ ] SubTask 6.10: 采纳率上报（本地缓存 + 联网时批量 flush）
  - [ ] SubTask 6.11: 域内测试（同步流程 + 召回客户端 + 离线降级 + 配置管理）

## 域内验证点
- [ ] 本地记忆文件夹读写（git working copy 管理）
- [ ] teamharness sync（pull --rebase + push）可用
- [ ] teamharness pr 发起 PR 可用
- [ ] 冲突 diff 辅助视图可用
- [ ] mapping.yaml 目录映射（两层适配）正确
- [ ] 召回客户端有网时调 /v1/recall/* API
- [ ] 召回客户端离线时降级本地文件检索
- [ ] module_path 从 coding 软件路径反查 mapping.yaml 正确
- [ ] CLI 命令齐全（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）
- [ ] 守护进程定时一级提炼调度可用
- [ ] 守护进程网络检测 + 离线召回代理可用
- [ ] 私有资产隔离（.teamharness/private/ + .gitignore）
- [ ] manifest.json 从 INDEX.md + 资产派生正确
- [ ] 采纳率上报本地缓存 + 联网时批量 flush

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文

## 领域经验
- 离线召回不应仅依赖 INDEX.md 派生的 manifest，须补充 `WorkingCopy.list_assets()` 扫描（按 path 去重）。防孤岛校验是独立职责（由 `index-reconcile` 命令承担），不应阻塞离线召回。与私有资产绕过 INDEX.md 直接扫描 `.teamharness/private/` 的设计一致。
`[来源: Agent 6 / SubTask 6.4 / 第二波 / L1 回测通过]`
