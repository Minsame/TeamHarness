# Checklist

## Spec 完整性与模块定位
- [x] spec.md 标题为"成员 AI 通信模块 Spec"（非"Shadow Comm Module"）
- [x] spec.md 顶部"模块定位"章节明确：模块统一负责成员 AI 之间的全部通信功能（在线实时 + 离线影子）
- [x] spec.md 明确在线实时通信是默认主路径，影子联络是 peer 不可达时的降级路径（非独立功能）
- [x] spec.md"命名说明"解释 `async_comm` 包名覆盖 sync + async 全场景（历史命名，不再改名）
- [x] spec.md"独立性"说明四个子包边界与三个接入点（ClientConfig / RecallClient / ClientDaemon）
- [x] spec.md 含"架构总览"图，展示 MCP/CLI → PeerComm → transport/async_comm 分层与数据流
- [x] spec.md 完整覆盖在线实时通信、影子联络、多软件适配、拓扑切换、MCP/CLI 入口
- [x] spec.md 标注 BREAKING 变更（`RecallClient.__init__` 签名）
- [x] spec.md 说明与现有 `TECH_PROPOSAL.md` 的关系
- [x] spec.md"多软件 harness 资产发现"Requirement 明确该能力不参与通信路径本身（仅资源发现）
- [x] spec.md"MCP / CLI 双 skill 入口"Requirement 明确两套入口均覆盖在线实时 + 离线影子全场景

## coding_adapters 模块
- [x] 模块独立可测试，不依赖 transport / async_comm
- [x] 所有 Adapter 实现 `SessionProvider` Protocol
- [x] 跨平台路径探测在 Windows / Linux / macOS 上均可工作
- [x] 三级探测策略（路径直探 / PATH 扫描 / 指纹模糊匹配）覆盖
- [x] 未知软件兜底走 `GenericJsonlSessionProvider`

## transport 模块
- [x] `SyncTransport` Protocol 定义清晰（deliver / fetch / is_peer_reachable）
- [x] 拓扑切换不影响现有 RecallClient 业务逻辑
- [x] RecallClient 默认保持 httpx 向后兼容
- [x] central 模式行为与现有项目一致
- [x] p2p 模式无中央服务器时可独立工作
- [x] hybrid 模式优先 P2P，降级到中央中转
- [x] mDNS + 种子混合发现可独立或组合使用
- [x] Peer 间消息携带 `AgentApiKeyService` 签名

## async_comm 模块（成员 AI 通信核心）
- [x] 模块复用 `adoption.py` 的 JSONL + event_id 幂等模式
- [x] `PeerComm.ask_peer()` 统一入口自动选择在线实时 / 离线影子路径
- [x] 在线实时通信路径标记 `realtime=true`，不经过影子快照
- [x] 路径选择对调用方透明（返回结构一致，仅 `degraded` 标记不同）
- [x] `share_asset()` 在线实时推送 / 离线进 outbox
- [x] peer 可达性缓存按 `network_check_interval_seconds` 生效
- [x] 影子联络在 peer 离线时生成 `simulated_answer` 事件
- [x] 模拟回答标记 `degraded=true` 与 `based_on` 版本号
- [x] `snapshot_stale` 检测（超过 `snapshot_ttl_days`）
- [x] peer 上线时自动触发同步与对账协议
- [x] 同步后基于语义相似度阈值决定 `confirmed` / `revised` / `needs_human_review`
- [x] `VectorClock` 实现因果排序与冲突检测
- [x] `PeerSnapshot` 包含 harness 副本 + manifest + `vector_clock.json`
- [x] `PeerSnapshot` 按 `snapshot_ttl_days` 过期清理
- [x] `ConversationLog` 为 append-only JSONL，含 `realtime_answer` / `simulated_answer` 事件类型
- [x] `Mailbox` 状态机覆盖五种状态

## MCP / CLI 入口
- [x] MCP Server 暴露 `ask_peer` / `search_team_assets` / `list_peers` / `share_asset` 工具
- [x] CLI 子命令与 MCP 入口共享同一底层
- [x] CLI 子命令包含 `ask-peer` / `peers` / `shadow-log`

