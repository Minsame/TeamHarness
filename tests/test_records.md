# TeamHarness 测试记录

> 每次测试后更新。无改动的模块在后续测试可跳过（标记 `skip-eligible`）。
> 有改动的模块必须重新测试（标记 `retest-required`）。
> 计划与状态分离：本文件只记录已发生的事实，规划放 plans/ 下。

## 模块测试状态（截至 2026-08-11 联调测试后）

| 模块 | 负责域 | 最后测试 | 状态 | 改动 | 测试脚本 |
|------|--------|----------|------|------|----------|
| 鉴权（API Key + owner + scope） | 后端 | 2026-08-11 联调 | PASS | skip-eligible | tests/module_auth/test_backend.ps1, tests/integration/test_integration.ps1 |
| 登录（auth + 前端校验） | 前后端 | 2026-08-11 模块B | FAIL(1) | retest-required（前端待修复） | tests/module_frontend/test_frontend.spec.mjs |
| 图谱（links + graph BFS） | 后端 | 2026-08-11 联调 | PASS | skip-eligible | tests/integration/test_integration.ps1 |
| ACL（CRUD + 参数校验） | 后端 | 2026-08-11 联调 | PASS | skip-eligible | tests/integration/test_integration.ps1 |
| 前端 UI（对话框入口） | 前端 | 2026-08-11 模块B | FAIL(1) | retest-required（前端待修复） | tests/module_frontend/test_frontend.spec.mjs |
| API 基础设施（IntegrityError 409） | 后端 | 2026-08-11 联调 | PASS | skip-eligible | tests/integration/test_integration.ps1 |
| nginx 路径遍历 | 部署 | 2026-08-11 模块C | FAIL(1) | retest-required（nginx 待修复） | tests/module_nginx/test_nginx.ps1 |
| nginx 安全头/gzip/端口 | 部署 | 2026-08-11 模块C | FAIL(4) | retest-required（nginx 待修复） | tests/module_nginx/test_nginx.ps1 |
| CORS（OPTIONS 405） | 后端 | 2026-08-11 联调 | FAIL | retest-required（后端待修复） | tests/module_nginx/test_nginx.ps1 |
| 并发（唯一约束） | 后端 | 2026-08-11 模块A | PASS | skip-eligible | tests/module_auth/test_backend.ps1 |
| webhook（pr_merged） | 后端 | 2026-08-11 内联 | PASS(4) | skip-eligible | 内联脚本（待固化） |
| tool_review（分级审查） | 后端 | 2026-08-11 内联 | PASS(5) | skip-eligible | 内联脚本（待固化） |

## 已发现问题清单

### 已关闭（根因已定位并验证修复）

- [x] **AUTH-1**: ~~`/v1/assets/*` 完全无鉴权中间件~~
  - **真实根因**：镜像过期。本地源码 `server/assets/api.py` 已含 `require_member`/`_assert_owner`/`_can_view_asset`，但容器跑旧镜像
  - **修复**：联调阶段 `docker compose build --no-cache asset-service` 重建镜像
  - **验证**：联调 agent 23 个原 FAIL 全部转 PASS（无效 key→401、bob 改 alice→403、查 private→404、共享库不含 private/restricted）
  - **教训**：测试前必须确认镜像与源码一致，否则会得出虚假 FAIL。模块 A 用 `docker exec grep` 对比容器内代码与本地源码才发现根因
- [x] **COMPAT-1**: ~~IntegrityError 返回 500 而非 409~~
  - **真实根因**：同 AUTH-1，镜像过期。源码已含 `try IntegrityError → 409`
  - **修复**：同 AUTH-1，重建镜像
  - **验证**：联调阶段重复创建关联/ACL 均返回 409

### 已修复（2026-08-11 第二轮修复后）

