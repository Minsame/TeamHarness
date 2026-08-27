# Agent 8: distill-team（二级提炼）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 2（AssetIndex）、Agent 4（recall_log 统计）

## 职责
- Light 阶段增量聚类（只处理新增/修改资产，全量聚类每周 cron）
- REM 阶段跨成员模式识别
- Deep 阶段六维评分（频率/来源多样性/泛化性/稳定性/可操作性/信噪比）
- 晋升门禁（冷启动期动态化：资产 < 50 时来源多样性 ≥ 2）
- 冷启动产出标记 confidence: low + cold_start: true
- 种子 Prompt 库（prompts/seeds/）
- is_convention=true 单成员旁路
- 二级提炼 Prompt 模板（6 步推理链 + SKIP 机制 + 反例检验）
- LLM 强制 JSON schema + SKIP 审查区（每周人工抽查 10%）
- 模型一致性测试集（20 条标准资产簇）
- 反向验证基线（冷启动用公开 Prompt 数据集）
- job 快照隔离（启动时快照 commit SHA，完成后增量 delta）
- distillation_job 表 trigger_source/cluster_fingerprint
- 采纳率降级（近 30 天 recall < 1 → 自动降级）
- DREAMS.md 审查界面数据

**含缺陷修复**：2.3 增量聚类、5.1 冷启动旁路、5.2 提示词一致性（二级提炼部分）、5.3 提炼 job 竞态

## 占位 API 契约

### 本 Agent 提供的 API
```
TeamDistill:
  trigger_incremental() → job_id
  trigger_full() → job_id (cron 周日)
  get_job_status(job_id) → {status, snapshot_sha, progress}
  get_cold_start_progress() → {assets_needed, current_count}
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 2 提供：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  EmbeddingService: embed(text) / embed_batch(texts) / get_active_version()
  ```
- Agent 4 提供（recall_log 统计）：
  ```
  RecallService:
    GET /v1/sync/status
      → {last_synced_commit, lag_seconds, sync_source}
  ```
  （recall_log 统计信号由 Agent 2 recall_log 表 + Agent 4 写入）

## SubTask 列表
- [ ] Task 8: 二级提炼引擎
  - [ ] SubTask 8.1: Light 增量聚类（只处理新增/修改资产，向量检索 top-K 匹配）
  - [ ] SubTask 8.2: 全量聚类（每周日凌晨 cron）
  - [ ] SubTask 8.3: REM 跨成员模式识别
  - [ ] SubTask 8.4: Deep 六维评分 + 晋升门禁
  - [ ] SubTask 8.5: 冷启动旁路（资产 < 50 时门禁 ≥ 2，标记 cold_start: true）
  - [ ] SubTask 8.6: 种子 Prompt 库（prompts/seeds/ 预置常见场景）
  - [ ] SubTask 8.7: is_convention=true 单成员旁路
  - [ ] SubTask 8.8: 二级提炼 Prompt 模板（6 步推理链 + SKIP 机制 + 反例检验）
  - [ ] SubTask 8.9: LLM 强制 JSON schema + SKIP 审查区写入 DREAMS.md
  - [ ] SubTask 8.10: 模型一致性测试集（20 条标准资产簇）
  - [ ] SubTask 8.11: 反向验证基线（冷启动用公开 Prompt 数据集）
  - [ ] SubTask 8.12: job 快照隔离（启动时快照 commit SHA，完成后增量 delta）
  - [ ] SubTask 8.13: distillation_job 表 trigger_source/cluster_fingerprint
  - [ ] SubTask 8.14: 采纳率降级（近 30 天 recall < 1 → 自动降级）
  - [ ] SubTask 8.15: DREAMS.md 审查界面数据接口
  - [ ] SubTask 8.16: 域内测试（增量聚类 + 冷启动 + SKIP + 快照隔离 + 采纳率降级）

