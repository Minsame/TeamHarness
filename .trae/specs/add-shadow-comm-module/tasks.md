# Tasks

## 阶段一：多软件适配层（coding_adapters）

- [x] Task 1: 创建 `server/coding_adapters/` 模块骨架与指纹表
  - [ ] SubTask 1.1: 创建 `fingerprints.py`，定义 `SOFTWARE_FINGERPRINTS` 表（软件 → 路径 / cli / provider 映射）
  - [ ] SubTask 1.2: 实现跨平台路径解析（USERPROFILE / HOME / XDG_CONFIG_HOME）
  - [ ] SubTask 1.3: 创建 `registry.py`，定义 `CodingSoftwareRegistry` 类

- [x] Task 2: 实现三级探测策略
  - [ ] SubTask 2.1: 路径直探（`stat` 检查已知路径）
  - [ ] SubTask 2.2: PATH 扫描（`shutil.which`）
  - [ ] SubTask 2.3: 指纹模糊匹配（扫描 `~` 下含 `sessions/*.jsonl` 的目录）

- [x] Task 3: 实现各软件 Adapter（均实现 `SessionProvider` Protocol）
  - [ ] SubTask 3.1: `ClaudeCodeAdapter`（`~/.claude/projects/**/*.jsonl`）
  - [ ] SubTask 3.2: `CodexAdapter`（`~/.codex/sessions/`）
  - [ ] SubTask 3.3: `CursorAdapter`（`~/.cursor/state.vscdb` SQLite 读取）
  - [ ] SubTask 3.4: `AiderAdapter`（`.aider.chat.history.md` Markdown 解析）
  - [ ] SubTask 3.5: `WindsurfAdapter`（`~/.codeium/windsurf/`）

- [x] Task 4: 集成到现有 PersonalDistill 流程
  - [x] SubTask 4.1: 修改 `create_session_provider` 工厂支持多 provider 聚合
  - [x] SubTask 4.2: 测试多软件会话合并采集

## 阶段二：通信拓扑抽象层（transport）

- [x] Task 5: 创建 `server/transport/` 模块骨架
  - [ ] SubTask 5.1: 定义 `SyncTransport` Protocol（`deliver` / `fetch` / `is_peer_reachable`）
  - [ ] SubTask 5.2: 定义 `PeerInfo` / `Message` / `SyncResult` 数据结构

- [x] Task 6: 实现三种拓扑传输
  - [ ] SubTask 6.1: `CentralSyncTransport`（复用现有 httpx，转发到中央服务）
  - [ ] SubTask 6.2: `P2PSyncTransport`（WebSocket 长连接 + 消息路由）
  - [ ] SubTask 6.3: `HybridSyncTransport`（P2P 优先，降级到中央）

- [x] Task 7: 实现 mDNS + 种子混合节点发现
  - [ ] SubTask 7.1: `MDnsDiscovery`（zeroconf 局域网广播）
  - [ ] SubTask 7.2: `SeedDiscovery`（配置种子节点列表交换）
  - [ ] SubTask 7.3: `CompositeDiscovery`（mDNS + 种子合并去重）

- [x] Task 8: 实现 Peer 身份互验
  - [ ] SubTask 8.1: 复用 `AgentApiKeyService` 做消息签名与验签
  - [ ] SubTask 8.2: P2P 握手协议（交换 key_hash + agent_id）

- [x] Task 9: 修改 `ClientConfig` 新增拓扑配置
  - [x] SubTask 9.1: 新增字段 `topology` / `peers` / `discovery`
  - [x] SubTask 9.2: 配置加载与校验

- [x] Task 10: 修改 `RecallClient` 传输层抽象
  - [x] SubTask 10.1: `__init__` 新增 `transport` 参数（默认 httpx 向后兼容）
  - [x] SubTask 10.2: 现有调用点回归测试

## 阶段三：成员 AI 通信核心（在线实时 + 影子联络）

- [x] Task 11: 创建 `server/async_comm/` 模块骨架与数据结构
  - [x] SubTask 11.1: 定义 `Message` / `ConversationEvent` / `VectorClock` 数据结构
  - [x] SubTask 11.2: 定义 `PeerSnapshot` 结构与目录布局

- [x] Task 12: 实现版本向量（`vector_clock.py`）
  - [x] SubTask 12.1: `VectorClock` 类（`increment` / `merge` / `compare`）
  - [x] SubTask 12.2: 因果排序与冲突检测算法
  - 注：VectorClock 实现已合并到 `types.py`（含 increment/merge/compare，返回 before/after/equal/concurrent，支持因果排序与冲突检测）

- [x] Task 13: 实现信箱（`mailbox.py`）
  - [x] SubTask 13.1: `Mailbox` 类（inbox + outbox，复用 `adoption.py` 的 JSONL 模式）
  - [x] SubTask 13.2: `event_id` 幂等去重
  - [x] SubTask 13.3: 消息状态机（`pending_delivery` / `delivered` / `confirmed` / `revised` / `needs_human_review`）

