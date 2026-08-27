# python-rules.md

> 本文件存放 Python 语言特性相关的工程踩坑规则
> 来源：多项目经验提炼，跨项目复用。
> 追溯链格式：`[来源类型: 新提炼|导入 | 来源: ...]`

## 文件系统

### 目录遍历安全
`Path.iterdir()` / `Path.glob()` 调用前必须先 `is_dir()` 短路（目录不存在视为空）；禁止把 `is_dir()` 判断写在生成器表达式内部（生成器内层已先调用 `iterdir()` 会抛 `FileNotFoundError`）。
`[来源类型: 新提炼 | 来源: Agent 1 / SubTask 1.10 / 第一波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 解析

### 状态机解析空行处理
状态机解析（如 markdown 段落）须区分"跳过空行"与"状态切换"——空行不应直接触发状态切换，应跳过后看下一行内容决定状态。否则 header 后空行会立即结束 metadata 区，后续 metadata 行被误归入 body。
`[来源类型: 新提炼 | 来源: Agent 1 / SubTask 1.7 / 第一波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 二进制魔数匹配
用 `startswith(magic)` 判定文件头时，`read(N)` 的 N 须 ≥ `len(magic)`（如 `b"\x00GIT-CRYPT"` 是 9 字节，`read(8)` 永不匹配）。建议读取 `len(magic) + 冗余` 字节。
`[来源类型: 新提炼 | 来源: Agent 1 / SubTask 1.8 / 第一波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## API 设计

### 打包/解压 arcname 对称
打包/解压 API 的 arcname 语义必须对称且文档化：
- 打包带顶层目录名时，解压须处理"顶层名与目标路径不匹配"（检测单子目录并上移内容）
- 解压目标参数语义应与同函数其他参数同构（如 `sqlite_dest` 是文件路径，`repo_dest` 也应是目录路径而非父目录）
`[来源类型: 新提炼 | 来源: Agent 3 / SubTask 3.3 / 第一波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 模板

### JSON 模板禁用 str.format()
含 JSON 大括号的 prompt/模板禁用 `str.format()`（`{candidates}` 会被误判为占位符抛 `KeyError`），改用 `.replace()` + 自定义占位符（如 `__X__`）或 `string.Template` 的 `$x` 语法。
`[来源类型: 新提炼 | 来源: Agent 5 / SubTask 5.3 / 第二波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 数据结构

### dataclass 字段语义四准则 + 业务规则优先
dataclass 作为 DTO / 领域模型时须遵守四条准则，且业务规则优先于默认值：
- **DTO 默认值**：除语义必填字段外，其余字段（计数器、可选返回值）应给默认值，避免调用方须按顺序传所有位置参数
- **@property 派生值不可构造**：dataclass 中用 `@property` 暴露的派生值（如加权总分 `total`）不能出现在构造参数中，测试和调用方都应只设置原始字段
- **测试构造只设原始字段**：测试构造 dataclass 时须遵守上述约束，只设置原始字段，不传 @property 派生值（如 `SixDimScore(total=0.8)` 会触发 `TypeError`）
- **默认值不覆盖业务规则**：dataclass 字段默认值不能隐式覆盖业务规则（如冷启动期所有产出 `confidence` 必须为 `low`，不能被 dataclass 默认值 `"medium"` 覆盖；业务规则须在赋值点显式强制）
`[来源类型: 新提炼 | 来源: Agent 5 / SubTask 5.1 + Agent 7 / SubTask 7.x + Agent 8 / SubTask 8.4 + 8.10 / 第一波+第二波+第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 参数传递完整性（注入 + 透传 + 契约同步）
参数传递须在三个维度保持完整，任一维度断裂都会导致隐性 bug：
- **透传完整性（数据层）**：factory / helper 函数接受的参数必须全部传递到底层 VO / 构造函数，**禁止"接受但不传"**。若某字段由服务内部计算（如 `content_hash`），helper 须显式允许通过 VO 覆盖，否则测试用显式指定的字段值会被静默忽略
  - 反例：`upsert_asset(content_hash="hash-abc")` 接受参数但未传给 `AssetVO.content_hash`，服务内部按内容重算 hash，测试用 `"hash-abc"` 检索时无匹配
  - 正例：`make_asset_vo(content_hash=content_hash)` 透传到 `AssetVO(content_hash=content_hash)`，服务优先用 VO 已有值
- **注入参数必须实际使用（逻辑层）**：方法 / 函数接受的注入参数（如依赖注入、策略注入）必须在内部实际使用，**禁止"接受后忽略"**。否则测试注入的值（如 `deep_stage`）不生效，方法仍走默认分支，导致测试断言失败
  - 反例：`_get_deep_stage(deep_stage=None)` 接受参数但内部直接 `self.deep_stage`，测试注入的 `deep_stage` 被忽略
  - 正例：`stage = deep_stage if deep_stage is not None else self.deep_stage`
- **接口契约同步（契约层）**：接口契约变更（新增必填参数 / 改签名）后，所有调用方 / 测试构造须同步更新，否则调用时缺参数抛 `TypeError` 或语义不符
  - 反例：`Signal` 新增 `content_excerpt` 参数，测试构造 `Signal(...)` 未传该参数 → 缺参数错误
  - 正例：契约变更时全局检索所有构造点同步更新
`[来源类型: 新提炼 | 来源: Agent 7 / SubTask 7.x + Agent 9 / SubTask 9.1 + 9.13 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### 按可空字段分组的 None 语义
按可空字段（如 `category`、`owner`）分组聚合时，**None 值的语义须显式处理**——是"同组合并"还是"各自独立"取决于业务逻辑，不能用 `None` 作为 dict key 默认合并（否则所有无值项被错误合并到同一组）。
- 反例：`collect_convention_clusters` 按 `r.category` 分组，`category=None` 的资产全部合并到 `None` 键 → 不同资产被误归为同簇
- 正例：无 category 的资产用 `f"__individual_{r.id}"` 作为唯一 key，确保各自独立成簇
- 判定规则：分组前明确 None 语义——同组（用固定 key 如 `"__unspecified__"`）vs 各自独立（用唯一 key 如 `f"__individual_{id}"`）
`[来源类型: 新提炼 | 来源: Agent 8 / SubTask 8.5 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 导入

### 模块级名称先定义后使用 + 别名单一来源
Python 模块级代码自上而下执行，装饰器 / 基类 / 运行时求值的类型注解等模块级名称**必须先 import / 定义后使用**。重构 import 别名（如 `from dataclasses import dataclass as dataclass_local`）时须确保所有引用点同步更新；**无必要时不要别名**，别名越少越不容易出现"别名引用点遗漏"。
- 反例：`@dataclass_local` 装饰器出现在 `from dataclasses import dataclass as dataclass_local` 之前 → `NameError`
- 正例：顶部 `from dataclasses import dataclass`，直接 `@dataclass`，不引入别名
`[来源类型: 新提炼 | 来源: Agent 9 / SubTask 9.1 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

### import 语句不支持条件表达式（探测式 import 正确写法）
Python `import` 语句**不能用三元表达式 / 条件表达式**（`from X import Y if cond else None` 是 `SyntaxError`，不是合法 Python 语法）。需要"可能存在也可能不存在"的探测式 import 时，必须用 `try/except ImportError`。
- 反例：`from server.x import Y if hasattr(__import__(...), "Y") else None` → `SyntaxError`（整个模块无法加载，所有测试 collection error）
- 正例：`try: from server.x import Y except ImportError: pass`（Y 可能不存在时跳过，后续用 `hasattr` 兜底）
- 适用范围：所有"可选导出 / 不同实现版本可能缺失的符号"的探测式 import
`[来源类型: 新提炼 | 来源: Agent 10 / SubTask 10.5 / 第四波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 替身与降级

### Stub / No-op 替身类构造签名兼容
当某依赖（如 `prometheus_client`）可选未安装时，常用 Stub / No-op 类降级。**Stub 类必须与被替身类构造签名兼容**：实现 `__init__(self, *args, **kwargs): pass` + 各方法 no-op 返回，否则调用方按真实类签名传参（如 `Gauge("name", "desc", ["labels"])`）会抛 `takes no arguments`。
- 反例：`_NoOpMetric` 无 `__init__`，`Gauge("name", "desc", ["labels"])` → `TypeError: takes no arguments`
- 正例：`_NoOpMetric.__init__(self, *args, **kwargs): pass` + `labels(**_)` 返回 self
- 适用范围：所有"可选依赖 + stub 降级"模式（Prometheus / qdrant-client / pgvector 等）
`[来源类型: 新提炼 | 来源: Agent 9 / SubTask 9.5 + 9.6 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 路径处理

### 路径层级 split 语义
按 module_path 前缀统计直接子模块时，`rest.split("/", 1)` **必返回至少 1 元素**（无分隔符时 `parts=["rest"]`，`len=1`）。用 `if len(parts) > 1 and parts[0]` 作条件会**漏掉无子路径的直接子层**（如 `modules/backend/auth` 相对 `modules/backend/` 的 rest 是 `"auth"`，`split("/", 1) → ["auth"]`，`len=1` 被跳过）。正确写法：`if parts[0]:`（取第一段为子模块名，无论是否有更深层级）。
- 统计语义："直接子层算一个，孙层不算" → 取 `parts[0]` 即可（孙层 `auth/sub` 的 `parts[0]="auth"` 与直接子层 `auth` 的 `parts[0]="auth"` 自动合并）
`[来源类型: 新提炼 | 来源: Agent 9 / SubTask 9.4 + 9.11 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 过滤聚合

### 过滤循环空结果默认值语义
"过滤 + 聚合"循环（如 `for x in items: if x.score < threshold: continue; result.append(x)`）执行后若结果为空，**默认值不能保留"待定"语义**，须根据业务语义显式设为"无冲突"/"通过"。否则会向调用方传递错误信号（如 PR Review 中"有候选但无一达阈值"本应 `keep_separate`，却保留默认 `needs_review`）。
- 判定规则：循环后 `if result:` 分支处理非空结果；`else:` 分支必须显式设默认值，不能省略
- 业务映射：去重无匹配 → `keep_separate`；校验无违规 → `pass`；告警无触发 → `ok`
`[来源类型: 新提炼 | 来源: Agent 9 / SubTask 9.1 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`

## 容错

### 冷启动期功能容错（content-based fallback）
依赖向量检索 / embedding 的模块（聚类、召回、语义去重），**必须有 content-based fallback**，否则冷启动期（embedding 未就绪、向量库为空、`embedding_id IS NULL`）功能完全失效——所有资产成为孤点，无法形成簇或匹配。
- 反例：`_find_neighbors` 在 `embedding_id` 为空时直接返回 `[]` → 冷启动期聚类完全失效
- 正例：向量检索无结果时退化为 content 匹配（优先 `content_hash` 精确匹配，其次 `content_snapshot`），相似度固定 1.0
- 适用范围：所有"向量依赖 + 冷启动期"场景（聚类 / 召回 / 语义去重 / 相似度计算）
`[来源类型: 新提炼 | 来源: Agent 8 / SubTask 8.1 / 第三波 | 迁移自: TeamHarness gotchas.md | 迁移时间: 2026-08-11]`
