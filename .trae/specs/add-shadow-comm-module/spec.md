# 成员 AI 通信模块 Spec

> **模块定位**：本模块是 TeamHarness 项目的**独立子模块**，统一负责成员 AI 之间的**全部通信功能**——既包括 peer 在线时的实时通信，也包括 peer 离线时的"影子联络"降级路径。模块名 `async_comm` 为历史命名（实现初期以异步场景为切入点），实际职责覆盖同步实时 + 异步影子两条路径，业务层通过统一入口 `PeerComm.ask_peer()` 调用，不感知 peer 是否在线。
>
> **命名说明**：
> - 模块对外名称：**成员 AI 通信模块**
> - 离线场景专用名称：**影子联络（Shadow Communication）**
> - 代码包名：`server/async_comm/`（历史命名，覆盖 sync + async 全场景，不再改名以避免破坏已完成的实现与测试）
>
> **独立性**：本模块作为 TeamHarness 的独立模块存在，通过 `server/transport/`、`server/async_comm/`、`server/coding_adapters/`、`server/mcp_server/` 四个子包边界清晰隔离，仅通过 `ClientConfig` / `RecallClient` / `ClientDaemon` 三个接入点与主项目耦合，可独立测试、独立演进。

## 架构总览

```
┌─────────────────────────────────────────────────────────────────┐
│                    成员 AI 通信模块（独立子模块）                   │
│                                                                  │
│  ┌──────────────────┐   ┌──────────────────────────────────┐    │
│  │  MCP Server      │   │  CLI 子命令                       │    │
│  │  (skill 入口)    │   │  ask-peer / peers / shadow-log    │    │
│  └────────┬─────────┘   └──────────────┬───────────────────┘    │
│           │                            │                         │
│           └────────────┬───────────────┘                         │
│                        ▼                                         │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  PeerComm（统一通信入口，自动路径选择）                      │   │
│  │  ├─ 在线实时路径 → transport.deliver + fetch（realtime）   │   │
│  │  └─ 离线路径     → ShadowComm（影子联络，degraded）         │   │
│  └────────┬───────────────────────────────┬──────────────────┘   │
│           │                               │                      │
│           ▼                               ▼                      │
│  ┌──────────────────┐         ┌──────────────────────────┐       │
│  │  transport 层    │         │  async_comm 核心         │       │
│  │  ├─ CentralSync  │         │  ├─ Mailbox（信箱）       │       │
│  │  ├─ P2PSync      │         │  ├─ ConversationLog      │       │
│  │  └─ HybridSync   │         │  ├─ PeerSnapshotManager  │       │
│  │  + discovery     │         │  ├─ SyncProtocol         │       │
│  │  + auth          │         │  └─ ConflictResolver     │       │
│  └──────────────────┘         └──────────────────────────┘       │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │  coding_adapters 层（多软件 harness 资源发现，非通信路径）   │   │
│  │  Trae / Claude Code / Codex / Cursor / Aider / Windsurf  │   │
│  └──────────────────────────────────────────────────────────┘   │
└─────────────────────────────────────────────────────────────────┘

        ▲ 接入点（与主项目耦合）              ▼ 主项目依赖
        ├─ ClientConfig（配置字段）           ├─ adoption.py（JSONL 幂等模式复用）
        ├─ RecallClient（传输层抽象）         ├─ AgentApiKeyService（peer 互验复用）
        └─ ClientDaemon（调度任务）           └─ SessionProvider Protocol（Adapter 契约）
```

**数据流**：
- **在线实时**：`PeerComm.ask_peer` → `transport.deliver` → peer 处理 → `transport.fetch` → 写 `ConversationLog`（`realtime=true`）
- **离线影子**：`PeerComm.ask_peer` → `ShadowComm.ask_peer` → 写 outbox + 读 `PeerSnapshot` → 生成 `simulated_answer`（`degraded=true`）→ peer 上线时 `SyncProtocol.sync_with_peer` 对账

## Why

TeamHarness 当前通信模式为"客户端 ↔ 中央服务端"的同步星型架构，强依赖中央服务在线，且仅适配 Trae 一种本地 AI 软件。本模块作为 TeamHarness 的独立子模块，**统一负责成员 AI 之间的通信功能**，覆盖以下全部场景：