- [x] **AUTH-2**: 无效 API Key 登录成功
  - **根因**：前端 `handleLogin` 未校验 `agent_id`
  - **修复**：[frontend/app.js:41](file:///d:/Code/TeamHarness/frontend/app.js#L41) 增加 `if (!resp || !resp.agent_id) { ElMessage.error("API Key 无效或已失效"); return; }`
  - **验证**：回归测试 PASS（app.js 含 agent_id 校验）
- [x] **UI-1**: 关联列表对话框 UI 不可达
  - **根因**：index.html 无 `@click="showLinksDialog"` 入口
  - **修复**：[frontend/index.html:240](file:///d:/Code/TeamHarness/frontend/index.html#L240) 我的规则库操作列添加"关联"按钮 `@click="showLinksDialog(row)"`
  - **验证**：回归测试 PASS（index.html 含 showLinksDialog 引用）
- [x] **CORS-1**: OPTIONS 返回 405
  - **根因**：server/app.py 无 CORSMiddleware
  - **修复**：[server/app.py:215](file:///d:/Code/TeamHarness/server/app.py#L215) 添加 `CORSMiddleware(allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])`
  - **验证**：curl OPTIONS 返回 200 + Access-Control-Allow-Origin
- [x] **NGINX-1**: 路径遍历返回 200
  - **根因**：nginx try_files SPA fallback + 客户端归一化路径
  - **修复**：[deploy/nginx.conf](file:///d:/Code/TeamHarness/deploy/nginx.conf) 用 `map $request_uri $is_path_traversal` 检测 `..` 和 `%2e%2e`，server 块 `if ($is_path_traversal) { return 400; }`
  - **验证**：curl --path-as-is 测 `..` 和 `%2e%2e` 都返回 400
- [x] **NGINX-2**: 安全头缺失
  - **修复**：nginx.conf server 块添加 X-Frame-Options / X-Content-Type-Options / X-XSS-Protection / Referrer-Policy
- [x] **NGINX-3**: gzip 未启用
  - **修复**：nginx.conf http 块添加 gzip on + gzip_types
- [x] **NGINX-4**: 端口暴露过多
  - **修复**：docker-compose.yaml 注释掉 postgres/qdrant/gitea 的 ports（开发时取消注释）
- [x] **NGINX-5**: 静态资源无缓存策略
  - **修复**：nginx.conf 添加 `location = /index.html` (no-cache) 和 `location ~* \.js$` (max-age=86400)

### 已修复（2026-08-12 前端异常测试后）

- [x] **STATE-1**: memberId 篡改泄露他人资产（安全漏洞）
  - **根因**：后端 `list_assets` 的 owner 参数未校验与 API Key 对应关系
  - **修复**：[server/assets/api.py:307](file:///d:/Code/TeamHarness/server/assets/api.py#L307) `list_assets` 中 `if owner and owner != member_id: raise 403`；`get_member_stats` 同理校验
  - **验证**：alice 用自己 key 查 bob 资产 → 403；查自己资产 → 200；查共享库（无 owner）→ 200
- [x] **INPUT-1**: 后端缺 grantee_id 格式校验
  - **修复**：[server/assets/api.py:892](file:///d:/Code/TeamHarness/server/assets/api.py#L892) `create_asset_acl` 中 `re.match(r'^[a-zA-Z0-9_\-]+$', grantee_id)` + Pydantic `max_length=128`
  - **验证**：XSS/路径遍历 grantee_id → 400；合法 grantee_id → 200
- [x] **STATE-2**: 无 401 拦截器
  - **修复**：[frontend/services/api.js:36](file:///d:/Code/TeamHarness/frontend/services/api.js#L36) `request()` 中 `if (resp.status === 401)` → 清除 localStorage + reload + 提示
  - **验证**：api.js 含 401 状态检查 + localStorage 清除 + reload
- [x] **ACTION-1**: handleUpdateScope 重复提交
  - **修复**：[frontend/app.js:269](file:///d:/Code/TeamHarness/frontend/app.js#L269) 加 `if (scopeUpdating.value) return` 防重入守卫
  - **验证**：app.js 含防重入守卫
- [x] **INPUT-2/3**: 超长输入触发后端 500
  - **修复**：[server/binding/api.py:186](file:///d:/Code/TeamHarness/server/binding/api.py#L186) `IssueApiKeyRequest` 加 `max_length=128`；`CreateAclRequest` 同理
  - **验证**：超长 member_id → 422；超长 grantee_id → 422
- [x] **STATE-3**: fetch 无超时
  - **修复**：[frontend/services/api.js:16](file:///d:/Code/TeamHarness/frontend/services/api.js#L16) `request()` 中 `AbortController + setTimeout(15000)` + 捕获 AbortError 提示"请求超时"
  - **验证**：api.js 含 AbortController
- [x] **STATE-4**: 无 history 集成
  - **修复**：[frontend/app.js:97](file:///d:/Code/TeamHarness/frontend/app.js#L97) `handleMenuSelect` 中 `history.pushState`；`onMounted` 中 `popstate` 监听 + URL hash 恢复
  - **验证**：app.js 含 pushState
- [x] **checkLogin 增强**: 校验 key 有效性
  - **修复**：[frontend/app.js:23](file:///d:/Code/TeamHarness/frontend/app.js#L23) `checkLogin` 改为 async，调 `lookupApiKey` 校验 key 有效性，无效则清除 localStorage
  - **验证**：篡改 apiKey 为无效值 → 回到登录页

### 仍需修复

- [ ] **COMPAT-4**: API 响应 Content-Type 缺 charset=utf-8（低优先级，FastAPI 默认行为）

## 测试轮次记录

### 第 1 轮（2026-08-11，废弃）
- **方式**：3 个 agent 按测试类型拆分（前端 E2E / 实际环境 / 兼容性）并发
- **问题**：按类型拆分导致同一数据库被多个 agent 并发操作，互相干扰（前端 agent 点 ACL 添加，实际环境 agent 同时创建/删除关联）
- **结论**：拆分方式错误，结果不可靠
- **产出**：tests/frontend/, tests/realenv/, tests/compat/（保留但标记为"首轮废弃"）

### 第 2 轮（2026-08-11，有效）
- **方式**：3 个 agent 按模块拆分（鉴权后端 A / 前端 B / nginx C）并发域内测试 → 1 个单 agent 联调
- **模块 A（后端鉴权）**：39 用例 16/23
  - 关键发现：23 个 FAIL 全因镜像过期，源码已含鉴权代码
  - 产出：tests/module_auth/test_backend.ps1
- **模块 B（前端）**：42 用例 36/6
  - 确认 AUTH-2 + UI-1
  - 产出：tests/module_frontend/test_frontend.spec.mjs + 27 张截图
- **模块 C（nginx）**：75 用例 55/20
  - 路径遍历 + 安全头 + gzip + 端口暴露
  - 产出：tests/module_nginx/test_nginx.ps1
- **联调**：34 用例 34/0
  - 重建镜像后 AUTH-1/COMPAT-1 全部转 PASS
  - 4 个跨模块场景（alice 管理 / bob 受限 / 图谱+ACL 联动 / 权限边界）全 PASS
  - 产出：tests/integration/test_integration.ps1

## 测试脚本索引

| 轮次 | 脚本 | 类型 | 路径 | 状态 |
|------|------|------|------|------|
| 第 2 轮 | 后端鉴权 | PowerShell | tests/module_auth/test_backend.ps1 | 有效 |
| 第 2 轮 | 前端 E2E | Playwright | tests/module_frontend/test_frontend.spec.mjs | 有效 |
| 第 2 轮 | nginx 部署 | PowerShell | tests/module_nginx/test_nginx.ps1 | 有效 |
| 第 2 轮 | 联调集成 | PowerShell | tests/integration/test_integration.ps1 | 有效 |
| 第 1 轮 | 前端 E2E（废弃） | Playwright | tests/frontend/e2e.spec.mjs | 废弃（被模块 B 替代） |
| 第 1 轮 | 权限边界（废弃） | PowerShell | tests/realenv/test_permissions.ps1 | 废弃（被模块 A 替代） |
| 第 1 轮 | 并发（废弃） | PowerShell | tests/realenv/test_concurrency.ps1 | 废弃（被模块 A 替代） |
| 第 1 轮 | 错误处理（废弃） | PowerShell | tests/realenv/test_errors.ps1 | 废弃（被模块 A 替代） |
| 第 1 轮 | 数据一致性（废弃） | PowerShell | tests/realenv/test_consistency.ps1 | 废弃（被联调替代） |
| 第 1 轮 | 兼容性（废弃） | PowerShell | tests/compat/test_compat.ps1 | 废弃（被模块 C 替代） |

## 待固化项
- [x] webhook pr_merged 测试脚本 → tests/test_webhook_pr_merged.py（8 用例全 PASS）
- [x] tool_review 分级审查测试脚本 → tests/test_tool_review_grading.py（14 用例全 PASS）
- [ ] 清理第 1 轮废弃脚本（tests/frontend/, tests/realenv/, tests/compat/）