## ClientConfig 与 Daemon 集成
- [x] ClientConfig 新增字段均有默认值，不破坏现有配置加载
- [x] ClientDaemon 新增 peer 心跳 / 实时通信会话管理 / 影子联络 / 上线同步 / 快照刷新任务
- [x] peer 心跳维护在线状态缓存供路径选择使用
- [x] 上线同步任务在 peer 由不可达转可达时触发

## AI 自主调用与职能路由（阶段七）
- [x] spec.md"AI 自主调用与职能路由"Requirement 明确 AI 通过 MCP skill 自主调用
- [x] MCP `ask_peer` 工具描述含使用时机说明（让 AI 知道何时调用）
- [x] `PeerComm.ask_peer` 支持 `tag` 参数（如 `tag="运维"`）
- [x] central 模式路由：`GET /v1/team/members` 实时查询 `Member.tags` 匹配候选
- [x] `/v1/comm/peers` 端点 `PeerInfo.capabilities` 从 `Member.tags` 实时读取（不在端点层缓存）
- [x] `CentralSyncTransport.discover_peers()` 无缓存，admin 修改标签后立即生效
- [x] P2P 模式管理员权威源：admin 节点定期广播 `tags_sync` 消息（含全部 peer_id → tags 映射）
- [x] 非 admin peer 接收 `tags_sync` 后刷新本地 `_peer_registry.capabilities`
- [x] 非 admin peer 不自行声明 tags（声明的 tags 不作为路由依据）
- [x] 非 admin peer 降级路径：未收到 `tags_sync` 时使用 `ClientConfig.peers[].tags` 静态配置
- [x] 多候选混合策略（广播探测 → 定向追问 → 无响应转影子联络）
- [x] 职能标签复用主项目 `Member.tags`（已开发完成），不在通信模块自管标签存储
- [x] 标签维护走 `POST/PATCH /v1/team/members`，`GET /v1/team/tags` 返回系统所有标签

## 对话持久化与恢复（阶段七）
- [x] ConversationLog 新增对话状态标记（active / paused / timeout_disconnect / resumed）
- [x] 对话超时检测（realtime_session_timeout 默认 600s）
- [x] 任一方下线时对话状态持久化（标记 paused）
- [x] peer 上线时自动恢复对话上下文（基于 in_reply_to 链重建）
- [x] 对话恢复后新消息接续原回复链
- [x] MCP 新增 `resume_conversation` 工具

## 梦境提炼集成（阶段七）
- [x] PersonalDistill.run_light 读取 ConversationLog 作为 session 输入
- [x] Light 阶段筛选跨职能协作信号 + needs_human_review 事件加权
- [x] REM 阶段归纳对话意图（如"反复向运维询问测试环境登录"）
- [x] Deep 阶段产出写入 DREAMS.md
- [x] 提炼产出回灌为 `Member.tags` 标签（通过 `PATCH /v1/team/members/{id}`）
- [x] 梦境提炼按现有 distill_schedule_cron 触发，不额外新建流程

## 测试与回归
- [x] 单元测试覆盖各模块核心分支
- [x] 单元测试覆盖在线实时通信路径
- [x] 单元测试覆盖影子联络端到端流程
- [x] 集成测试覆盖双 peer 在线实时通信全流程
- [x] 集成测试覆盖双 peer 影子联络全流程
- [x] 集成测试覆盖在线 → 离线路径切换（会话中途 peer 下线）
- [x] 拓扑切换回归测试通过
- [x] 不删除或破坏现有功能
- [x] 所有新增模块位于 `server/` 下独立子目录
- [x] 阶段七：按 tag 路由 + 多候选混合策略单元测试
- [x] 阶段七：对话持久化与恢复端到端测试
- [x] 阶段七：对话超时断开 + 自动恢复测试
- [x] 阶段七：梦境提炼集成测试（ConversationLog → DREAMS.md）
- [x] 阶段七：MCP ask_peer 按 tag 路由 + resume_conversation 工具测试