- **在线实时通信（主路径）**：peer 可达时，成员 AI 之间直接提问 / 讨论 / 资产共享，延迟为网络 RTT + 处理时间
- **离线影子联络（降级路径）**：peer 不在线时，基于其本地 harness 快照进行模拟交流，上线后自动同步与对账
- **拓扑无关**：有中央服务器时由服务器中转，无中央服务器时 P2P 直连，或混合模式
- **多软件资源发现**：自动检测本机已安装的 AI coding 软件（Claude Code / Codex / Cursor / Aider / Windsurf）并采集其 harness 资产作为通信内容来源

**核心设计原则**：在线实时与离线影子是**同一通信接口的两种执行路径**，业务层不感知 peer 是否在线，由模块自动选择实时投递或影子联络。在线实时是默认主路径，影子联络仅在 peer 不可达时自动降级触发。

## What Changes

- 新增 `server/coding_adapters/` 子模块：多 AI 软件指纹检测与会话采集（Trae / Claude Code / Codex / Cursor / Aider / Windsurf）
- 新增 `server/transport/` 子模块：通信拓扑抽象（central / p2p / hybrid），WebSocket P2P 节点，mDNS + 种子混合发现
- 新增 `server/async_comm/` 子模块：**成员 AI 通信核心**（在线实时 + 离线影子联络），含信箱、peer 快照、版本向量、上线同步协议、冲突解决
- 新增 `server/mcp_server/` 子模块：MCP Server 统一 skill 入口
- 新增 CLI 子命令 `teamharness ask-peer` / `teamharness peers` / `teamharness shadow-log` 作为不支持 MCP 软件的兜底入口
- 修改 `server/client/config.py`：新增 topology / peers / discovery / async_comm 配置字段（含 `realtime_session_timeout`）+ `peers[].tags` 预配置（P2P 降级用）
- 修改 `server/client/daemon.py`：新增 peer 心跳、实时通信调度、对话恢复检测、影子联络触发、上线同步、peer 快照刷新、梦境提炼集成任务
- 修改 `server/client/recall_client.py`：传输层从硬编码 httpx 改为 Transport 抽象注入
- 修改 `server/async_comm/peer_comm.py`：`ask_peer` 支持 `tag` 参数（按 `Member.tags` 路由 + 多候选混合策略）
- 修改 `server/async_comm/conversation_log.py`：新增对话状态标记（`paused` / `timeout_disconnect` / `resumed`）与对话恢复 API
- 修改 `server/mcp_server/tools.py`：`ask_peer` 工具描述增强（使用时机 + 按 peer_id 或 tag 路由）
- 修改 `server/distill_personal/`：Light 阶段读取 ConversationLog 作为 session 输入
- **复用**：主项目已有 `Member.tags` + `GET /v1/team/tags` + `GET /v1/team/members`（已开发完成），通信模块直接查询，不新增标签存储
- **BREAKING**：`RecallClient.__init__` 签名变更（新增 `transport` 参数，默认保持 httpx 向后兼容）

## Impact

- Affected specs：`TECH_PROPOSAL.md` 通信架构章节需补充影子联络与拓扑切换说明；`feasibility-gap-analysis/` 不冲突
- Affected code：
  - `server/client/config.py` — 配置字段扩展
  - `server/client/daemon.py` — 调度任务扩展
  - `server/client/recall_client.py` — 传输层抽象
  - `server/distill_personal/session_provider.py` — 新增多软件 Provider
  - `server/binding/auth_service.py` — peer 互验复用（不修改其内部实现）
  - `server/client/adoption.py` — JSONL + event_id 幂等模式被 async_comm 复用（不修改其内部实现）

## ADDED Requirements

### Requirement: 成员 AI 通信（核心）
系统 SHALL 提供成员 AI 之间的统一通信接口，作为本模块的首要职责。根据 peer 在线状态自动选择实时投递或影子联络路径，业务层不感知 peer 是否在线。**在线实时通信是默认主路径**，仅当 peer 不可达时才降级到影子联络。