- [x] Task 14: 实现 PeerSnapshot 管理（`peer_snapshot.py`）
  - [x] SubTask 14.1: `PeerSnapshotManager`（拉取 / 刷新 / 过期清理）
  - [x] SubTask 14.2: harness 文件副本 + manifest 副本 + `vector_clock.json`
  - [x] SubTask 14.3: `snapshot_policy` 配置（`on_demand` / `scheduled`）

- [x] Task 15: 实现 ConversationLog（`conversation_log.py`）
  - [x] SubTask 15.1: append-only JSONL 事件日志
  - [x] SubTask 15.2: 事件类型（`ask` / `realtime_answer` / `simulated_answer` / `confirmed` / `revised` / `needs_human_review`）

- [x] Task 16: 实现通信核心入口（`peer_comm.py`）
  - [x] SubTask 16.1: `PeerComm.ask_peer()` 统一入口（自动路径选择：在线实时 / 离线影子）
  - [x] SubTask 16.2: 在线实时通信路径（通过 transport 实时投递 + 接收回答，标记 `realtime=true`）
  - [x] SubTask 16.3: `share_asset()` 资产定向共享（在线实时推送 / 离线进 outbox）
  - [x] SubTask 16.4: peer 可达性缓存（按 `network_check_interval_seconds` 缓存，避免每次探测）

- [x] Task 17: 实现影子联络离线路径（`shadow_comm.py`）
  - [x] SubTask 17.1: `ShadowComm.ask_peer()` 离线入口（写 outbox + 读 PeerSnapshot + 生成模拟回答）
  - [x] SubTask 17.2: 模拟回答生成（调本地 AI + peer 快照资产）
  - [x] SubTask 17.3: `degraded` 标记与 `based_on` 版本记录
  - [x] SubTask 17.4: `snapshot_stale` 检测（超过 `snapshot_ttl_days`）

- [x] Task 18: 实现上线同步协议（`sync_protocol.py`）
  - [x] SubTask 18.1: 交换 `vector_clock` 算出增量
  - [x] SubTask 18.2: 推送 outbox 待投递消息
  - [x] SubTask 18.3: 接收 inbox 消息 + 写入 ConversationLog
  - [x] SubTask 18.4: 差异检测（基于语义相似度阈值）

- [x] Task 19: 实现冲突解决（`conflict_resolver.py`）
  - [x] SubTask 19.1: 自动确认（相似度 ≥ `auto_confirm_threshold`）
  - [x] SubTask 19.2: 自动修订（中间区间）
  - [x] SubTask 19.3: 标记人工介入（≤ `conflict_threshold`）

## 阶段四：MCP Server 与 CLI 入口

- [x] Task 20: 实现 MCP Server（`server/mcp_server/`）
  - [x] SubTask 20.1: MCP 协议封装（基于 Python MCP SDK）
  - [x] SubTask 20.2: 工具注册（`ask_peer` / `search_team_assets` / `list_peers` / `share_asset`）
  - [x] SubTask 20.3: `transport_bridge`（调用 async_comm + transport 层）

- [x] Task 21: 实现 CLI 子命令
  - [x] SubTask 21.1: `teamharness ask-peer` 子命令
  - [x] SubTask 21.2: `teamharness peers` 列出已知 peer
  - [x] SubTask 21.3: `teamharness shadow-log` 查看交流报告（含实时 + 影子）

## 阶段五：守护进程集成

- [x] Task 22: 修改 `ClientDaemon` 新增任务调度
  - [x] SubTask 22.1: peer 心跳检测任务（维护在线状态缓存供路径选择）
  - [x] SubTask 22.2: 实时通信会话管理（在线 peer 会话维持与超时清理）
  - [x] SubTask 22.3: 影子联络触发任务（基于 `ask_peer` 调用事件）
  - [x] SubTask 22.4: 上线同步任务（peer 由不可达转可达时触发同步与对账）
  - [x] SubTask 22.5: peer 快照刷新任务（按 `snapshot_policy`）

## 阶段六：测试与验证

- [x] Task 23: 单元测试
  - [x] SubTask 23.1: `coding_adapters` 各 Adapter 解析测试（81 tests，Task 1-3）
  - [x] SubTask 23.2: `vector_clock` 合并 / 比较测试（28 tests，Task 11）
  - [x] SubTask 23.3: `mailbox` 幂等与状态机测试（63 tests，Task 13）
  - [x] SubTask 23.4: 在线实时通信路径测试（peer 可达 → 实时投递）（19 tests，Task 16）
  - [x] SubTask 23.5: 影子联络端到端测试（模拟离线 → 上线 → 同步与对账）（28+24 tests，Task 17+18）

- [x] Task 24: 集成测试
  - [x] SubTask 24.1: 双 peer 在线实时通信全流程测试
  - [x] SubTask 24.2: 双 peer 影子联络全流程测试
  - [x] SubTask 24.3: 在线 → 离线路径切换测试（会话中途 peer 下线）
  - [x] SubTask 24.4: 拓扑切换回归测试
  - [x] SubTask 24.5: MCP 工具调用测试

## 阶段七：AI 自主调用、职能路由、对话持久化与梦境集成

