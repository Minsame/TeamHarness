# TeamHarness 技术方案

> 目标：搭建一个团队级 AI 协作资产共享平台，沉淀成员的规则 / 记忆 / Skill / Tool，自动去重并提炼出可复用的泛化 Prompt。软件本身只负责资产与 harness 文件的管理，不介入代码测试环节。

---

## 1. 背景与目标

### 1.1 现状痛点
- 每个成员本地都有自己的 rules / memory / skills / tools，彼此割裂，重复造轮子。
- 优秀经验难以沉淀和传承，新人需要重新摸索。

### 1.2 目标
1. **资产共享**：中央服务作为共享区域，接收并管理成员的规则、记忆、skill、tool。
2. **去重提炼**：对入库资产做内容级与语义级去重，并由 LLM 提炼出泛化 Prompt。
3. **Git 化管理**：共享资产以 Git 仓库为单一数据源（SSOT），支持版本、分支、回滚、Code Review。
4. **本地互通**：客户端只负责读写本地记忆文件夹，并作为用户 coding 软件与共享控制端之间的桥梁。

### 1.3 范围边界
- **做**：harness 资产（rules/memory/skills/tools/prompts）的采集、去重、归并、提炼、分发；本地记忆文件夹读写与结构指引；与控制端交互。
- **不做**：业务代码细节管理、代码测试、联调、CI/CD 执行、多租户隔离（暂不支持）。

---

## 2. 整体架构

**核心原则：git 为 SSOT（人协作）+ DB 为派生索引层（AI 召回/装配）。**
- 资产文件、版本历史、权限、Review 流程：git 仓库（权威源）
- 向量索引、元数据索引、召回 API、Agent 装配表：DB 索引层（从 git 派生，可重建）
- 人走 git 流程，AI 走 DB 索引，两条路互不干扰

```
┌──────────────────────────────────────────────────────────────┐
│                       客户端层（成员侧）                        │
│  ┌────────────────────────────────────────────────────────┐  │
│  │ TeamHarness Client                                      │  │
│  │  ├─ 对话记录采集（从 coding 软件读取会话历史）          │  │
│  │  ├─ 一级提炼（个人 dream：对话 → harness 资产）         │  │
│  │  ├─ 本地记忆文件夹读写（= git working copy）            │  │
│  │  ├─ git 同步封装（sync / pr / 冲突辅助）                │  │
│  │  ├─ AI 召回：走服务端索引 API（有网）/ 本地文件（离线） │  │
│  │  └─ 与 coding 软件（如 Trae）共享本地文件夹             │  │
│  └────────┬─────────────────┬──────────────┬───────────────┘  │
└───────────┼─────────────────┼──────────────┼──────────────────┘
  读对话    │      git        │   召回API    │  装配清单
            │   push/pull     │  (有网时)    │  (有网时)
            ▼                 ▼              ▼
┌──────────────┐  ┌───────────────────────────────────────────┐
│ coding 会话  │  │              中央服务层                      │
│ sessions/    │  │                                             │
│ *.jsonl      │  │  ┌─────────── git SSOT 层 ─────────────┐   │
└──────────────┘  │  │  Git Repo (资产文件/历史/权限/Review)│   │
                  │  └──────────┬──────────────────────────┘   │
                  │             │ webhook (push/merge)          │
                  │             ▼                               │
                  │  ┌─────────── DB 派生索引层 ──────────────┐ │
                  │  │  ┌────────────┐  ┌──────────────────┐  │ │
                  │  │  │Asset Service│  │ Recall Service   │  │ │
                  │  │  │ PR Review  │  │ /v1/recall/list  │  │ │
                  │  │  │ 语义去重   │  │ /v1/recall/read  │  │ │
                  │  │  └─────┬──────┘  └────────┬─────────┘  │ │
                  │  │        │   Distillation   │            │ │
                  │  │        │   Engine(二级)   │            │ │
                  │  │        └────────┬──────────┘            │ │
                  │  │                 │                       │ │
                  │  │  ┌──────────────▼───────────────────┐  │ │
                  │  │  │ PostgreSQL(元数据) + 向量库(语义)│  │ │
                  │  │  │ + Agent 装配表                   │  │ │
                  │  │  └──────────────────────────────────┘  │ │
                  │  └─────────────────────────────────────────┘ │
                  │           Provider 抽象层                    │
                  └──────────────────────────────────────────────┘
```

### 提炼链路总览
```
coding 对话记录 ──一级提炼(个人dream)──► 个人 harness 资产(L2)
                                              │ git push
                                              ▼
                                    中央 git 仓库(团队L2, SSOT)
                                              │ webhook 同步
                                              ▼
                                    DB 派生索引层(向量/元数据/装配)
                                              │ 二级提炼(团队dream)
                                              ▼
                                    泛化 Prompt 共享池(L3, 回写 git)
```

### 部署形态
- 服务端以 **HTTP API** 形式提供能力，不绑定具体部署环境（自建机房 / 云主机 / 容器化均可）。
- 部署侧只需保证 API 可达，客户端通过配置服务端地址接入。
- **DB 索引层可重建**：从 git 仓库全量重新扫描构建，DB 故障不影响 git 协作。

---

## 3. 核心模块设计

### 3.1 资产服务（Asset Service）

#### 3.1.1 资产类型
| 类型 | 说明 | 示例 |
|------|------|------|
| rule | 编码规范、风格约束、团队约定 | "提交前必须跑 lint" |
| memory | 项目记忆、决策记录、踩坑笔记 | "X 模块用 Y 协议" |
| skill | 可复用的技能包（含 prompt + 工具链） | "DB 迁移 skill" |
| tool | 可被 Agent 调用的工具/脚本 | "lint runner" |
| prompt | 提炼后的泛化 Prompt | "代码审查通用模板" |

#### 3.1.2 资产 schema
```yaml
asset:
  id: uuid
  type: rule | memory | skill | tool | prompt
  owner: member_id
  scope: private | team | restricted | public
  content: text | file_ref
  content_hash: sha256          # 服务端语义去重辅助
  embedding_id: vector_ref      # 语义去重
  tags: [string]
  version: semver
  module_path: path             # 组织层级路径（如 modules/backend），根级留空
  category: <type>-<module>     # 功能分类标签（受控词汇表，见 3.2c），用于自动装配
  related_to: [asset_id]        # 相似但独立的关联资产（服务端归并标记）
  created_at / updated_at
```

> 注：`module_path` 是**组织层级**（资产在仓库的物理位置），与 3.3.1 的**提炼层级** L1/L2/L3 正交，后者描述资产从对话到泛化 Prompt 的提炼阶段。
> `category` 与 `module_path` 也正交：module_path 是物理位置，category 是功能分类（受控词汇表，见 3.2c）。

#### 3.1.3 同步模型

**双轨同步：git 负责资产版本与协作（SSOT），DB 索引层负责派生索引与召回（从 git 同步）。**

##### 仓库关系
```
中央仓库 (teamharness-shared, bare repo on GitLab/Gitea)
    ▲                                  │
    │ git push                         │ git pull
    │                                  ▼
成员本地 working copy (= 本地记忆文件夹)

中央仓库 ──webhook(push/merge)──► DB 索引层(派生, 可重建)
```

##### 人协作：git 流程
```
1. 本地变更（一级提炼产出 / 手动编辑）→ git add + commit
2. 同步前：git pull --rebase origin main（拉取远端最新）
3. 有冲突 → git 标准冲突解决（手动或客户端辅助 diff 工具）
4. git push origin main
```

死循环、增量、冲突合并、删除传播、历史回溯全部交给 git，不自建同步协议。