#### Scenario: 在线实时通信
- **WHEN** 发起方调用 `ask_peer(peer_id="bob", question="...")` 且 bob 当前可达
- **THEN** 系统通过当前拓扑（central / p2p / hybrid）实时投递消息给 bob
- **AND** bob 的 AI 实时处理并返回回答
- **AND** 通信事件写入双方 ConversationLog（标记 `realtime=true`）
- **AND** 不经过影子快照路径

#### Scenario: 在线双向讨论
- **WHEN** alice 与 bob 均在线且进行多轮讨论
- **THEN** 系统维持会话上下文（基于 `in_reply_to` 回复链）
- **AND** 每轮消息实时投递，延迟低于网络 RTT + 处理时间
- **AND** 会话结束后完整记录写入双方 ConversationLog

#### Scenario: 自动路径选择
- **WHEN** 发起方调用 `ask_peer` 且系统不确定 peer 是否可达
- **THEN** 系统先探测 peer 可达性（按 `network_check_interval_seconds` 缓存）
- **AND** peer 可达 → 走实时通信路径（主路径）
- **AND** peer 不可达 → 自动降级为影子联络路径（降级路径）
- **AND** 路径选择对调用方透明（返回结构一致，仅 `degraded` 标记不同）

#### Scenario: 资产定向共享
- **WHEN** 发起方调用 `share_asset(asset_id, to=peer_id)` 且 peer 可达
- **THEN** 系统实时推送资产内容（含双区 frontmatter）给 peer
- **AND** peer 收到后写入本地 inbox 并提示用户
- **AND** peer 不可达时资产进入 outbox 等待上线同步

> **边界说明**：`share_asset` 是**通信层**的 peer 间定向传递（类似发邮件附件，走 `POST /v1/comm/deliver`），**不替代主项目的 git PR 流程**。资产正式发布到团队库仍需走 `teamharness pr` → git SSOT + PR Review + DB 索引。peer 收到 `share_asset` 传递的资产后，若想正式纳入团队库，需另行发起 PR。

### Requirement: 影子联络（离线降级路径）
影子联络是成员 AI 通信在 peer 不在线时的**降级路径**，而非独立功能。系统 SHALL 基于 peer 的本地 harness 快照进行模拟交流，并将交流报告持久化到本地，待 peer 上线时自动同步与对账。影子联络的触发条件是"peer 不可达"，终止条件是"peer 上线并完成对账"。

#### Scenario: 离线发起影子提问
- **WHEN** 发起方调用 `ask_peer(peer_id="bob", question="...")` 且 bob 当前不可达
- **THEN** 系统将问题写入本地 outbox（状态 `pending_delivery`）
- **AND** 系统读取本地 `PeerSnapshot[bob]` 的 harness 快照
- **AND** 基于快照生成 `simulated_answer` 事件并写入 ConversationLog
- **AND** 模拟回答标记 `degraded=true`，`based_on=bob_v38`（v38 为快照版本）

#### Scenario: peer 上线自动同步与对账
- **WHEN** bob 上线且 alice 有待投递的影子联络事件
- **THEN** 系统交换双方 vector_clock
- **AND** alice 推送 outbox 中 `pending_delivery` 消息给 bob
- **AND** bob 收到后对比"alice 的模拟回答"与"基于 bob 当前 harness 的回答"
- **AND** 语义相似度 ≥ `auto_confirm_threshold`（默认 0.8）→ 标记 `confirmed`
- **AND** 相似度 ≤ `conflict_threshold`（默认 0.3）→ 标记 `needs_human_review`
- **AND** 中间区间 → 标记 `revised`（附上 bob 新回答）

#### Scenario: 无中央服务器的纯 P2P 模式
- **WHEN** 配置 `topology: p2p` 且无 server_url
- **THEN** 在线时 peer 直连实时通信
- **AND** 离线时消息存本地 outbox，影子联络快照由本地维护
- **AND** peer 上线时自动触发同步与对账
- **AND** 不依赖任何中央服务

#### Scenario: 影子快照版本过期
- **WHEN** 发起方发起影子提问但本地 PeerSnapshot 已超过 `snapshot_ttl_days`（默认 30 天）
- **THEN** 系统标记该模拟回答 `snapshot_stale=true`
- **AND** 在 ConversationLog 中提示"快照已过期，结果可信度低"
- **AND** peer 上线后强制刷新快照

