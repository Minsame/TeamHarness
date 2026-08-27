# pytest-rules.md

> 本文件存放 pytest 测试框架相关的工程踩坑规则
> 来源：多项目经验提炼，跨项目复用。
> 追溯链格式：`[来源类型: 新提炼|导入 | 来源: ...]`

## 测试

### 测试全局状态隔离
测试中使用的全局/上下文状态（`contextvars.ContextVar`、FastAPI 模块级服务变量等）必须在 `conftest.py` 用 `autouse` fixture 或 `yield` teardown 清理，避免跨用例状态污染。
- contextvars：每个测试前后清理 contextvar 值
- FastAPI 模块级变量：`configure_xxx_api()` 注入的全局变量须在 fixture 末尾 reset 回 None
`[来源类型: 新提炼 | 来源: Agent 4 / SubTask 4.10 + Agent 5 / SubTask 5.11 / 第一波+第二波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 测试构造物语义
测试构造物须保持语义准确：
- **fixture 须单一语义**：不可同时含合规与违规元素；违规元素应拆为独立 fixture（如"干净仓库"与"含孤儿资产仓库"分开）
- **mock 须模拟真实语义**：mock 文件系统类 API（如 `ls_tree`）应返回直接子条目而非全量扁平列表，否则递归遍历会失败；mock HTTP 响应对象须实现真实类全部被调用的方法（如 `_FakeResponse` 须实现 `.json()`，不能只 stub `.text`）
- **fixture 正交标记统计**：测试集 fixture 若有多个正交标记（如 `is_convention=True` + `expected_cold_start=True`），统计任一标记数量时必须包含交叉场景，不能只数"纯"场景
`[来源类型: 新提炼 | 来源: Agent 1 / SubTask 1.10 + Agent 4 / SubTask 4.11 + Agent 7 / SubTask 7.x + Agent 8 / SubTask 8.10 / 第一波+第二波+第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### Windows mtime 测试不可靠
Windows 文件系统 mtime 精度有限（通常 2 秒粒度），测试中通过 `time.sleep()` + 比较文件 mtime 判定"文件已修改"不可靠——同一秒内修改的文件 mtime 可能相同。须用 `os.utime(path, (atime, mtime))` 显式设置确定性时间戳，确保断言稳定可复现。
- 反例：`time.sleep(1); assert file.mtime > old_mtime` → Windows 上可能相等 → 断言随机失败
- 正例：`os.utime(path, (time.time(), time.time() + 100))` 显式设置未来时间戳
`[来源类型: 新提炼 | 来源: Agent 7 / SubTask 7.x / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 确定性随机数生成
确定性抽样 / 随机数生成函数若需在循环中产生分布，**种子必须与循环变量组合**，不能只用固定种子。固定种子会导致每次调用创建的新 RNG 返回同一个随机值，所有迭代结果相同（如全部 `False` 或全部命中同一分支），破坏分布语义。
- 反例：`should_human_review(skip=i, seed=42)` 内部 `random.Random(42)` → 所有 i 返回同一值 → true_count=0
- 正例：`random.Random(base + skip_count_this_week)` → 不同输入产生不同输出
- 适用范围：所有"确定性 + 循环调用"场景（抽样、洗牌、分桶）
`[来源类型: 新提炼 | 来源: Agent 8 / SubTask 8.6 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 集成测试不假设实现名称与签名
集成测试跨模块验证时，**不要假设实现的具体类名 / 模块位置 / 构造签名**——这些是各 Agent 自主决定的实现细节。须先 `Grep`/`Read` 查实际实现再写断言，或用 `try/except ImportError` + `hasattr` 容错。常见假设陷阱：
- **类名假设**：`EmbeddingMigrationService` 实际叫 `EmbeddingMigration`；`BudgetManager` 在 `budget` 模块而非 `llm_provider` 模块
- **常量位置假设**：`COLD_START_CONFIDENCE` 是类属性（`ColdStartBypass.COLD_START_CONFIDENCE`）而非模块级常量
- **类型假设**：`StorageBackend` 是 `@dataclass` 不可迭代，`StorageKind` 才是 `Enum` 可迭代（`for b in StorageBackend` → `TypeError: 'type' object is not iterable`）
- **构造签名假设**：`Database(url)` 抛 `TypeError`（只接受关键字参数 `sync_engine=`/`async_engine=`），须用工厂函数 `create_database(sync_url=..., async_url=...)`
- 判定规则：集成测试写断言前先查实现（`Grep "^class \w+"` / `Read` 构造函数），不凭 spec 文档的类名假设
`[来源类型: 新提炼 | 来源: Agent 10 / SubTask 10.5 + 10.6 / 第四波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### Mock 返回值格式须匹配被测函数期望的 schema
mock 依赖（如 mock LLM）的返回值**必须匹配被测函数期望的格式**，否则解析失败触发 fallback，误判为功能 bug。JSON 字符串解析后访问 `.get()` 时，若实际是 list 而非 dict 会抛 `'list' object has no attribute 'get'`。
- 反例：mock LLM 返回 `{"content": '[{"category":"..."}]'}`（JSON 数组），但被测函数期望 `{"candidates":[...]}`（JSON 对象含 candidates 键）→ `_parse_llm_json` 解析成功但 `data.get("candidates")` 返回 None → 触发 fallback → 测试误判"注入 LLM 后仍走 fallback"
- 正例：mock LLM 返回 `{"content": '{"candidates":[{"category":"..."}]}'}`（符合 schema）→ 解析成功 → `used_fallback=False`
- 适用范围：所有"mock 返回 JSON + 被测函数按 schema 解析"场景（LLM / HTTP API / 配置文件）
`[来源类型: 新提炼 | 来源: Agent 10 / SubTask 10.6 / 第四波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### xfail 标记须验证测试逻辑本身无 bug
`@pytest.mark.xfail`（无 `raises=` 参数时）默认捕获**任何异常**使测试显示为 xfail，会掩盖测试代码本身的 bug（如遍历到错误类型抛 `AttributeError`、`NameError`、`TypeError` 等）。这类 bug 在 xfail 下显示为"符合预期"，移除 xfail 后才暴露为 ERROR/FAIL——契约修复后移除 xfail 时往往同时触发测试逻辑 bug，导致"修复了实现却测试 ERROR"的困惑。
- **xfail 测试也须保证内部代码无 bug**：标记 xfail 前 / 移除 xfail 后，用 `pytest --runxfail`（忽略 xfail 标记）跑一遍，验证测试逻辑本身能正确区分 pass/fail，而非因代码 bug 异常退出
- **strict=True 更危险**：strict xfail 下，测试因 bug 异常被捕获为 xfail（"预期 fail"），修复实现后测试仍因 bug ERROR → XPASS-fail（strict 下 XPASS 算失败）→ 误判为"实现未修复"。务必先 `--runxfail` 验证测试逻辑
- 适用范围：所有 xfail 标记的测试（尤其是"契约缺失/功能未实现"类 xfail，待修复后移除标记时）
`[来源类型: 新提炼 | 来源: Agent 10 / SubTask 10.6 / 第四波 — xfail 移除后暴露 candidates 过滤 bug | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### dir(module) 名称过滤须 isinstance 类型校验
用 `dir(module)` + 名称模式（如 `"router" in name.lower()`）收集模块属性时，会捕获**所有名字含该模式的属性**，包括 import 进来的类（如 `from fastapi import APIRouter` 使 `APIRouter` 成为模块属性）、函数（如 `build_router`）、子模块等，不只是目标实例。遍历到非目标类型时，访问其实例属性（如 `APIRouter.routes`）会抛 `AttributeError`（类无实例属性）。
- 反例：`candidates = [getattr(m, n) for n in dir(m) if "router" in n.lower()]` → 捕获 `APIRouter` 类 + `build_router` 函数 + `governance_router` 实例 → `for r in candidates: for route in r.routes:` 遍历到类时抛 `AttributeError: type object 'APIRouter' has no attribute 'routes'`
- 正例：① `isinstance` 类型过滤：`[getattr(m, n) for n in dir(m) if isinstance(getattr(m, n), APIRouter)]`；② 直接引用具名实例：`from server.x import governance_router`（已知模块只有一个目标 router 时最简洁）
- 适用范围：所有 `dir(module)` + 名称模式过滤收集属性的场景（收集 router / service / config 实例等）
`[来源类型: 新提炼 | 来源: Agent 10 / SubTask 10.6 / 第四波 — test_api_contracts.py candidates 过滤 bug | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`


### 跨进程边界须有集成测试覆盖
单进程内 import + mock 的测试无法覆盖跨进程边界（nginx ↔ FastAPI、PG ↔ SQLAlchemy、CDN ↔ 浏览器），所有跨进程链路都是测试盲区。单元测试用 mock 合理，但合并前必须有跨进程集成测试覆盖关键链路。
- **判定规则**：项目若有以下任一跨进程边界，CI 必须有对应的集成测试：① 反向代理（nginx/traefik）→ 应用服务器：测试真实 HTTP 转发链路；② 应用 → 外部 DB：测试真实驱动加载 + schema 建表 + 索引创建；③ 前端 CDN 依赖：测试 CDN 资源可达或纳入构建。最小覆盖：docker-compose up → 真实 HTTP 请求关键端点 → 验证响应
- **反例**：TeamHarness 所有测试都是单进程内 import + mock，nginx.conf 路由表无任何测试解析校验，nginx ↔ FastAPI 断层（/v1/distill/ 转发到无 router 的 service）只在生产暴露；test_e2e_and_compose.py 用 mock_server 模拟，不真正 docker compose up
- **正例**：CI 用 docker-compose up -d --wait 起真实服务，httpx 请求 /healthz + 关键业务端点，验证 nginx 转发 + FastAPI 处理 + PG 持久化全链路；nginx.conf 路由表有测试解析并校验与 router.routes 一致性
- 适用范围：所有有跨进程边界的项目（反向代理、多服务 compose、外部 DB、CDN 前端）
`[来源类型: 新提炼 | 来源: 依赖分析 / tests/ + deploy/ / 2026-08-10]`