- [x] Task 25: 按 tag 路由 + 缓存同步（复用主项目 Member.tags）
  - [x] SubTask 25.1: `PeerComm.ask_peer` 新增 `tag` 参数（如 `tag="运维"`）
  - [x] SubTask 25.2: central 模式路由：通过 `GET /v1/team/members` 查询 `Member.tags` 匹配候选（实时查 DB，不缓存）
  - [x] SubTask 25.3: `/v1/comm/peers` 端点实现约束：`PeerInfo.capabilities` 从 `Member.tags` 实时读取
  - [x] SubTask 25.4: P2P 模式管理员权威源：admin 节点定期广播 `tags_sync` 消息（msg_type=`tags_sync`，payload 含全部 peer_id → tags 映射）
  - [x] SubTask 25.5: 非 admin peer 接收 `tags_sync` 后刷新本地 `_peer_registry.capabilities`
  - [x] SubTask 25.6: 非 admin peer 降级路径：未收到 `tags_sync` 时使用 `ClientConfig.peers[].tags` 静态配置
  - [x] SubTask 25.7: 多候选混合策略（先广播轻量探测 → 收到响应后定向追问 → 无响应超时转影子联络）
  - [x] 注：主项目 `Member.tags` + `GET /v1/team/tags` + `GET /v1/team/members` 已开发完成，直接复用

- [x] Task 26: MCP 工具描述增强
  - [x] SubTask 26.1: `ask_peer` 工具描述增加使用时机说明（"当你需要向其他成员的 AI 提问、讨论或共享资产时调用"）
  - [x] SubTask 26.2: `ask_peer` 工具 schema 支持 `peer_id` 或 `tag` 二选一参数
  - [x] SubTask 26.3: 新增 `resume_conversation` 工具（恢复暂停的对话）

- [x] Task 27: 对话持久化与恢复
  - [x] SubTask 27.1: `ConversationLog` 新增对话状态标记（`active` / `paused` / `timeout_disconnect` / `resumed`）
  - [x] SubTask 27.2: 对话超时检测（`realtime_session_timeout` 默认 600s，daemon 周期扫描）
  - [x] SubTask 27.3: 对话恢复 API（基于 `in_reply_to` 链重建上下文，peer 上线时自动触发）
  - [x] SubTask 27.4: 任一方下线时对话状态持久化（写 ConversationLog + 标记 `paused`）

- [x] Task 28: 梦境提炼集成
  - [x] SubTask 28.1: `PersonalDistill.run_light` 读取 ConversationLog 作为 session 输入
  - [x] SubTask 28.2: Light 阶段筛选跨职能协作信号 + `needs_human_review` 事件加权
  - [x] SubTask 28.3: 提炼产出回灌为 `Member.tags` 标签（如"账号管理专家"，通过 `PATCH /v1/team/members/{id}`）
  - [x] SubTask 28.4: 集成测试（对话 → 梦境提炼 → DREAMS.md 产出 → tags 回灌）

- [x] Task 29: 阶段七测试
  - [x] SubTask 29.1: 按 tag 路由 + 多候选混合策略单元测试
  - [x] SubTask 29.2: 对话持久化与恢复端到端测试（中途下线 → 恢复 → 继续讨论）
  - [x] SubTask 29.3: 对话超时断开 + 自动恢复测试
  - [x] SubTask 29.4: 梦境提炼集成测试（ConversationLog → Light/REM/Deep → DREAMS.md）
  - [x] SubTask 29.5: MCP `ask_peer` 按 tag 路由 + `resume_conversation` 工具测试

# Task Dependencies

- Task 2 依赖 Task 1
- Task 3 依赖 Task 1
- Task 4 依赖 Task 3
- Task 6 依赖 Task 5
- Task 7 依赖 Task 5
- Task 8 依赖 Task 5
- Task 10 依赖 Task 6
- Task 12-15 依赖 Task 5
- Task 16 依赖 Task 13, 14, 15
- Task 17 依赖 Task 12, 13, 14, 15, 16
- Task 18 依赖 Task 12, 16, 17
- Task 19 依赖 Task 17, 18
- Task 20 依赖 Task 16, 17, 18
- Task 21 依赖 Task 16, 17, 18
- Task 22 依赖 Task 16, 17, 18, 19
- Task 23 依赖对应被测模块
- Task 24 依赖所有前置任务
- Task 25 依赖 Task 16, 22（PeerComm + Daemon 已就绪）
- Task 26 依赖 Task 20, 25（MCP Server + role 路由已就绪）
- Task 27 依赖 Task 15, 22（ConversationLog + Daemon 已就绪）
- Task 28 依赖 Task 15, 27（ConversationLog + 对话状态已就绪）
- Task 29 依赖 Task 25, 26, 27, 28

# 可并行任务

- Task 1-4（阶段一）与 Task 5-10（阶段二）可并行启动
- Task 20 与 Task 21 可并行
- Task 25 与 Task 27 可并行（职能路由与对话持久化互不依赖）
- Task 26 依赖 Task 25，但 Task 28 依赖 Task 27，两者可并行