### Requirement: 多软件 harness 资产发现
系统 SHALL 自动检测本机已安装的 AI coding 软件并采集其 harness 资产（会话 / 规则 / 记忆），支持 Trae、Claude Code、Codex、Cursor、Aider、Windsurf。此能力用于发现用户已有的 harness 资源作为通信内容来源，**不参与通信路径本身**。

#### Scenario: 多软件并存
- **WHEN** 用户本机同时安装 Trae 和 Claude Code
- **THEN** 系统检测到两个软件并存
- **AND** 为每个软件实例化对应 Adapter
- **AND** 所有 Adapter 实现 `SessionProvider` Protocol
- **AND** 采集的会话统一转为 `Session` 内部结构

#### Scenario: 跨平台路径探测
- **WHEN** 系统在 Windows / Linux / macOS 上启动
- **THEN** 系统按平台对应的路径模式探测软件
- **AND** Windows 使用 `%USERPROFILE%` / `%LOCALAPPDATA%`
- **AND** Linux / macOS 使用 `$HOME` / `$XDG_CONFIG_HOME`
- **AND** 显式环境变量覆盖自动探测

#### Scenario: 未知软件兜底
- **WHEN** 用户使用未登记的 AI 软件
- **THEN** 系统通过指纹模糊匹配扫描 `~` 下含 `sessions/*.jsonl` 的目录
- **AND** 命中后使用 `GenericJsonlSessionProvider` 兜底采集

### Requirement: 通信拓扑可切换
系统 SHALL 支持三种通信拓扑配置切换：central（现有）、p2p（去中心化）、hybrid（混合），业务层不感知具体拓扑。

#### Scenario: 拓扑切换
- **WHEN** 用户在 config.yaml 设置 `topology: p2p`
- **THEN** 系统使用 `P2PSyncTransport` 实现
- **AND** 现有 RecallClient / daemon 业务逻辑不变
- **AND** 切换到 `central` 时使用 `CentralSyncTransport`
- **AND** 切换到 `hybrid` 时优先 P2P，不可达降级到中央中转

#### Scenario: 拓扑无关的消息投递
- **WHEN** 业务层调用 `transport.deliver(peer_id, messages)`
- **THEN** 无论底层拓扑如何，接口契约一致
- **AND** peer 可达时实时投递
- **AND** peer 不可达时存入本地 outbox 等待下次同步

### Requirement: MCP / CLI 双 skill 入口
系统 SHALL 提供 MCP Server 作为统一 skill 入口，同时提供 CLI 子命令作为不支持 MCP 软件的兜底入口，两套入口共享同一套 transport + async_comm 底层。**两套入口均覆盖在线实时与离线影子全场景**，不因入口不同而阉割通信能力。

#### Scenario: MCP 工具调用
- **WHEN** Claude Code 通过 MCP 调用 `ask_peer` 工具
- **THEN** MCP Server 接收请求并调用 async_comm 层
- **AND** peer 在线 → 返回实时回答（`realtime=true`）
- **AND** peer 离线 → 返回模拟回答（`degraded=true`，影子联络）

#### Scenario: CLI 兜底
- **WHEN** 用户在 Aider 中调用 `teamharness ask-peer`
- **THEN** CLI 子命令调用同一 async_comm 层
- **AND** 行为与 MCP 入口一致（含在线实时 + 离线影子全路径）

### Requirement: Peer 身份互验
系统 SHALL 复用现有 `AgentApiKeyService` 在 peer 间进行身份互验，P2P 模式下每条消息携带发送方 API Key 的哈希签名。

#### Scenario: P2P 消息验签
- **WHEN** peer A 向 peer B 发送消息
- **THEN** 消息携带 `sender_key_hash` 和 `signature`
- **AND** peer B 通过 `AgentApiKeyService` 反查身份
- **AND** 验签失败拒绝消息并记录