##### DB 索引层同步：webhook 驱动（基于 INDEX.md 增量扫描）
```
git push/merge ──► webhook 触发服务端
                     │
                     ▼
              服务端 git fetch 最新 main
                     │
                     ▼
              diff 新旧 commit 找变更文件
                     │
              ┌──────┴──────┐
              ▼             ▼
        INDEX.md 变更    资产文件变更
              │             │
              ▼             ▼
        解析层级结构    按 INDEX.md 清单定位资产
        更新 module 树  计算 embedding / 删除索引项
              │             │
              └──────┬──────┘
                     ▼
              更新元数据表 + 装配表（自动绑定匹配）
              更新 module_stats 镜像表（从 INDEX.md counts 派生）
```

- **增量同步**：基于 commit diff + INDEX.md 清单，只处理变更文件，不全量重建、不全仓库扫描。
- **可重建**：任何时刻可从 git 全量重新扫描 INDEX.md + 资产文件构建 DB 索引，DB 不是真相源。
- **最终一致**：DB 索引可能短暂滞后于 git（webhook 异步处理），但最终一致；AI 召回若需最新可回退到 git 读取。
- **防孤岛校验**：webhook 同步时检查"资产文件存在但 INDEX.md 未登记"→ 告警并跳过索引（PR Review 阶段应已阻断，此为兜底）。
- **counts 维护原则**：INDEX.md 的 `counts` 字段由**人在 PR 中维护**（资产新增/删除时同步改 counts），服务端不回写 git；webhook 同步时从 git INDEX.md 读取 counts 写入 DB 镜像表 `module_stats`，治理看板（8.12）从 DB 镜像读取。避免服务端回写 git 触发 webhook 循环。

##### 分支与权限策略
- **main 分支**：团队共享资产的稳定版本，受保护，需 PR + Review 才能合入。
- **个人分支**：`member/<id>`，成员在此分支自由 commit 一级提炼产出与手动编辑。
- **同步路径**：个人分支 push → 发起 PR → Reviewer 审阅 → 合入 main → webhook 触发 DB 索引更新 → 其他人 pull main。
- **快速模式**（小团队可选）：成员直接 push main，跳过 PR。

##### 客户端封装
客户端在 git 命令之上做薄封装，降低使用门槛：
- `teamharness sync`：一键执行 pull --rebase + push（个人分支）。
- `teamharness pr`：自动从个人分支向 main 发起 PR。
- `teamharness recall`：走服务端 DB 索引召回（有网）/ 本地文件（离线降级）。
- 冲突时调用内置 diff 视图辅助解决，底层仍是 git merge。
- 不隐藏 git，高级用户可直接用 git 命令操作。

##### 私有资产处理
- `scope=private` 的资产不入中央仓库，存放于本地 `.teamharness/private/` 并加入 `.gitignore`。
- 一级提炼产出的资产默认 private，用户显式改为 team/public 后才会被 `teamharness sync` 纳入提交。
- private 资产不进入 DB 索引层（不在服务端存储），召回时仅本地匹配。

#### 3.1.4 资产分层与索引登记（借鉴分层记忆结构）

借鉴个人记忆体系的分层递归 + 索引防孤岛 + 写入时判定拆分机制，团队资产也分层组织。

##### 分层结构（可递归）
```
teamharness-shared/ (git 仓库根)
├── INDEX.md                    ← 项目级索引（全局资产 + 模块登记表）
├── rules/                      ← 全局通用资产
├── memory/
├── skills/
├── tools/
├── prompts/
└── modules/                    ← 模块级资产（按业务模块拆分）
    ├── backend/
    │   ├── INDEX.md            ← 模块级索引（本模块资产 + 子模块登记）
    │   ├── rules/
    │   ├── memory/
    │   └── submodules/         ← 子模块级（可继续递归）
    │       └── auth/
    │           └── INDEX.md
    └── frontend/
        └── INDEX.md
```

- 小团队/小项目止于项目级即可，modules/ 可为空。
- 模块数 > 5 或某模块资产 > 20 条时，建议拆分独立成层（治理建议，非强制自动执行，见 8.12）。

##### INDEX.md 规范
```yaml
# INDEX.md
level: project | module | submodule
parent: ../INDEX.md             ← 父级索引指针（根级为 null）
module: backend                 ← 本层标识（根级为项目名）
assets:                         ← 直接下属资产登记表（防孤岛）
  - id: rule-backend-lint
    path: rules/lint.md
    type: rule
    purpose: 后端 lint 规范
  - id: memory-db-decisions
    path: memory/db-decisions.md
    type: memory
    purpose: 数据库选型决策记录
submodules:                     ← 直接下级模块登记表
  - name: auth
    path: submodules/auth/
    purpose: 认证模块
counts:                         ← 拆分判定用计数（写入时维护）
  assets: 12
  submodules: 1
```

##### 防孤岛强制校验
- **新建资产必须在父级 INDEX.md 登记**：PR Review 阶段检查"资产文件存在但 INDEX.md 未登记"→ 阻断合入。
- **新建模块必须在父级 INDEX.md 登记**：同理。
- **删除资产/模块必须同步更新 INDEX.md**：PR Review 检查一致性。
- INDEX.md 本身的变更作为 PR 的一部分提交，随资产一起 Review。

##### 与 DB 索引层的协同
- webhook 同步时**先读 INDEX.md**拿到资产清单与层级结构，再读具体资产文件，不用全仓库扫描。
- DB 索引层的 asset_index 表增加 `module_path` 字段，记录资产所属层级路径，支持按模块过滤召回。
- INDEX.md 的 `counts` 字段作为治理看板数据源（8.12 节）。

### 3.2 去重与归并（Dedup & Merge）

git 处理文本级版本与冲突，服务端在此基础上做 git 不擅长的**语义级去重与归并**。

#### 3.2.1 服务端语义去重（PR Review 阶段）

成员 push 个人分支并发起 PR 后，服务端在 Review 阶段对 PR 内的新增/修改资产做语义去重：

1. **内容级去重**：`content_hash` 精确匹配，重复直接提示"与已有资产 X 完全一致"，建议撤回。
2. **语义级去重**：计算 embedding，与 main 分支同类型资产做相似度检索；超过阈值（0.92）进入"待归并"。

归并策略：
- AI 辅助合并：LLM 读取相似资产簇，抽取共性、保留差异，生成统一版本。
- 归并建议作为 PR Review 评论呈现，由 Reviewer 决定是否采纳。
- 归并后保留原资产引用（`related_to`），确保可追溯。

#### 3.2.2 为什么不在本地做去重

- git 已保证同一文件不会重复，跨文件的语义重复交给服务端统一处理更高效（集中维护向量库）。
- 本地无需维护向量库，降低客户端复杂度。
- 语义去重需要全量资产池视角，单个成员本地视角不完整。

#### 3.2.3 二级提炼的去重入口

二级提炼引擎（3.3）在扫描中央资产池时，也会触发跨成员的语义去重——这属于提炼流水线 Light 阶段的一部分，与 PR Review 阶段的去重互补：
- PR Review 去重：成员资产入库时即时检查，防重复入库。
- 提炼 Light 去重：定期全量扫描，清理历史遗留的语义重复。

### 3.2b 召回服务（Recall Service）

DB 索引层的核心能力：让 AI 按需召回资产，而非全量加载本地文件。借鉴 TAM 的 /tools/list + /tools/call 模式。

#### API 设计
```
POST /v1/recall/list
  入参：agent_id, query?, module_path?, task_type?, asset_type?, tags?
  出参：匹配资产的摘要清单（id, type, title, tags, relevance_score, git_path, module_path）
  逻辑：
    1. 若有 module_path → 先读该模块 INDEX.md 缩小候选集（+ 父级全局资产）
    2. 按 agent_binding 过滤可访问资产
    3. 若有 query → 候选集内向量检索 + BM25 + RRF；无 query → 返回装配清单

POST /v1/recall/read
  入参：agent_id, asset_id
  出参：资产完整内容 + frontmatter
  逻辑：校验 agent_binding 权限后返回，内容从 git 仓库读取（保证最新）
```

