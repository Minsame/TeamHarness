# TeamHarness 工程踩坑规则

> 本文件由 TRAE 自动加载（`.trae/rules/`），适用于本项目所有后续会话。
> 本文件只保留 TeamHarness 项目特定的规则。技术栈通用规则已迁移到 ~/.trae-cn/rules/ 下对应技术栈文件。
> 来源：多 Agent 协作经验提炼（L2 子 Agent 上传 → L1 主 Agent 多源聚合 + 回测验证）。
> 追溯链格式：`[来源类型: 新提炼|导入 | 来源: ...]`

## API 设计

### 测试注入路径与生产注入路径一致性
Service 依赖注入若存在"测试用闭包/工厂注入 + 生产用模块级全局变量 configure_xxx()"双轨制，测试全绿但生产端点返回 503——测试走 `build_router(svc)` 旁路绕过了生产路径的 `configure_xxx()` 注入，且生产 wrapper（如 `_configure_xxx_services`）函数体可能为空（标注"暂跳过"而不调用对应 configure_xxx）。双轨制的存在本身即是技术债。
- **判定规则**：每个 Service 必须有且仅有一条注入路径。若生产用 `configure_xxx(svc)`，测试也必须调用同一函数（而非 `build_router(svc)` 旁路）；若测试需要闭包注入，生产也必须用同一工厂。CI 检查：生产 wrapper `_configure_xxx_services` 函数体不能只含 `logger.info("跳过")` 而不调用对应的 `configure_xxx`
- **反例**：`_configure_recall_services(db)` 函数体只有 `logger.info("recall 服务注入跳过")`，从不调用 `configure_recall()`；测试用 `build_router(recall_service)` 闭包注入 → 测试全绿，生产端点 503。`configure_recall` 函数定义存在但全仓库零调用，成为死代码
- **正例**：测试调用 `configure_recall(real_service)` 注入模块级变量，与生产路径完全一致；或生产也改用 `build_router(svc)` 工厂模式
- 适用范围：所有"Service 注入 + 测试"场景，尤其是存在 `build_router(svc)` 与 `configure_xxx()` 两种注入入口，或生产 wrapper 与 configure 函数分离时
`[来源类型: 新提炼 | 来源: 依赖分析 / server/app.py + recall/api.py / 2026-08-10]`

## 后端资产访问控制

### 资产归属验证（owner 与 API Key 一致性）
`list_assets` 端点若仅凭 API Key 认证但不校验查询参数中的 owner 与该 Key 对应的 member 是否一致，用户 A 可用自身 Key 传入 `owner=user_b` 检索他人资产——权限隔离在数据层失效，UI 层的权限控制可被 API 直接绕过。
- **判定规则**：所有按 owner 过滤资产的查询端点，必须从认证上下文（API Key → member）推导 owner，禁止信任客户端传入的 owner 参数；或显式校验 `request.owner == authenticated_member.id`，不匹配返回 403
- **反例**：`list_assets(owner=request.query_params["owner"])` 直接用客户端传入的 owner 查询，未与 API Key 对应的 member 校验
- **正例**：`owner = authenticated_member.id; list_assets(owner=owner)` 从认证上下文推导，忽略客户端传入
- 适用范围：所有按 owner 维度的资产查询 / 操作端点
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

### ACL 入参格式校验（grantee_id 格式验证）
创建 ACL 授权时若不校验 `grantee_id` 格式（如非 string、超长、含特殊字符），直接传入 ORM 查询可能触发数据库异常返回 500，暴露内部错误信息且无法区分"用户不存在"与"系统错误"。
- **判定规则**：所有接受 `grantee_id` / `member_id` 的端点，必须校验：① 类型为 string；② 长度在合理范围（如 ≤ 64 字符）；③ 格式符合 ID 规范（如 UUID 或预定义字符集）。校验失败返回 4xx，不进入 ORM 查询层
- **反例**：`create_acl(grantee_id=request.body["grantee_id"])` 无校验，传入 `grantee_id=None` 或超长字符串 → 500
- **正例**：入口校验 `if not isinstance(grantee_id, str) or len(grantee_id) > 64: return 400`，通过后才查询
- 适用范围：所有接受用户 ID / 实体 ID 作为入参的后端端点
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

### 资产可见性与关系管理（ACL + 图结构）
restricted 可见性的资产若仅靠 owner 字段过滤，无法表达"特定用户可访问"的细粒度授权；资产间的关联关系（依赖、版本、派生）若用 JSON 字段或外键列表存储，无法高效查询多跳关系。
- **判定规则**：① restricted 资产必须通过显式 ACL 列表管理可见性（owner + ACL 授权列表双校验），不能用简单的 owner 字段过滤；② 资产间关系必须用 `asset_links` 表的图结构管理（source_id + target_id + link_type），支持多跳关系查询，禁止用 JSON 字段存储关系列表
- **反例**：restricted 资产仅检查 `asset.owner == requester`，无 ACL 授权机制；资产关系用 `asset.related_ids = "[1,2,3]"` JSON 字段存储
- **正例**：`acl = get_acl_entries(asset_id, grantee_id=requester); if not acl and asset.owner != requester: return 403`；关系用 `INSERT INTO asset_links (source_id, target_id, link_type) VALUES (...)`
- 适用范围：所有需要细粒度访问控制 / 资产关系管理的后端模块
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