### Requirement: AI 自主调用与职能路由
系统 SHALL 让成员的 AI 通过 MCP skill 自主发起通信，无需用户手动 CLI 调用。AI 通过 MCP 工具描述知晓何时调用通信能力、如何按职能路由到对应人员。**职能标签复用主项目已有的 `Member.tags` 字段**（Text 列存 JSON 数组，如 `["前端","后端"]`，由 `POST/PATCH /v1/team/members` 管理，`GET /v1/team/tags` 返回系统所有已用标签）。通信模块通过 `GET /v1/team/members` 查询成员标签做路由，不在通信模块自管标签存储。

#### Scenario: AI 自主调用 ask_peer skill
- **WHEN** 后端开发 A 的 AI 在开发过程中需要确认"这个 API 能否在前端加个按钮"
- **THEN** AI 识别需要跨职能沟通，自主调用 MCP `ask_peer` 工具
- **AND** 工具描述包含使用时机说明（"当你需要向其他成员的 AI 提问、讨论或共享资产时调用"）
- **AND** AI 按 `peer_id` 或 `tag` 指定目标（如 `ask_peer(peer_id="bob")` 或 `ask_peer(tag="前端")`）

#### Scenario: 按职能路由（未指明具体人）
- **WHEN** 后端开发 A 的 AI 调用 `ask_peer(tag="运维", question="为啥我登不上测试环境")`
- **AND** 系统中有多个带"运维"标签的成员（charlie / dave）
- **THEN** 系统通过 `GET /v1/team/members` 查询 `Member.tags` 匹配含"运维"标签的成员
- **AND** 采用混合策略：先向所有匹配候选广播轻量探测（"谁负责账号管理 / 测试环境登录？"）
- **AND** 收到响应后定向追问具体问题给确认职责的 peer
- **AND** 若无候选响应，超时后转为影子联络（逐一基于各候选快照模拟询问）

#### Scenario: 职能标签维护与缓存同步
- **WHEN** admin 在前端成员管理界面为成员添加/修改标签
- **THEN** 标签通过 `POST/PATCH /v1/team/members` 写入 `Member.tags` 字段
- **AND** `GET /v1/team/tags` 返回系统所有已用标签（去重排序）

**central 模式缓存同步**：
- **AND** `/v1/comm/peers` 端点实现约束：`PeerInfo.capabilities` 必须从 `Member.tags` 实时读取（每次 API 调用查 DB，不在端点层独立缓存）
- **AND** `CentralSyncTransport.discover_peers()` 无缓存，每次 `GET /v1/comm/peers` 实时查询 → admin 修改标签后立即生效

**P2P 模式缓存同步（管理员权威源）**：
- **AND** P2P 模式下管理员电脑作为标签权威源（管理员本地有完整的成员数据 + `Member.tags`）
- **AND** 管理员节点定期（按 `network_check_interval_seconds`）向 P2P 网络广播最新成员标签快照（msg_type=`tags_sync`，payload 含全部 peer_id → tags 映射）
- **AND** 非 admin peer 收到 `tags_sync` 消息后刷新本地 `_peer_registry` 的 `capabilities` 字段
- **AND** 非 admin peer **不自行声明 tags**（声明的 tags 不作为路由依据，只接收管理员广播）
- **AND** 非 admin peer 首次启动时若未收到 `tags_sync`，降级使用 `ClientConfig.peers[].tags` 静态配置

> **设计决策**：
> 1. **central 模式**：`/v1/comm/peers` 端点从 `Member.tags` 实时读取，不在端点层缓存 → admin 修改标签后立即生效
> 2. **P2P 模式（方案 A + 管理员权威）**：管理员电脑作为"软中心"，定期广播标签快照到 P2P 网络，其他 peer 接收后刷新本地缓存。理由：①P2P 网络无中央 DB，但管理员电脑有完整成员数据（通过前端管理界面维护）；②其他成员的 self-declared tags 不重要，以管理员维护的为准；③避免每个 peer 各自声明导致的不一致
> 3. **降级路径**：非 admin peer 未收到广播时使用 `ClientConfig.peers[].tags` 静态配置

### Requirement: 对话持久化与恢复
系统 SHALL 保证在线实时对话的持久化，任一方随时停止对话时不丢失上下文，对话超时自动断开，peer 回来时能恢复上下文继续对话。