#### 检索流程（索引下钻 + 向量精排）
```
Agent 发起召回（带上下文：module? task_type?）
  │
  ▼
步骤1：索引下钻（缩小候选集）
  ├─ 上下文含 module → 读该模块 INDEX.md → 候选集 = 本模块资产 + 父级全局资产
  └─ 无上下文 → 候选集 = 全量资产（回退纯向量检索）
  │
  ▼
步骤2：权限过滤
  查 agent_binding 表过滤可访问资产
  │
  ▼
步骤3：精排
  ├─ 无 query：返回装配清单（fixed 绑定的资产）
  └─ 有 query：在候选集内向量检索 + BM25 + RRF 混合排序
  │
  ▼
Agent 选择需要的资产
  │
  ▼
/recall/read ──► 校验权限 → 从 git 读取资产内容 → 返回
```

两步检索相比纯向量检索全量资产池：候选集更小、精度更高、成本更低，尤其资产池增长后。

#### 离线降级
- **有网**：走服务端 DB 索引召回（语义检索 + 按需读取，上下文精简）。
- **离线**：降级为本地文件全量加载（coding 软件按自身逻辑读取本地记忆文件夹）；/recall/read 离线时从本地 git working copy 按 git_path 读取。
- 客户端自动检测网络状态切换模式，离线时提示"召回降级为本地模式"。

#### module_path 获取方式
召回服务的索引下钻依赖 `module_path` 缩小候选集，但 Agent 不一定知道当前任务属于哪个模块。获取方式按优先级：
1. **客户端上下文推断**：从 coding 软件当前打开的项目路径，经 mapping.yaml 反查所属模块（最常用）。
2. **用户显式指定**：客户端命令行 `teamharness recall --module backend` 显式传入。
3. **服务端 LLM 推断**：召回 API 接受任务描述，服务端用小模型推断 module_path（成本较高，作为兜底）。
4. **无 module_path**：回退纯向量检索全量资产池（精度略低但可用）。

#### 与 coding 软件的协同
- coding 软件仍按自身逻辑加载本地记忆文件夹（不干预）。
- 召回服务作为**补充通道**：coding 软件可通过 MCP/HTTP 调用召回 API，获取本地未同步的团队资产或做语义检索。
- 不强制 coding 软件接入召回 API；接入则获得语义检索能力，不接入则用本地文件。

### 3.2c Agent 装配机制（Agent Binding）

服务端维护装配表，强制控制不同角色 Agent 能访问哪些资产，而非靠 frontmatter 约定。

#### 装配模型（调度索引表 + 手动叠加）
借鉴个人记忆体系的"操作类型 → 类别 → 路径"调度索引表，建立"任务类型 → 资产类别 → 自动绑定"机制，减少人工逐条配置。

```yaml
# Agent 装配配置
agent:
  id: builder-agent-01
  role: builder

# 调度索引表（自动绑定，新资产入库按 category 自动匹配）
task_routing:
  - task_type: db-migration
    asset_categories: [rule-backend, skill-db, memory-db-decisions]
    auto_bind: true              # 该任务类型自动绑定这些类别的资产
  - task_type: api-design
    asset_categories: [rule-api, memory-api-conventions]
    auto_bind: true

# 手动绑定（固定装配，覆盖自动绑定）
manual_bindings:
  - asset_id: <rule-backend-lint>
    type: fixed                  # fixed=每次必加载；on-demand=按需召回
    priority: high
  - asset_id: <memory-project-decisions>
    type: fixed
    priority: high
```

#### 装配管理
- **自动绑定**：新资产入库时带 `category` 标签（frontmatter），服务端按调度索引表自动匹配并绑定到对应 Agent，无需人工逐条配置。
- **手动绑定**：固定装配（fixed）通过管理面板或 API 配置，写入 agent_binding 表，覆盖自动绑定。
- **装配生效**：召回服务每次请求先查 agent_binding 过滤（自动 + 手动合并），未绑定资产不可见。
- **角色模板**：预置角色模板（builder/reviewer/scout），新 Agent 按角色继承默认调度索引表，可微调。
- **装配变更**：服务端即时生效，无需客户端同步（与 git 资产更新解耦）。

#### 与 git 资产的关系
- 装配表只存引用（asset_id），资产内容仍在 git。
- git 中的资产被删除/重命名 → webhook 同步时在 agent_binding 表标记 `enabled=false, invalidated_at=now()`；告警写入治理看板并通知资产 owner。
- 调度索引表是**运行态配置，存 DB**；`.teamharness/routing.yaml` 是其**导出快照**（人可读备份，入 git 但 webhook 路由配置排除 `.teamharness/` 路径，不触发 DB 同步），用于版本追溯与灾备恢复。
- 装配表不进 git（是运行态数据，非资产），只存 DB 索引层。

#### category 受控词汇表
自动绑定按 `category` 匹配调度索引表，category 必须受控以保证一致性。

- **命名规范**：`<type>-<module>`，如 `rule-backend`、`skill-db`、`memory-api-conventions`。
- **受控词汇表**：存 `.teamharness/categories.yaml`（入 git），新 category 需 PR 登记。
- **category 与 module_path 的关系**：正交。module_path 是物理位置（如 `modules/backend`），category 是功能分类（如 `rule-backend`）。一个 module_path 下可有多个 category，一个 category 可跨 module_path 存在。
- **一致性保证**：PR Review 阶段校验资产的 category 是否在 categories.yaml 登记，未登记则阻断合入。

### 3.3 两级提炼体系（Distillation Engine）

借鉴 OpenClaw 的 Dreaming 三阶段机制（Light 浅睡收集 → REM 反思提取模式 → Deep 深睡评分固化），构建两级提炼链路：

- **一级提炼（个人 dream）**：从成员 coding 对话记录 → 提炼出结构化的个人 harness 资产（L1→L2）。运行在客户端侧。
- **二级提炼（团队 dream）**：从成员上传的资产池 → 提炼出泛化 Prompt（L2→L3）。运行在服务端侧。

两级均采用三阶段流水线，但输入输出与目标不同。

#### 3.3.1 三层资产对应关系
| OpenClaw（个人） | TeamHarness（团队） | 说明 |
|---|---|---|
| L1 瞬时记忆 | coding 对话记录 | 成员与 coding 软件的原始会话历史，未结构化 |
| L2 每日日志 | 个人/中央 harness 资产 | 一级提炼产出的 rules/memory/skills/tools，带时间衰减 |
| L3 MEMORY.md | 泛化 Prompt 共享池 | 二级提炼产出的可复用 Prompt，团队级 SSOT |

#### 3.3.2 一级提炼：个人 dream（对话记录 → harness 资产）

运行在客户端侧。数据源是 coding 软件的对话记录（会话历史）。