## 前端安全与健壮性

### HTTP 请求层安全（401 拦截 + 超时控制）
前端 `request()` 封装若不拦截 401 响应，API Key 失效后用户仍能操作界面直到下次请求失败；若不设置超时，后端无响应时前端无限等待，用户体验卡死。
- **判定规则**：前端统一的 `request()` 封装必须：① 包含 401 拦截器，收到 401 时自动清除本地凭证并跳转登录页；② 包含 AbortController 设置超时（默认 15 秒），超时后中断请求并提示用户
- **反例**：`fetch(url)` 无 401 拦截，API Key 失效后用户看到空白页面；无超时控制，后端挂起时前端永远 loading
- **正例**：`fetch(url, { signal: AbortSignal.timeout(15000) }).then(res => { if (res.status === 401) { clearCredentials(); router.push('/login') } })`
- 适用范围：所有前端 HTTP 请求封装层
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

### 表单提交重入控制（handleUpdateScope 重入守卫）
scope 更新等异步操作若无重入守卫，用户快速点击按钮会触发多次并发请求，导致状态不一致或数据错乱（如 scope 被重复展开 / 收起）。
- **判定规则**：所有触发异步操作的交互函数（如 `handleUpdateScope`、`handleShare`、`handleDelete`）必须包含重入守卫：函数入口设置 `is_processing = true`，finally 块重置；或按钮 disabled 绑定到 processing 状态
- **反例**：`async function handleUpdateScope() { await api.updateScope(...); refresh() }` 无守卫，快速点击触发多次请求
- **正例**：`async function handleUpdateScope() { if (isUpdating.value) return; isUpdating.value = true; try { await api.updateScope(...) } finally { isUpdating.value = false } }`
- 适用范围：所有触发异步操作的前端交互函数
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

### Vue 生命周期异步声明（onMounted + await）
Vue 3 的 `onMounted(callback)` 若 callback 使用 `await` 但未声明 `async`，`await` 后的代码不会执行（Promise 被静默吞掉），表现为初始化逻辑部分丢失——如异步校验 API Key 有效性时，校验通过后的页面跳转 / 状态设置不执行。
- **判定规则**：所有 `onMounted` / `onActivated` 等生命周期钩子的 callback，若内部使用 `await`，必须声明为 `async`：`onMounted(async () => { await checkLogin(); ... })`
- **反例**：`onMounted(() => { const valid = await checkLogin(); if (!valid) router.push('/login') })` → `await` 后的跳转不执行
- **正例**：`onMounted(async () => { const valid = await checkLogin(); if (!valid) router.push('/login') })`
- 适用范围：所有 Vue 3 生命周期钩子中使用 await 的场景
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints + Lessons Learned / 多账号测试 / 2026-08-12]`

### 登录态异步校验与浏览器导航集成
`checkLogin` 若仅检查本地 token 存在性而不异步验证 API Key 有效性，token 失效后用户仍能访问受保护页面；若不集成 history API，浏览器前进/后退按钮无法正确导航。
- **判定规则**：① `checkLogin` 必须异步调用后端验证 API Key 有效性，不能仅检查本地 token 存在；② 前端必须集成 history API（`popstate` 事件监听），确保浏览器前进/后退按钮触发正确的路由导航
- **反例**：`function checkLogin() { return !!localStorage.getItem('api_key') }` 仅检查本地存在性；无 `popstate` 事件监听，浏览器后退按钮不触发 Vue Router 导航
- **正例**：`async function checkLogin() { const key = localStorage.getItem('api_key'); if (!key) return false; const res = await api.validate(key); return res.valid }`；`window.addEventListener('popstate', () => router.handleRouteChange())`
- 适用范围：所有需要登录态校验和浏览器导航集成的前端应用
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 多账号测试 / 2026-08-12]`

## 工程流程治理

### 资产同步与代码审查门禁
Rule 和 tool 资产若不经 PR 审查直接同步到数据库，可能引入未经审核的规则 / 工具，污染规则库或引入安全风险。
- **判定规则**：所有 rule 和 tool 资产同步到数据库前，必须经过 Gitea PR review and approval 流程；`share_asset` 作为通信层传输机制，不替代 git PR 流程
- **反例**：`share_asset` 直接将规则写入目标仓库的 .trae/rules/，未经 PR 审查
- **正例**：`share_asset` 传输资产到目标仓库的分支 → 目标仓库创建 PR → 审查通过后合并到 main → 触发同步
- 适用范围：所有 rule / tool / knowledge 资产的跨仓库同步场景
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 2026-08-12]`

### Gitea 分支保护
若 Gitea 仓库未配置分支保护，开发者可直接推送 main 分支，绕过 PR 审查流程，未经审核的代码进入生产环境。
- **判定规则**：所有 Gitea 仓库必须配置分支保护规则：① main 分支禁止直接推送；② 必须通过 PR + 至少 1 个 approval 才能合并；③ 禁止 force push 到 main
- **反例**：`git push origin main` 直接推送，无 PR 审查
- **正例**：`git push origin feature-branch` → 创建 PR → approval 后合并
- 适用范围：所有 Gitea 仓库的 main / release 分支
`[来源类型: 导入 | 来源: project_memory.md Hard Constraints / 2026-08-12]`