#### Scenario: 对话中途一方停止
- **WHEN** alice 与 bob 在线实时讨论 API 设计，bob 突然下线
- **THEN** 系统将当前对话状态持久化到 ConversationLog（含 `in_reply_to` 回复链）
- **AND** alice 侧对话标记为 `paused`（等待恢复）
- **AND** bob 回来时系统自动恢复对话上下文（基于 `in_reply_to` 链重建）
- **AND** 恢复后双方可继续讨论，新消息接续回复链

#### Scenario: 对话超时断开
- **WHEN** 对话双方超过 `realtime_session_timeout`（默认 600s）无新消息
- **THEN** 系统自动断开实时会话，标记为 `timeout_disconnect`
- **AND** 对话状态持久化到 ConversationLog
- **AND** 任一方发起新消息时自动恢复对话（若对方在线）
- **AND** 若对方不在线，降级为影子联络

#### Scenario: 对话恢复
- **WHEN** bob 上线且有与 alice 的 `paused` / `timeout_disconnect` 对话
- **THEN** 系统自动加载历史对话上下文（基于 `in_reply_to` 链）
- **AND** 向 alice 通知 bob 已回来
- **AND** 双方可继续讨论，新消息接续原回复链

### Requirement: 梦境提炼集成
系统 SHALL 将成员 AI 通信的 ConversationLog 作为现有 PersonalDistill（一级提炼 / 梦境机制）的输入信号，由 Light/REM/Deep 三阶段提炼产出可复用经验，写入 DREAMS.md。提炼按现有 `distill_schedule_cron` 定时触发，不额外新建提炼流程。

#### Scenario: 对话记录进入梦境提炼
- **WHEN** 按现有 `distill_schedule_cron`（默认每日 02:00）触发一级提炼
- **THEN** PersonalDistill 的 `run_light(sessions)` 读取 ConversationLog 作为 session 输入
- **AND** Light 阶段筛选有价值的对话信号（如跨职能协作模式、反复出现的问题）
- **AND** REM 阶段归纳意图（"用户反复向运维询问测试环境登录问题" → 可复用经验）
- **AND** Deep 阶段产出资产（规则 / 记忆 / skill），写入 DREAMS.md

#### Scenario: 影子联络对账结果进入提炼
- **WHEN** 影子联络的 `confirmed` / `revised` / `needs_human_review` 事件产生
- **THEN** 这些对账结果作为高价值信号进入 Light 阶段
- **AND** `needs_human_review` 事件标记为高优先级信号（pattern_count 加权）
- **AND** 提炼产出的经验可回灌为 peer 的 `Member.tags` 标签（如"账号管理专家"，通过 `PATCH /v1/team/members/{id}` 更新）

## MODIFIED Requirements

### Requirement: 客户端配置
ClientConfig 新增字段：`topology` / `peers` / `discovery` / `async_comm` 子配置（含 `snapshot_policy` / `snapshot_ttl_days` / `conflict_threshold` / `auto_confirm_threshold` / `realtime_session_timeout`）。`peers` 列表项可选携带 `tags` 字段（**P2P 模式降级用**：非 admin peer 未收到管理员 `tags_sync` 广播时使用静态配置；admin peer 本身不读此字段，从本地 `Member.tags` 读取）。所有新字段均有默认值，不破坏现有配置加载。

### Requirement: 守护进程调度
ClientDaemon 新增任务：
- peer 心跳检测（按 `network_check_interval_seconds`，维护 peer 在线状态缓存供路径选择）
- 实时通信会话管理（在线 peer 的会话维持与超时清理，`realtime_session_timeout` 默认 600s）
- 对话恢复检测（peer 上线时检查是否有 `paused` / `timeout_disconnect` 对话待恢复）
- 影子联络触发（按 `ask_peer` 调用事件，非周期）
- 上线同步任务（peer 由不可达转可达时触发同步与对账）
- peer 快照刷新任务（按 `snapshot_policy`）
- 梦境提炼集成（按现有 `distill_schedule_cron`，将 ConversationLog 作为 PersonalDistill 输入）

### Requirement: RecallClient 传输层抽象
RecallClient 的传输依赖从硬编码 `httpx` 改为注入 `Transport`。默认保持 `httpx` 实现以向后兼容现有中央服务模式。

## REMOVED Requirements

无（本模块为新增，不删除现有功能）。