## 域内验证点
- [ ] Light 增量聚类只处理新增/修改资产
- [ ] 全量聚类每周日凌晨 cron 运行
- [ ] REM 跨成员模式识别
- [ ] Deep 六维评分（频率/来源多样性/泛化性/稳定性/可操作性/信噪比）
- [ ] 晋升门禁正常模式：来源多样性 ≥ 3 + 被召回 ≥ 3 次
- [ ] 冷启动期（资产 < 50）门禁降为 ≥ 2
- [ ] 冷启动产出标记 confidence: low + cold_start: true
- [ ] 种子 Prompt 库（prompts/seeds/）存在且可用
- [ ] is_convention=true 单成员旁路触发提炼
- [ ] 二级提炼 Prompt 模板 6 步推理链 + SKIP 机制 + 反例检验
- [ ] LLM 强制 JSON schema 输出
- [ ] SKIP 候选写入 DREAMS.md SKIP 审查区
- [ ] 模型一致性测试集（20 条标准资产簇）存在
- [ ] 反向验证基线冷启动期用公开 Prompt 数据集
- [ ] job 启动时快照 commit SHA，全程基于该 SHA 读取
- [ ] job 完成后比对 HEAD 与快照 SHA，有新 commit 则触发增量 job
- [ ] distillation_job 表有 trigger_source/cluster_fingerprint 字段
- [ ] 近 30 天 recall < 1 时 Prompt 自动降级
- [ ] DREAMS.md 审查界面数据接口可用

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文

## 领域经验

### 1. 向量检索冷启动容错（clustering.py）
**根因**：`_find_neighbors` 原先在 `embedding_id` 为空时直接返回 `[]`，导致冷启动期（embedding 未就绪）聚类完全失效——所有资产都成为孤点，无法形成簇。
**修复**：向量检索无结果时退化为 content 匹配（优先 `content_hash`，其次 `content_snapshot` 精确匹配），相似度固定 1.0。
**通用规则**：任何依赖向量检索的聚类/召回模块，必须有 content-based fallback，否则冷启动期功能不可用。

### 2. dataclass @property 不可作为构造参数（models.py）
**根因**：`SixDimScore.total` 是 `@property`（加权计算），不是 dataclass 字段。测试中 `SixDimScore(total=0.8)` 会触发 `TypeError`。
**修复**：测试改为只设置六维字段（如 `SixDimScore(source_diversity=0.6)`），total 由 property 自动计算。
**通用规则**：dataclass 中用 `@property` 暴露的派生值（如加权总分）不能出现在构造参数中，测试和调用方都应只设置原始字段。

### 3. 确定性抽样必须用变化的输入（llm_schema.py）
**根因**：`should_human_review(skip_count_this_week=i, seed=42)` 每次都用 `random.Random(42)` 创建新 RNG，所有调用返回同一个随机值（0.639 >= 0.1 → 全 False，true_count=0）。
**修复**：将 `skip_count_this_week` 纳入种子：`random.Random(base + skip_count_this_week)`，确保不同输入产生不同输出。
**通用规则**：确定性抽样函数若需在循环中产生分布，种子必须与循环变量组合，不能只用固定种子。

### 4. None-key 分组陷阱（convention.py）
**根因**：`collect_convention_clusters` 按 `r.category` 分组时，所有 `category=None` 的资产被合并到同一个 `None` 键下，而非各自独立成簇。
**修复**：无 category 的资产用 `f"__individual_{r.id}"` 作为唯一 key，确保各自成簇。
**通用规则**：按可空字段分组时，None 值的语义需显式处理——是"同组"还是"各自独立"取决于业务逻辑，不能用 None 作为 dict key 默认合并。

### 5. 测试集 fixture 交叉场景计数（consistency_test.py）
**根因**：TC-019 同时是 `is_convention=True` 和 `expected_cold_start=True`，但 `test_convention_fixtures` 原先期望 4 个 convention fixture（只数 TC-013~016），实际有 5 个。
**修复**：更新测试期望为 5，注释标明 TC-019 是交叉场景。
**通用规则**：测试集 fixture 若有多个正交标记（convention / cold_start），统计任一标记数量时必须包含交叉场景，不能只数"纯"场景。

### 6. 冷启动 confidence 一致性（consistency_test.py）
**根因**：TC-018 和 TC-020 是冷启动 SKIP 场景，但 `expected_confidence` 默认为 "medium"（TestClusterFixture dataclass 默认值），与"冷启动期所有产出 confidence=low"的规则矛盾。
**修复**：所有 `expected_cold_start=True` 的 fixture 显式设置 `expected_confidence="low"`。
**通用规则**：冷启动标记是全局性的——不论 PROMOTE 还是 SKIP，只要 `cold_start=True`，confidence 必须为 low。dataclass 默认值不能隐式覆盖业务规则。
