# Agent 7: distill-personal（一级提炼）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 6（client）、Agent 2（LLM Provider 接入）

## 职责
- SessionProvider 抽象（Trae 适配 + 通用 JSONL 兜底）
- 对话记录增量采集
- Light 阶段（信号筛选 + L0→L1 原子事实抽取）
- REM 阶段（意图归纳，区分一次性上下文 vs 可复用经验）
- Deep 阶段（五维评分 + 结构化固化，产出带 frontmatter 资产）
- 四类资产子 Prompt 模板（rule/memory/skill/tool）
- LLM 默认走服务端代理（统一计费与模型版本）
- 每成员 daily_token_budget + 超限降级（Deep 跳过，候选入 pending）
- Light 阶段候选信号计数上报（预算动态调整）
- 隐私保护（对话不离开本机，只上传结构化资产）
- LLM 强制 JSON schema 输出 + schema 校验失败重试
- cost estimate 命令实现

**含缺陷修复**：2.1 LLM 成本归属、5.2 提示词跨模型一致性（一级提炼部分）

## 占位 API 契约

### 本 Agent 提供的 API
```
PersonalDistill:
  run_light(sessions) → signals
  run_rem(signals) → intents
  run_deep(intents, budget) → {assets, pending}
  report_metrics(member_id, signal_count, yield_ratio)
LLMProvider (服务端代理):
  POST /v1/llm/chat (messages, schema) → {content, usage}
  GET /v1/llm/budget (member_id) → {daily_token_budget, used}
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 6 提供（SessionProvider 接入、守护进程调度一级提炼）：
  ```
  ClientDaemon:
    定时一级提炼调度 / 网络状态检测 / 采纳率批量上报
  ```
- Agent 2 提供（资产写入）：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  ```

## SubTask 列表
- [x] Task 7: 一级提炼引擎
  - [x] SubTask 7.1: SessionProvider 抽象（Trae 适配 + 通用 JSONL 兜底 + discover_sessions_root）
  - [x] SubTask 7.2: 对话记录增量采集
  - [x] SubTask 7.3: Light 阶段（信号筛选 + L0→L1 原子事实抽取）
  - [x] SubTask 7.4: REM 阶段（意图归纳，区分一次性上下文 vs 可复用经验）
  - [x] SubTask 7.5: Deep 阶段（五维评分 + 结构化固化，产出带 frontmatter 资产）
  - [x] SubTask 7.6: 四类资产子 Prompt 模板（rule/memory/skill/tool）
  - [x] SubTask 7.7: LLM 服务端代理接入（POST /v1/llm/chat + GET /v1/llm/budget）
  - [x] SubTask 7.8: 每成员 daily_token_budget + 超限降级（Deep 跳过，候选入 pending）
  - [x] SubTask 7.9: Light 阶段候选信号计数上报
  - [x] SubTask 7.10: LLM 强制 JSON schema 输出 + 校验失败重试
  - [x] SubTask 7.11: cost estimate 命令实现
  - [x] SubTask 7.12: 隐私保护（对话不离开本机）
  - [x] SubTask 7.13: 域内测试（三阶段提炼 + 预算超限 + JSON schema 校验 + 四类资产）

## 域内验证点
- [x] SessionProvider Trae 适配可用（读 .trae-cn/sessions/*.jsonl）
- [x] SessionProvider 通用 JSONL 兜底可用
- [x] discover_sessions_root 按 OS 自动探测
- [x] 对话记录增量采集（只处理新会话）
- [x] Light 阶段信号筛选 + L0→L1 原子事实抽取
- [x] REM 阶段意图归纳正确
- [x] Deep 阶段五维评分 + 结构化固化产出带 frontmatter 资产
- [x] 四类资产子 Prompt 模板（rule/memory/skill/tool）各有提炼
- [x] LLM 默认走服务端代理（统一计费与模型版本）
- [x] 每成员 daily_token_budget 配置生效
- [x] 超预算时 Deep 跳过，候选写入 .dreams/pending/
- [x] 次日预算恢复后 pending 候选被处理
- [x] Light 阶段候选信号计数上报服务端
- [x] LLM 强制 JSON schema 输出
- [x] schema 校验失败时重试或降级小模型
- [x] cost estimate 命令输出成本估算
- [x] 对话记录不离开本机（只上传结构化资产）

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文

## 领域经验
> 本 Agent 修复 16 个测试失败提炼的经验，已由 L1 主 Agent 多源聚合后固化到 `.trae/rules/gotchas.md`，此处登记追溯链：

1. **_FakeResponse 缺 .json() 方法**：mock HTTP 响应对象须实现真实类全部被调用的方法 → 聚合到 gotchas「测试 → 测试构造物语义 → mock 须模拟真实语义」
2. **Signal 缺 content_excerpt 参数**：接口契约变更后所有调用方/测试须同步 → 聚合到 gotchas「数据结构 → 参数传递完整性 → 接口契约同步（契约层）」
3. **PersonalDistill._get_deep_stage 忽略注入的 deep_stage**：注入参数必须被实际使用，禁止"接受后忽略" → 聚合到 gotchas「数据结构 → 参数传递完整性 → 注入参数必须实际使用（逻辑层）」
4. **Windows mtime 测试不可靠**：用 os.utime() 显式设置确定性时间戳 → 固化到 gotchas「测试 → Windows mtime 测试不可靠」
5. **dataclass 无默认值字段是测试陷阱**：测试构造时只设原始字段，不传 @property 派生值 → 聚合到 gotchas「数据结构 → dataclass 字段语义四准则 → 测试构造只设原始字段」
`[来源: Agent 7 / 第三波 / L1 多源聚合回测通过]`