**数据采集**
- 客户端通过对话记录适配器读取 coding 软件的会话存储（如 Trae 的 sessions/*.jsonl）。
- 不同 coding 软件会话格式不同，通过 SessionProvider 抽象屏蔽差异（见 4.3）。
- 采集范围：已完成（非进行中）的会话；按时间增量采集。

**三阶段流程**
```
coding 对话记录（L1）
      │
      ▼
 ① Light 浅睡 —— 信号筛选（不产出资产）
   • 扫描会话，过滤纯闲聊/问候/无信息轮次
   • 抽取含决策、约束、踩坑、经验、工具使用的轮次
   • 标注候选类型：rule / memory / skill / tool
   • 写入本地 .dreams/light/
      │
      ▼
 ② REM 反思 —— 意图归纳（不产出资产）
   • 对候选轮次做意图归纳："用户实际想沉淀的是什么？"
   • 识别重复出现的模式（如多次纠正 AI 同一错误 → 规则）
   • 区分"一次性任务上下文" vs "可复用经验"
   • 写入本地 .dreams/rem/
      │
      ▼
 ③ Deep 深睡 —— 结构化固化（产出资产）
   • 评分：频率、跨会话复现度、可复用性、明确性
   • 过阈值才调用 LLM 提炼为结构化资产
   • 产出带 frontmatter 的资产文件，写入本地记忆文件夹
   • 人工确认关口（默认开启，用户可改为自动）
      │
      ▼
 ④ 草稿入池
   • 提炼出的个人资产进入本地 L2，等待用户 push 到中央
```

**个人提炼评分（Deep 阶段）**
| 维度 | 含义 | 说明 |
|------|------|------|
| 频率 | 同类信号在会话中出现次数 | 多次出现更值得沉淀 |
| 跨会话复现 | 是否在不同会话中重复出现 | 一次性场景不沉淀 |
| 可复用性 | 能否在未来的 coding 中复用 | 纯项目特定上下文不沉淀 |
| 明确性 | 能否表述为清晰可执行的条目 | 模糊感悟需用户确认 |
| 类型适配 | 是否能明确归类为 rule/memory/skill/tool | 无法归类则丢弃 |

**个人提炼 Prompt 设计要点**
- 任务是"从对话中提炼经验"，不是"总结对话"——聚焦可复用知识，非流水账。
- 先标注候选轮次再提炼，避免对整段对话做摘要导致信息稀释。
- 显式区分四类资产目标，分别用不同子 prompt 提炼：
  - rule 提炼："用户反复强调的约束/规范是什么？"
  - memory 提炼："用户提及的项目事实/决策是什么？"
  - skill 提炼："用户展示了什么可复用的操作流程？"
  - tool 提炼："用户使用了什么工具/脚本，且对其效果认可？"
- SKIP 机制：纯闲聊、一次性调试细节、无明确结论的讨论不产出。

#### 3.3.3 二级提炼：团队 dream（资产池 → 泛化 Prompt）

运行在服务端侧。输入是成员上传的 harness 资产，输出是泛化 Prompt。

```
中央资产池（团队 L2）
      │
      ▼
 ① Light 浅睡 —— 收集与去重（不产出 Prompt）
   • 扫描资产池，按 type+tags 聚类
   • 内容级 + 语义级去重
   • 统计每簇的频次、来源成员数、时间跨度
   • 写入 .dreams/light/ 暂存区
      │
      ▼
 ② REM 反思 —— 模式识别（不产出 Prompt）
   • 跨成员识别重复出现的模式与共性主题
   • LLM 分析：这些资产是否共享同一"底层意图"？
   • 标记"可泛化候选簇" vs "一次性/个人偏好簇"
   • 写入 .dreams/rem/ 候选区
      │
      ▼
 ③ Deep 深睡 —— 评分固化（产出 Prompt）
   • 六维评分（见 3.3.4）
   • 过阈值才触发 LLM 提炼
   • 提炼后写入 prompts/distilled/ + DREAMS.md
   • 人工 Review 关口（可选）
      │
      ▼
 ④ 发布 + 反馈回流
   • 采纳率 / 修改率回流，驱动下一轮迭代
```

#### 3.3.4 六维评分（二级 Deep 阶段门禁）

借鉴 OpenClaw 六维评分，适配团队泛化场景：

| 维度 | 含义 | 权重 | 说明 |
|------|------|------|------|
| 频率 | 同类资产出现次数 | 中 | 越高越可能值得提炼 |
| 来源多样性 | 来自不同成员的数量 | 高 | ≥3 个不同成员才允许晋升，防个人偏好污染 |
| 泛化性 | 跨场景/技术栈适用度 | 高 | LLM 评估，剥离具体项目后是否仍成立 |
| 稳定性 | 时间跨度内一致性 | 中 | 跨越一定时间窗仍被引用，非一次性热点 |
| 可操作性 | 能否直接指导行动 | 中 | 过于抽象、无法落地的拒绝 |
| 信噪比 | 共性 vs 噪声占比 | 中 | 簇内差异过大、无法收敛的拒绝 |

**晋升门禁**（全部满足才进入提炼）：
- 总分 ≥ 0.8
- 来源多样性 ≥ 3 个不同成员
- 泛化性单项 ≥ 0.7
- 被召回次数 ≥ 3 次

#### 3.3.5 二级提炼 Prompt 工程（核心）

> 目标：指导 AI 做**有意义的泛化提炼**，避免无意义提炼（把一次性事件当规律、把个人偏好当规范、过度抽象到失去可操作性）。

##### 设计原则（结合模型思维习惯）
1. **先具体后抽象**：模型从实例归纳模式比直接抽象更可靠 → 先呈现原始资产全文，再要求归纳。
2. **先发散后收敛**：先让模型列出"所有可能的共性"，再强制筛选保留高置信度项。
3. **显式推理链**：要求模型输出"为什么认为这是泛化的"推理过程，而非直接给结论。
4. **主动反例检验**：让模型自己寻找"这个 Prompt 在什么场景下不适用"，倒逼边界明确。
5. **去个性化**：显式要求剥离项目名、人名、特定技术栈细节，保留结构化逻辑。
6. **区分事实与偏好**：明确区分"客观约束"（如"提交前跑 lint"）与"主观偏好"（如"我喜欢用 TypeScript"），仅对前者泛化。

##### 提炼 Prompt 模板（伪代码）

```
你是一个团队经验提炼专家。下面是来自 {N} 名成员的 {M} 条同类 {type} 资产。
你的任务：判断它们是否共享可泛化的底层模式，若是则提炼为一个泛化 Prompt。

【输入资产】
{逐条列出资产内容 + owner + tags + 来源时间}

【工作步骤】（请严格按顺序执行，并输出每步的推理）

Step 1 — 事实标注
  逐条标注：这是"客观约束"还是"主观偏好"？是否绑定特定项目/技术栈？
  若 >30% 是主观偏好且无客观内核 → 输出 SKIP，理由：偏好不可泛化。

Step 2 — 共性发散
  列出所有可能的共性点（不评价质量，尽量多发散）。

Step 3 — 共性收敛
  对每个共性点评估：
   - 置信度（多少条资产支持？是否来自不同成员？）
   - 是否存在反例（某条资产明确反对该共性？）
  保留置信度高且无强反例的共性。

Step 4 — 去个性化
  将保留的共性改写为不依赖特定项目/人名/技术栈的表述。
  保留必要的结构化逻辑（如"提交前检查 X"而非"提交前跑 eslint"）。

Step 5 — 反例自检
  主动构造 2-3 个该 Prompt 不适用的场景，说明边界。

Step 6 — 输出
  若 Step 3 保留的共性 < 2 条 → 输出 SKIP，理由：共性不足，不值得提炼。
  否则输出：
    - distilled_prompt: 提炼后的泛化 Prompt
    - rationale: 为什么值得提炼（引用 Step 3 结论）
    - applicability: 适用场景
    - boundaries: 不适用场景（来自 Step 5）
    - source_refs: 引用的原始资产 ID
    - confidence: 0-1
```

##### 跳过机制（防无意义提炼）
以下情况强制 SKIP，不产出 Prompt：
- 主观偏好占比过高且无客观内核
- 共性点不足 2 条
- 存在强反例且无法调和
- 簇内资产语义方差过大（向量距离均值超阈值，说明并非真同类）
- 来源单一（< 3 个成员），除非显式标记为"团队级约定"

##### 反向验证
- 维护一组历史样例（已知优质 Prompt + 已知劣质 Prompt）。
- 新 Prompt 在样例上回放评分，必须不低于基线才允许发布。
- 评分维度：泛化性、完整度、无歧义性、可操作性。

#### 3.3.6 质量保障与防退化
- **回归基线**：新 Prompt 必须不低于基线 Prompt 的得分。
- **采纳率反馈**：发布后跟踪被 pull/引用次数与修改率；采纳率连续下降则自动降级。
- **防噪声放大**：已发布的 Prompt 不直接作为下一轮提炼的输入，避免"提炼→发布→再提炼"循环。
- **版本回滚**：一键回退到上一可用版本。
- **DREAMS.md 审查界面**：每次 Deep 阶段产出写入 DREAMS.md，供人工审阅与追溯。

### 3.4 Git 化版本管理

#### 3.4.1 仓库结构（分层 + 索引登记）

合并 3.1.4 的分层结构，以 3.1.4 为权威，此处补全 `.teamharness/` 与 `restricted/` 目录。
```
teamharness-shared/
├── INDEX.md                    ← 项目级索引（全局资产 + 模块登记表，见 3.1.4）
├── rules/                      ← 全局通用资产
├── memory/
├── skills/
├── tools/
├── prompts/
│   └── distilled/              # 提炼后的泛化 Prompt
├── restricted/                 ← restricted scope 资产（CODEOWNERS 路径级保护）
│   └── <module_path>/
├── modules/                    ← 模块级资产（按业务模块拆分，见 3.1.4）
│   └── <module>/
│       ├── INDEX.md
│       └── submodules/<sub>/INDEX.md
├── DREAMS.md                   # 提炼审查日记（人类可读，入 git）
└── .teamharness/               # 运行态配置与本地缓存（见下表）
    ├── categories.yaml         # category 受控词汇表（入 git，PR 登记）
    ├── routing.yaml            # 调度索引表导出快照（入 git，不触发 DB 同步）
    ├── hooks.yaml              # webhook/CI 配置（入 git）
    ├── manifest.json           # 客户端本地缓存索引（不入 git，从 INDEX.md+资产派生）
    └── private/                # 客户端私有资产（不入 git，.gitignore，镜像分层结构）
```

| 文件/目录 | 用途 | 是否入 git | 维护方 |
|---|---|---|---|
| INDEX.md | 分层登记表（防孤岛+召回下钻） | 是 | 人（PR 维护） |
| categories.yaml | category 受控词汇表 | 是 | 人（PR 登记） |
| routing.yaml | 调度索引表导出快照 | 是 | 服务端导出 |
| hooks.yaml | webhook/CI 配置 | 是 | 人 |
| manifest.json | 客户端本地缓存索引 | 否 | 客户端派生 |
| private/ | 客户端私有资产 | 否 | 客户端 |

> 注：一级提炼的 `.dreams/` 暂存区在客户端本地 `.teamharness-local/dreams/`（不入 git，纯临时）；二级提炼的 `.dreams/` 在服务端 `/var/teamharness/dreams/`（不入 git，纯临时）。只有 DREAMS.md 入 git（人类可读审查日记）。

#### 3.4.2 约定
- 服务端为仓库托管方，成员通过 git（底层经 Git Provider 抽象）访问。
- 元数据（owner、评分、引用数）存于 PostgreSQL（DB 派生索引层），通过 `asset_id` 与 Git 文件关联。
- **钩子流水线**：push 触发 webhook → 结构校验（防孤岛/category）→ 语义去重（PR Review）→ 合入 main 触发 DB 索引同步 → 触发提炼任务。
- 重要变更（如 prompts 发布）走 PR + Review。

### 3.5 客户端（TeamHarness Client）

定位：本地记忆文件夹的管家 + 与控制端交互的桥梁。

#### 3.5.1 职责
1. **本地读写**：管理本地记忆文件夹（= git working copy），支持 rules/memory/skills/tools 的增删改查。
2. **与 coding 软件协同**：本地文件夹即 coding 软件（如 Trae）的记忆源，客户端只维护文件，不干预 coding 软件内部逻辑；通过 mapping.yaml 目录映射适配不同 coding 软件。
3. **对话记录采集**：经 SessionProvider 读取 coding 软件会话存储，增量采集，供一级提炼使用。
4. **一级提炼（个人 dream）**：客户端本地运行 Light/REM/Deep 三阶段，产出资产（默认 private）写入本地文件夹。
5. **git 同步封装**：`sync`（pull --rebase + push 个人分支）、`pr`（发起 PR）、冲突 diff 辅助，底层是 git。
6. **召回客户端**：调用服务端 `/v1/recall/list` + `/v1/recall/read` API（有网）/ 离线降级本地文件检索；提供 module_path 上下文推断。
7. **配置管理**：服务端地址、API Key、本地路径、mapping.yaml、同步策略（手动/自动）。

#### 3.5.2 本地文件夹结构指引

**核心原则**：文件夹的物理布局服从 coding 软件的硬性调用逻辑（它要求放哪就放哪、要求什么命名就用什么命名），但 TeamHarness 在其上叠加自己的检索流程与资产布置规范。

##### 两层适配模型
```
┌─────────────────────────────────────────────┐
│  TeamHarness 逻辑层（软性规范，跨软件统一）    │
│  ├─ 资产分类：rules / memory / skills / tools│
│  ├─ 命名约定：kebab-case + 前缀              │
│  ├─ 元数据头：frontmatter（id/owner/tags）   │
│  └─ 索引文件：.teamharness/manifest.json     │
└──────────────────┬──────────────────────────┘
                   │ 目录映射配置（mapping.yaml）
                   ▼
┌─────────────────────────────────────────────┐
│  coding 软件物理层（硬性约束，各软件不同）     │
│  Trae:  .trae-cn/memory/...                 │
│  OpenClaw: ~/.openclaw/workspace/...        │
│  自定义: <任意路径>                          │
└─────────────────────────────────────────────┘
```

##### 目录映射配置示例
```yaml
# .teamharness/mapping.yaml
target: trae              # 目标 coding 软件
root: .trae-cn/memory     # 物理根目录（硬性约束）
layout:
  rules:   rules/         # 逻辑分类 → 物理子目录
  memory:  memory/
  skills:  skills/
  tools:   tools/
naming:
  convention: kebab-case
  prefix:
    rule:  "rule-"
    memory: "mem-"
index: .teamharness/manifest.json   # TeamHarness 自维护索引
```

##### 资产文件格式（统一 frontmatter）
```markdown
---
id: <uuid>
type: rule | memory | skill | tool | prompt
owner: <member_id>
scope: private | team | restricted | public
tags: [backend, lint]
version: 1.0.0
related_to: []               # 相似但独立的关联资产 id（服务端归并标记）
---

# 规则标题

规则正文...
```

##### 检索流程指引（客户端侧）
1. **启动加载**：coding 软件按自身逻辑加载记忆文件，TeamHarness 不干预。
2. **变更检测**：客户端基于 git status 检测本地未提交变更，提示用户 commit。
3. **冲突处理**：git pull --rebase 产生冲突时，客户端调用内置 diff 视图辅助解决。
4. **只追加优先**：对 coding 软件已维护的索引/缓存文件，TeamHarness 只追加不重写，避免破坏其内部状态。

#### 3.5.3 形态
- 轻量 CLI / 常驻守护进程二选一或兼具。
- 一级提炼运行在客户端，需调用 LLM（通过 LLM Provider 抽象，可直连或走服务端代理）。
- 二级提炼运行在服务端。

---

## 4. Provider 抽象层

为兼容多种部署环境，关键依赖均做 Provider 抽象，通过配置切换。

### 4.1 Git Provider

抽象接口（伪代码）：
```python
class GitProvider(Protocol):
    def create_commit(self, path, content, message) -> CommitRef
    def read_file(self, path, ref?) -> bytes
    def list_dir(self, path, ref?) -> list[Entry]
    def create_pr(self, branch, target, title) -> PRRef
    def get_pr_status(self, pr_id) -> PRStatus
```

实现：
- `GitLabProvider`：基于 GitLab API v4。
- `GiteaProvider`：基于 Gitea API。
- 通过 `GIT_PROVIDER` 环境变量 + 配置项切换，服务端启动时加载对应实现。

### 4.2 LLM Provider

抽象接口：
```python
class LLMProvider(Protocol):
    def chat(self, messages, *, model, temperature, max_tokens) -> Response
    def embed(self, texts, *, model) -> list[Vector]
```

实现：
- 兼容 **OpenAI API 格式**（覆盖 OpenAI / DeepSeek / Moonshot / 通义千问兼容模式 / 本地 vLLM 等）。
- 通过 `LLM_BASE_URL` + `LLM_API_KEY` + `LLM_MODEL` 配置。
- 提炼 / 归并 / 评分均通过此抽象调用，便于切换模型。
- 客户端一级提炼与服务端二级提炼共用同一抽象；客户端可直连 LLM，也可通过服务端代理调用（便于统一计费与密钥管理）。

### 4.3 Session Provider（对话记录适配）

用于一级提炼，适配不同 coding 软件的会话存储格式。

抽象接口：
```python
class SessionProvider(Protocol):
    def list_sessions(self, since?: timestamp) -> list[SessionMeta]
    def read_session(self, session_id) -> Session
    def is_completed(self, session_id) -> bool
```

实现：
- `TraeSessionProvider`：读取 `.trae-cn` 下的会话存储。
- `CursorSessionProvider`：读取 Cursor 的会话存储。
- `GenericJsonlProvider`：读取通用 jsonl 格式会话（兜底）。
- 通过客户端 `mapping.yaml` 的 `session.target` 配置切换。

适配要点：
- 会话存储路径与格式因软件而异，需各自实现解析逻辑。
- 统一输出为内部 `Session` 结构（含轮次、角色、时间戳、工具调用记录）。
- 增量采集：基于 `since` 时间戳，只读取新会话。
- 隐私：只在本机读取与提炼，不上传原始对话内容，仅上传提炼后的资产。

---

## 5. 数据模型（核心表）

> 全部为 DB 派生索引层表，从 git 仓库派生，可随时重建。

```sql
-- 资产索引（从 git frontmatter + 文件内容派生）
asset_index(
  id, type, owner, scope, content_hash, embedding_id,
  version, tags, git_path, git_commit,        -- git 追溯
  module_path, category,                      -- 组织层级 + 功能分类（召回下钻与自动绑定）
  related_to, created_at, updated_at, indexed_at
)
CREATE INDEX idx_asset_module ON asset_index(module_path);
CREATE INDEX idx_asset_category ON asset_index(category);

-- 向量索引（embedding 存储）
asset_embedding(id, asset_id, embedding, model_version)

-- Agent 装配表（哪个 Agent 装备哪些资产，服务端强制）
agent_binding(
  id, agent_id, agent_role,                   -- 如 builder/reviewer
  asset_id, binding_type,                     -- fixed/on-demand
  priority, enabled, created_at
)

-- 一级提炼任务（个人侧，客户端产生，可选上报元数据）
personal_distillation(id, member_id, session_ids[], output_asset_ids[], status, score, created_at)

-- 二级提炼任务（团队侧，服务端执行）
distillation_job(id, input_asset_ids[], output_prompt_id, status, score, created_at)

-- 泛化 Prompt
prompt(id, distilled_from[], content, version, confidence, adoption_rate, status)

-- DB 索引同步状态（追踪与 git 的同步水位）
index_sync_state(id, last_synced_commit, last_sync_at, status)

-- 模块统计镜像（从 git INDEX.md counts 派生，治理看板数据源）
module_stats(module_path, asset_count, submodule_count, last_synced_at)

-- 召回日志（二级提炼晋升门禁"被召回次数"数据源）
recall_log(asset_id, agent_id, recalled_at)
```

---

## 6. 技术选型（建议）

| 层 | 选型 | 说明 |
|----|------|------|
| 服务端 | Python + FastAPI | 生态适合 LLM/向量操作 |
| Git 托管 | GitLab / Gitea（可切换） | Provider 抽象，SSOT 层 |
| 元数据 | PostgreSQL | DB 派生索引层，事务、关系 |
| 向量库 | Qdrant 或 PGVector | DB 派生索引层，语义去重/检索 |
| LLM | OpenAI 兼容格式 | Provider 抽象，可切外调/本地 |
| 客户端 | 轻量 CLI / 守护进程 | 本地文件夹读写 + git sync + 召回客户端 |
| 钩子 | GitLab Webhook / Gitea Hook | push/merge 触发 DB 索引同步与提炼 |
| 召回服务 | FastAPI + 向量库 | /v1/recall/list + /v1/recall/read |
| Agent 装配 | 服务端管理面板 / API | agent_binding 表配置 |

---

## 7. 关键流程时序

### 7.1 资产入库与同步（git SSOT + webhook 派生 DB）
```
Client                              Git Provider         Asset Service(Review)    DB 索引层
  │ 本地 commit + push 个人分支       │                      │                        │
  ├─────────────────────────────────►│                      │                        │
  │                                  │ 触发 PR              │                        │
  │                                  ├─────────────────────►│                        │
  │                                  │                      │ PR Review:             │
  │                                  │                      │  · 防孤岛校验(INDEX.md) │
  │                                  │                      │  · 语义去重(查DB索引)   │
  │                                  │                      │  · category 校验       │
  │                                  │◄─────────────────────┤ Review 评论/阻断       │
  │ Reviewer 审批合入 main            │                      │                        │
  ├─────────────────────────────────►│ merge to main        │                        │
  │                                  │ webhook              │                        │
  │                                  ├──────────────────────┼───────────────────────►│
  │                                  │                      │  fetch main + diff     │
  │                                  │                      │  读 INDEX.md 清单      │
  │                                  │                      │  计算 embedding        │
  │                                  │                      │  更新 asset_index      │
  │                                  │                      │  自动绑定 agent_binding│
  │                                  │                      │  更新 module_stats     │
```

### 7.2 召回流程（索引下钻 + 权限过滤 + 精排）
```
Agent/Client                Recall Service           DB 索引层           Git Repo
  │ /recall/list(module_path,query)    │                │                   │
  ├───────────────────────────────────►│                │                   │
  │                                    │ 读 INDEX.md    │                   │
  │                                    │ 缩小候选集     │                   │
  │                                    ├───────────────►│ 查 agent_binding  │
  │                                    │◄───────────────┤ 权限过滤          │
  │                                    ├───────────────►│ 向量+BM25 精排    │
  │                                    │◄───────────────┤ Top-K             │
  │◄───────────────────────────────────┤ 返回摘要清单   │                   │
  │ /recall/read(asset_id)             │                │                   │
  ├───────────────────────────────────►│                │                   │
  │                                    ├────────────────┼──────────────────►│
  │                                    │◄───────────────┼───────────────────┤ 资产内容
  │◄───────────────────────────────────┤ 返回内容       │                   │
  │                                    │ 写 recall_log  │                   │
  │                                    ├───────────────►│ (晋升门禁数据源)  │
```

### 7.3 一级提炼（客户端本地，对话→资产）
```
SessionProvider      Distillation(Client)      本地记忆文件夹(git working copy)
  │ 读 sessions/*.jsonl  │                          │
  ├────────────────────►│ Light: 信号筛选          │
  │                      │ REM: 意图归纳            │
  │                      │ Deep: 评分固化           │
  │                      │ 产出资产(默认 private)   │
  │                      ├─────────────────────────►│ 写入 + git add/commit
  │                      │                          │ (用户改为 team/public 后 sync)
```

### 7.4 二级提炼（服务端，资产池→泛化 Prompt）
```
Distillation Engine        DB 索引层              LLM Provider          Git Repo
  │ 扫描 asset_index          │                      │                     │
  ├─────────────────────────►│ 按类别聚类            │                     │
  │◄─────────────────────────┤ 候选簇               │                     │
  │ Light: 内容+语义去重      │                      │                     │
  │ REM: 跨成员模式识别       │                      │                     │
  │ Deep: 六维评分            │                      │                     │
  │  过阈值 → 调用 LLM 提炼   │                      │                     │
  ├─────────────────────────────────────────────────►│ 提炼泛化 Prompt     │
  │◄─────────────────────────────────────────────────┤ distilled_prompt    │
  │ 反向验证 + 采纳率统计     │                      │                     │
  │ 发布：回写 prompts/       │                      │                     │
  ├──────────────────────────────────────────────────┼────────────────────►│ commit + push
  │                            │ webhook 触发 DB 同步 │                     │
```

### 7.5 DB 索引层故障降级
```
Recall Service              向量库/PG               Git Repo
  │ 召回请求                  │ 挂了                  │
  │                            │ ✗ 连接失败           │
  │ 降级：git 路径遍历 + BM25 关键词检索              │
  ├──────────────────────────────────────────────────►│
  │◄──────────────────────────────────────────────────┤ 结果(无向量排序)
  │ 返回降级标记 + 结果       │                       │
  │ 故障恢复后从 index_sync_state.last_synced_commit 增量补同步
```

---

## 8. 需要重点考虑的点

### 8.1 权限与可见性（双轨模型）

折中方案下，权限分两层：
- **写权限（git 层）**：分支保护 + CODEOWNERS + PR Review，控制谁能改资产。
- **读权限（DB 索引层）**：agent_binding 表 + scope 校验，控制哪个 Agent 能召回哪些资产。

| scope | git 层（写/读） | DB 索引层（召回） |
|---|---|---|
| private | 本地 `.gitignore`，不入中央仓库 | 不进 DB 索引，仅本地匹配 |
| team | 主仓库 main，团队成员可读可 PR | agent_binding 控制哪些 Agent 可召回 |
| public | 主仓库 main，组织级可读 | 同 team，但 binding 可跨团队 |
| restricted | 主仓库 restricted/ 路径 + CODEOWNERS | agent_binding 精确到角色/Agent |

- 敏感记忆（含密钥、内部信息）需要**脱敏入库**，原值不入 Git。
- 审计日志：git history（资产变更）+ 服务端日志（召回访问）。
- **Agent 装配强制力**：召回服务每次请求查 agent_binding，未绑定资产不可见，不靠 frontmatter 约定。

### 8.2 隐私与合规
- 成员个人记忆默认私有，提炼进共享池前需 owner 确认或自动脱敏。
- **一级提炼的对话记录不离开本机**：客户端只上传提炼后的结构化资产，原始对话内容不上传，保护成员隐私。
- 一级提炼产出的资产默认 `scope=private`，由用户主动决定是否提升为 team/public 再 push。

### 8.3 冲突与回滚
- **文本冲突**：git merge/rebase 的标准冲突，客户端提供 diff 视图辅助解决，高级用户可直接用 git 工具。
- **语义归并冲突**：PR Review 阶段服务端识别的语义重复，作为 Review 评论呈现，由 Reviewer 决定。
- **回滚**：git revert/reset 天然支持，Prompt 版本可一键回退到历史 commit。
- AI 自动归并可能引入语义错误 → 重要归并仍需人工 Review 关口。

### 8.4 Prompt 质量与防退化
- 提炼不是越多越好，需要**质量门禁**（六维评分阈值 + 晋升门禁 + 回归基线）。
- **意义性保障**：提炼 Prompt 模板内置 SKIP 机制（偏好占比过高、共性不足、强反例、来源单一、语义方差过大均跳过），避免无意义提炼。
- 采纳率/修改率作为反馈信号，低效 Prompt 自动降级。
- 防止"提炼→发布→再提炼"的噪声放大循环：已发布 Prompt 不作为下一轮输入。
- 三阶段流水线中 Light/REM 不产出，只有 Deep 过阈值才产出，从流程上抑制过度提炼。

### 8.5 同步与离线
- **同步全部交给 git**：pull/push/rebase/merge 即标准 git 流程，无死循环风险（git 天然区分本地 commit 与远端 commit）。
- **离线工作**：本地照常 commit，恢复网络后 `git pull --rebase && git push`，git 处理增量与冲突。
- **语义去重集中在服务端**：本地不做语义去重，降低客户端复杂度；PR Review 与提炼 Light 阶段双层把关。
- **私有资产隔离**：`scope=private` 资产放 `.teamharness/private/` 并加入 `.gitignore`，不进中央仓库。

### 8.6 成本控制
- LLM 调用（一级个人提炼 + 二级团队提炼 + 归并 + 评分）是主要成本。
- 一级提炼：增量采集只处理新会话；Light 阶段先用规则过滤（关键词/轮次长度），减少进入 LLM 的量。
- 二级提炼：相似度命中才触发；批量处理；小模型做初筛，大模型做精炼。
- 向量计算缓存，避免重复 embedding。
- 客户端一级提炼可配置调度（如空闲时运行、每日限额）。

### 8.7 Provider 兼容性
- GitLab 与 Gitea API 行为差异（PR 模型、Webhook 字段、鉴权方式）需在 Provider 实现内屏蔽。
- LLM 不同供应商的 OpenAI 兼容度参差，需做特性探测与降级（如不支持 function calling 时回退）。
- 配置项要支持热更新或至少免重启切换，便于适配不同工地环境。

### 8.8 本地文件夹兼容
- 不同 coding 软件的记忆目录结构与命名约定不同（如 Trae 的 `.trae-cn/memory`）。
- 客户端需提供**目录映射配置**，支持将共享资产按目标软件的目录约定写入。
- 避免破坏 coding 软件已有的索引/缓存格式，必要时只追加不重写。

### 8.9 可观测性
- 资产流通、两级提炼质量需可视化 Dashboard。
- 关键指标：一级提炼产出率（对话→资产转化比）、资产去重率、二级提炼 SKIP 率、Prompt 采纳率、同步成功率、平均同步时长。

### 8.10 治理与生命周期
- 资产过期/失效机制：长期未被引用的规则应进入归档。
- Owner 变更/离职后的资产接管流程。

### 8.11 安全与 API 鉴权
- Tool 上传需沙箱校验，防止恶意脚本进入共享池。
- Git 仓库访问鉴权，防止越权拉取。
- **召回 API 鉴权**：Agent 通过 API Key 调用 `/v1/recall/*`，API Key 由服务端颁发且可轮换；`agent_id` 从 API Key 反查而非入参传递，防止伪造。
- **webhook 鉴权**：GitLab/Gitea webhook 用 secret token 校验签名，服务端拒绝未签名请求。
- **客户端 git 鉴权**：经 Git Provider 的 OAuth/Token（4.1 已隐含），不存储明文密码。

### 8.12 可靠性与并发
- **webhook 幂等**：以 commit SHA 为幂等键，同一 commit 多次触发只处理一次（GitLab/Gitea 会重试）。
- **提炼 job 并发控制**：二级提炼 job 对同一资产簇加分布式锁（`asset_cluster_id`），避免多 job 重复提炼。
- **LLM 调用重试与熔断**：一级/二级提炼调用 LLM 失败时指数退避重试 3 次；连续失败触发熔断，job 标记为 `failed` 待人工介入。
- **装配表失效清理**：webhook 同步检测 git diff 删除项，批量标记 `agent_binding.enabled=false`；周期任务清理长期失效绑定并告警 owner。
- **DB 索引层故障降级**：向量库不可用时召回降级为 git 路径遍历 + BM25（见 7.5）；PostgreSQL 故障时召回服务 503，客户端回退本地文件；恢复后从 `index_sync_state.last_synced_commit` 增量补同步。

### 8.13 资产池治理（借鉴分层记忆拆分判定）

借鉴个人记忆体系的"写入时判定拆分"机制，对团队资产池做结构治理。

#### 拆分信号与阈值（基于 INDEX.md counts）
| 触发信号 | 阈值 | 建议动作 |
|---|---|---|
| 项目内模块数 > 5 | INDEX.md submodules > 5 | 建议按业务模块独立成层（modules/ 下建子目录 + INDEX.md） |
| 单模块资产数 > 20 | 模块 INDEX.md assets > 20 | 建议该模块拆分子模块 |
| 模块边界清晰可独立 | 软信号（需判断） | 建议下沉为独立模块层 |

#### 判定时机
- **写入时快照判定**：webhook 同步时基于 INDEX.md counts 判定，不依赖历史对比。
- **判定结果只建议不自动执行**：拆分是人的决策，避免并发冲突；结果写入治理看板。
- **外部触发回顾**：用户要求或定期治理复盘时，全项目统一检查。

#### 治理看板
Dashboard 展示：
- 各模块资产数 / 子模块数 / 拆分建议
- 未登记资产告警（防孤岛兜底）
- 长期未引用资产（归档建议，与 8.10 联动）
- 召回命中率（按模块统计，识别"资产多但命中率低"的模块，可能需要重组）

---

## 8.13 与 TencentDB-Agent-Memory（TAM）的对比与借鉴

> 真实仓库地址：`https://github.com/Tencent/TencentDB-Agent-Memory`（org 是 `Tencent`；`TencentCloud/TencentDB-Agent-Memory` 是 404）。以下分析基于真实 README，非二手文章。

TAM 是腾讯云开源的团队级 Agent 记忆中枢（MIT, TypeScript），三服务架构（memory-core + memory-hub + proxy），核心是把对话/文档/代码转为四类记忆资产，通过 Memory Hub 统一登记、按 ACL 装配给 Agent，跨框架可迁移。

#### 结论：借鉴设计，不直接基于它开发

折中方案（git SSOT + DB 派生索引）吸收了 TAM 的召回/装配能力，同时保留 git 的协作/离线/审计优势。

| 维度 | TAM（真实 README） | TeamHarness（折中方案） | 差异 |
|------|-----|-------------|------|
| SSOT | Memory Hub（DB 中心化三服务） | Git 仓库 + DB 派生索引 | TAM 的 DB 是权威；TeamHarness 的 DB 是派生物可重建 |
| 核心能力 | 记忆持久化 + 按需召回 + Agent 装配 | **两级提炼 + 泛化 Prompt + 按需召回 + Agent 装配** | TeamHarness 多了泛化提炼产出 |
| 分层目的 | L0→L3 是压缩召回的最终产物 | 分层是提炼中间态，终点是泛化 Prompt | TAM 的 L3 Persona 是终点；TeamHarness 的 L2 资产是起点 |
| 资产类型 | Chat Memory / Skill / Wiki / CodeGraph | rule / memory / skill / tool / prompt | TAM 含 Wiki/CodeGraph；TeamHarness 含 rule/tool/prompt |
| 同步 | 中心化服务，无 git 版本流 | git push/pull/PR + webhook 同步 DB | TeamHarness 有人协作的 PR Review 闭环 |
| 召回 | /v3/tools/list + /v3/tools/call | /v1/recall/list + /v1/recall/read | **已借鉴 TAM** |
| Agent 装配 | Memory Hub Fixed Binding + ACL | agent_binding 表 + 角色模板 | **已借鉴 TAM** |
| 离线 | 受限 | git working copy + 召回降级本地 | TeamHarness 离线可用 |
| 可重建 | DB 是权威，不可重建 | DB 从 git 重建 | TeamHarness 的 DB 故障不影响协作 |

#### 已借鉴的设计点（折中方案采纳）

1. **按需召回 API**：借鉴 TAM 的 /v3/tools/list + /v3/tools/call，实现 /v1/recall/list + /v1/recall/read（3.2b 节）。
2. **Agent 装配机制**：借鉴 TAM 的 Fixed Binding + ACL，实现 agent_binding 表 + 角色模板（3.2c 节）。
3. **L0-L3 分层作为提炼中间态**：一级提炼 Light 阶段借鉴 L0→L1 原子事实抽取。
4. **四级可见性**：private / team / restricted / public，restricted 用 CODEOWNERS + agent_binding 实现（8.1 节）。
5. **Owner 自动管理权限**：资产 Owner 自动拥有管理权限。
6. **冷启动导入**：新成员首次 pull 时一键导入团队资产概览。
7. **Skill 结构化定义**：借鉴 Hermes Agent 的版本/资源/触发边界/执行步骤/验证规则。

#### 不借鉴的部分

- **DB 作为权威 SSOT**：TeamHarness 的 DB 是派生索引，可从 git 重建，不是权威源。
- **L0-L3 作为最终产物**：TeamHarness 的分层是提炼中间态，终点是泛化 Prompt。
- **Wiki / CodeGraph 资产**：TeamHarness 只管 harness 资产，不涉及代码细节与文档结构化。
- **手动资产绑定为主**：TeamHarness 通过 git PR + 提炼流水线自动化，装配表是运行态配置非手动逐条绑定。

---

## 9. 里程碑建议（粗粒度）

1. **M1 共享区域 MVP**：资产 schema（含 module_path/category）+ 分层仓库结构 + INDEX.md 规范 + 防孤岛 PR 校验（CI）+ Git Provider 抽象 + 客户端 git 同步封装（sync/pr/基础 diff）+ 分支与权限策略 + 私有资产隔离（.gitignore）。
2. **M2 DB 派生索引层 + 召回服务 + 基础装配**：webhook 同步 + PostgreSQL 元数据 + 向量库 embedding + /v1/recall/list + /v1/recall/read + 离线降级 + agent_binding 表 + 基础角色模板（builder/reviewer/scout）+ API 鉴权。
3. **M3 客户端一级提炼**：SessionProvider 抽象（Trae/Cursor 适配）+ 对话记录采集 + 个人 dream 三阶段 + 个人提炼 Prompt + LLM Provider 接入 + 本地文件夹读写 + frontmatter 规范 + mapping.yaml 目录映射配置。
4. **M4 二级提炼引擎 + 服务端语义去重**：PR Review 语义去重归并 + 团队 dream 三阶段 + 六维评分 + 二级提炼 Prompt 模板 + 反向验证基线 + 二级提炼 job 读取 recall_log 统计。
5. **M5 Agent 装配增强 + 客户端完善**：调度索引表自动绑定 + category 受控词汇表 + 管理面板 + 冲突 diff 视图增强（三方合并/语义冲突提示）+ 角色模板细化。
6. **M6 治理与可观测性**：Dashboard（资产池治理看板/召回命中率/采纳率）、DREAMS.md 审查界面、两级采纳率反馈、生命周期管理、module_stats 拆分建议。

---

## 10. 已确认的决策

| 项 | 决策 |
|----|------|
| 部署形态 | 以 HTTP API 提供，不绑定部署环境，做好兼容性 |
| Git 托管 | 同时支持 GitLab 和 Gitea（Provider 抽象） |
| LLM | 支持主流 API 格式（OpenAI 兼容），可配置切换 |
| 客户端形态 | 只读写本地记忆文件夹，作为 coding 软件与控制端的交互桥梁 |
| 测试功能 | 不做，软件只负责代码和 harness 文件管理 |
| 多租户 | 暂不支持 |
