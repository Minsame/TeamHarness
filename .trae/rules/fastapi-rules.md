# fastapi-rules.md

> 本文件存放 FastAPI 框架相关的工程踩坑规则
> 来源：多项目经验提炼，跨项目复用。
> 追溯链格式：`[来源类型: 新提炼|导入 | 来源: ...]`

## API 设计

### 参数命名语义
参数命名与类型注解须与实际语义一致（如 `transport: httpx.BaseTransport` 不可当作 `client: httpx.BaseClient` 使用），否则 IDE 与静态检查无法发现隐性 bug。
`[来源类型: 新提炼 | 来源: Agent 1 / SubTask 1.1 / 第一波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 响应体状态标志聚合
响应体状态标志（如 `degraded`）应汇总所有来源信号（consistency 解析阶段 + 执行阶段），不得在单一返回点硬编码 `False` 覆盖已计算的降级信号。
`[来源类型: 新提炼 | 来源: Agent 4 / SubTask 4.4 / 第二波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 服务类 ↔ HTTP 路由注册一致性（契约三件套）
实现对外契约 API（spec/overview 声明的 HTTP 端点）时，"服务类 + HTTP 路由注册 + 契约验证用例"必须三件套同步交付，缺一会导致契约验证阶段才发现断层。仅实现服务类不注册路由时，服务类域内测试 PASS（直接调用服务类），但 HTTP 契约测试 FAIL（路由 404）——这种断层在单 Agent 域内测试中不可见，跨 Agent 契约验证才暴露。
- **判定规则**：每实现一个 spec/overview 声明的 HTTP 端点，必须：① 服务类 + 域内测试；② router 注册端点 + 端点测试（happy path + 服务未注入降级，如 503）；③ 契约验证（`router.routes` 含 `path` + `methods`，用 `TestClient` 验证非 404）
- **降级一致性**：服务实例通过模块级全局变量 + `configure_xxx()` 注入时，未注入端点应返回 503（与已注入端点的 200 区分），不阻断同 router 的其他路由
- 反例：`PRReviewDedupService` / `DashboardService` 服务类已实现，但 `governance_router` 只注册了 `/v1/metrics*`，缺 `/v1/review/dedup` 和 `/v1/governance/dashboard` → Agent 10 契约验证 FAIL（xfail 标记上报）
- 正例：服务类实现后立即在 `governance_router` 注册端点，并用 `TestClient.post("/v1/review/dedup")` 验证非 404；服务未注入时返回 503（不阻断 `/v1/metrics*` 路由）
- 适用范围：所有 FastAPI（或类似框架）多 Agent 协作场景，尤其是"服务类实现"与"路由注册"可能由不同 Agent / 不同阶段负责时
`[来源类型: 新提炼 | 来源: Agent 9 / SubTask 9.1+9.3 + Agent 10 / SubTask 10.2+10.6 / 第三波+第四波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 配置文件路由表 ↔ 代码 router 一致性校验
反向代理配置（nginx.conf）的路由表与 FastAPI 代码实际注册的 router 路径若不一致，单进程测试无法发现——契约测试用 `router.routes` 静态检查代码内路由，不经过 nginx 转发；nginx.conf 是纯文本配置，无自动化校验。表现为 nginx 转发到不存在端点的 service（404/502），或代码已注册的端点无 nginx 路由（外部不可达）。
- **判定规则**：每次 nginx.conf 变更或新增 HTTP 端点时，必须有测试解析 nginx.conf 的 `location` 块 → 路由表，与各 role 的 `router.routes` 实际路径比对，断言每条 nginx 路由都能在对应 service 找到匹配的 router
- **反例**：nginx.conf 配置 `/v1/distill/` → distill-service，但 distill_team 模块无任何 router（grep 无 `APIRouter`），`/v1/distill/team/*` 端点 404；nginx.conf 配置 `/v1/metrics` → distill-service，但该端点实际由 asset-service 的 governance_router 提供（且 nginx.conf 兜底注释自承"含 /v1/metrics*"走 asset-service，但精确匹配 `location = /v1/metrics` 优先级更高导致转发错误）
- **正例**：测试解析 nginx.conf 提取 `location /v1/distill/ { proxy_pass http://distill-service; }`，验证 distill role 的 `router.routes` 含至少一个 `/v1/distill/*` 路径；不匹配则 FAIL
- 适用范围：所有"反向代理 + 多 service"架构（nginx/traefik/envoy + FastAPI/Flask），尤其是路由表与服务端 router 分离维护时
`[来源类型: 新提炼 | 来源: 依赖分析 / nginx.conf + distill_team 模块 / 2026-08-10 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`
