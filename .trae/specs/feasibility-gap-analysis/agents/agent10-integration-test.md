# Agent 10: integration-test（集成测试）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
全部 Agent

## 职责
- 跨模块全链路联通测试
- 公共 API 契约验证
- 角色权限跨越测试
- 三层测试体系执行（域内测试汇总 + 集成测试 + 视觉验证）
- Bug 触发测试（用户报告的 bug 添加回归用例）
- 可行性缺陷 checklist 验证

## 占位 API 契约

### 本 Agent 提供的 API
无（集成测试不对外提供 API，只消费其他 Agent 的 API）

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
集成测试需要验证以下全部占位 API 契约：
- Agent 1 提供：
  ```
  GitProvider: fetch(repo) / show(sha, path) / diff(sha_a, sha_b) / ls_tree(sha, path)
  WebhookReceiver: POST /v1/webhook/git
  ```
- Agent 2 提供：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  EmbeddingService: embed(text) / embed_batch(texts) / get_active_version()
  SyncService: trigger_sync(commit_sha) / get_sync_status() / reconcile()
  ```
- Agent 3 提供：
  ```
  DeployConfig: get_mode() / get_storage_backend() / get_version()
  ```
- Agent 4 提供：
  ```
  RecallService:
    POST /v1/recall/list
    POST /v1/recall/read
    GET /v1/sync/status
  ```
- Agent 5 提供：
  ```
  BindingService:
    POST /v1/binding/create
    POST /v1/binding/auto
    GET /v1/binding/list
    POST /v1/category/suggest
    POST /v1/auth/apikey
  ```
- Agent 6 提供：
  ```
  ClientCLI: teamharness sync / pr / recall / category-suggest / cost-estimate / index-reconcile
  ClientDaemon: 定时一级提炼调度 / 网络状态检测 / 采纳率批量上报
  ```
- Agent 7 提供：
  ```
  PersonalDistill:
    run_light / run_rem / run_deep / report_metrics
  LLMProvider:
    POST /v1/llm/chat
    GET /v1/llm/budget
  ```
- Agent 8 提供：
  ```
  TeamDistill:
    trigger_incremental / trigger_full / get_job_status / get_cold_start_progress
  ```
- Agent 9 提供：
  ```
  GovernanceService:
    POST /v1/review/dedup
    GET /v1/governance/dashboard
    POST /v1/metrics
    GET /v1/metrics/dashboard
  ```

## SubTask 列表
- [ ] Task 10: 集成测试 + 契约验证
  - [ ] SubTask 10.1: 跨模块全链路联通测试（入库→同步→召回→提炼→发布）
  - [ ] SubTask 10.2: 公共 API 契约验证（全部占位 API 契约）
  - [ ] SubTask 10.3: 角色权限跨越测试（private/team/restricted/public + agent_binding）
  - [ ] SubTask 10.4: 治理看板视觉验证（截图对比 + 无 console error）
  - [ ] SubTask 10.5: 可行性缺陷 checklist 验证（checklist.md 全部检查点）
  - [ ] SubTask 10.6: Bug 触发测试回归用例（用户报告的 bug）

## 域内验证点
- [ ] 跨模块全链路：入库 → webhook 同步 → DB 索引 → 召回 → 一级提炼 → push → 二级提炼 → 发布 Prompt
- [ ] 全部占位 API 契约验证通过（GitProvider/AssetIndex/EmbeddingService/SyncService/RecallService/BindingService/PersonalDistill/TeamDistill/GovernanceService/DeployConfig）
- [ ] 角色权限跨越：private 资产不进 DB 索引，team/restricted/public 按 agent_binding 过滤
- [ ] 治理看板视觉验证：截图对比 + 无 console error
- [ ] 可行性缺陷 checklist 全部检查点通过
- [ ] 用户报告 bug 的回归测试用例通过

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文

## 领域经验

> Agent 10 集成测试过程中发现的域内经验（仅适用于本 Agent / 本项目）。
> 通用经验已固化到 `.trae/rules/gotchas.md`，此处仅记录域内实现细节发现。

### Agent 9 契约缺失（需主 Agent 双派发修复）

**问题**：Agent 9 的 governance router（`server/governance/metrics.py:358 governance_router`）只注册了 `/v1/metrics*` 系列路由，缺失两个 spec 要求的 HTTP 路由：
- `POST /v1/review/dedup` — PR Review 语义去重（服务类 `PRReviewDedupService` 已实现，缺 HTTP 路由注册）
- `GET /v1/governance/dashboard` — 治理看板（服务类 `DashboardService.get_dashboard/get_overview` 已实现，缺 HTTP 路由注册）

**处理**：在 `test_api_contracts.py` 和 `test_bug_regression.py` 中用 `@pytest.mark.xfail(strict=True)` 标记，已上报主 Agent 双派发（派 Agent 9 补注册路由 + Agent 10 保留 xfail 回归用例，修复后移除 xfail）。

**追溯**：`[来源: Agent 10 / SubTask 10.2 + 10.6 / 第四波]`

### 实现名称与 spec 文档不一致

集成测试发现多处实现名称与 spec 文档的假设不一致（非 bug，是实现细节）：
- `BudgetManager` 在 `server.distill_personal.budget` 模块（非 `llm_provider` 模块）；`llm_provider` 模块只有 `LLMBudget` 数据类（含 consume/reset/exhausted 预算管理逻辑）
- `EmbeddingMigrationService`（spec 名）实际类名是 `EmbeddingMigration`（`server/infra_db/embedding_migration.py:73`）
- `COLD_START_CONFIDENCE` 是 `ColdStartBypass` 的类属性（`server/distill_team/cold_start.py:31`），非模块级常量，须用 `ColdStartBypass.COLD_START_CONFIDENCE` 访问
- `StorageBackend` 是 `@dataclass(frozen=True)`（`server/deploy/config.py:61`），非 Enum 不可迭代；`StorageKind` 才是 `str, Enum`（`config.py:41`），含 SQLITE/POSTGRES/SQLITE_VEC/PGVECTOR/QDRANT/LIBGIT2/GITLAB/GITEA

**追溯**：`[来源: Agent 10 / SubTask 10.5 / 第四波]`

### conftest fixture mock 数据格式不匹配

**问题**：`tests/integration/conftest.py` 的 `category_suggest_service` fixture 注入的 mock LLM 返回 `{"content": '[{"category":"rule-backend",...}]'}`（JSON 数组字符串），但 `call_llm_for_category_suggestions`（`server/binding/llm.py:86`）期望 `{"candidates":[...]}` 格式（JSON 对象含 candidates 键）。`_parse_llm_json` 解析成功后 `data.get("candidates")` 返回 None，触发 fallback。

**影响**：直接用 `category_suggest_service` fixture 测试"注入 LLM 后不走 fallback"会误判失败（实际是 mock 数据格式问题，非功能 bug）。

**规避**：测试 LLMProvider 注入切换时，直接调用 `call_llm_for_category_suggestions` + 自行构造格式正确的 mock LLM，不依赖 conftest fixture。

**追溯**：`[来源: Agent 10 / SubTask 10.6 / 第四波]`

### 遗留待办验证结果

- **遗留待办 1（Agent 6 在线召回/category-suggest 切换真实调用）**：✅ 已切换。`RecallClient` 含 `_call_remote_list` / `_call_remote_read` 真实 HTTP 方法 + httpx 使用 + HTTP 失败降级路径（`test_bug_regression.py` 3 PASS）
- **遗留待办 2（Agent 5 LLMProvider 切换）**：✅ 已就绪。`LLMChatProtocol` 协议可注入，注入后 `call_llm_for_category_suggestions` 走真实路径（`used_fallback=False`），`llm=None` 时退化启发式（4 PASS）
- **遗留待办 3（Agent 3 docker-compose / PyInstaller 实际未跑通）**：⚠️ 集成测试环境无法完整验证（需实际 docker build / PyInstaller 打包）。已验证 `DeployConfig` / `DeployMode` / `StorageKind` 配置完整性 + All-in-One 默认后端含 sqlite + libgit2（`test_gap_7_1` PASS）。实际跑通须在 CI/部署环境验证
- **遗留待办 4（config.py int 容错 bug）**：✅ 已修复。`_coerce_int` 函数存在且包裹所有 int 字段，env=not-a-number 时不抛 ValueError（6 PASS）

**追溯**：`[来源: Agent 10 / SubTask 10.6 / 第四波]`
