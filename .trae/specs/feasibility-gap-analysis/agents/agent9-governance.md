# Agent 9: governance（治理与可观测性）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
Agent 2（module_stats/recall_log）、Agent 8（distillation_job）

## 职责
- PR Review 语义去重（≥0.92 相似度，LLM 判断归并 vs 独立）
- 语义归并（移入 archive/<date>/，6 个月后删除文件）
- 治理看板（模块资产数/拆分建议/未登记告警/召回命中率/采纳率）
- 拆分判定（基于 asset_index 实时查询，非人维护 counts）
- 指标采集（Prometheus + Grafana，客户端 /v1/metrics 批量上报）
- 指标文档化（指标名|采集组件|埋点位置|标签|聚合方式|告警阈值）
- 采纳率服务端可采（recall 次数 + read 次数，客户端上报作辅助）
- 过期归档（长期未引用资产）
- Owner 接管流程
- 仓库大小告警（500MB 阈值）
- module_stats 从 asset_index 实时派生（不依赖人维护 counts）
- teamharness index reconcile 命令

**含缺陷修复**：6.1 指标落地、6.3 采纳率服务端可采、8.1 counts 派生

## 占位 API 契约

### 本 Agent 提供的 API
```
GovernanceService:
  POST /v1/review/dedup (pr_id, assets) → {duplicates, suggestions}
  GET /v1/governance/dashboard → {module_stats, split_suggestions, alerts}
  POST /v1/metrics (batch) → ack (客户端上报)
  GET /v1/metrics/dashboard → Grafana 嵌入
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
- Agent 2 提供：
  ```
  AssetIndex: upsert(asset) / delete(asset_id) / query(filter) / get_status(asset_id)
  ```
  （module_stats/recall_log 表由 Agent 2 schema 提供）
- Agent 8 提供：
  ```
  TeamDistill:
    get_job_status(job_id) → {status, snapshot_sha, progress}
  ```
  （distillation_job 表由 Agent 8 维护）

## SubTask 列表
- [ ] Task 9: 治理与可观测性
  - [ ] SubTask 9.1: PR Review 语义去重（≥0.92 相似度，LLM 判断归并 vs 独立）
  - [ ] SubTask 9.2: 语义归并（移入 archive/<date>/，6 个月后删除文件）
  - [ ] SubTask 9.3: 治理看板（模块资产数/拆分建议/未登记告警/召回命中率/采纳率）
  - [ ] SubTask 9.4: 拆分判定（基于 asset_index 实时查询，非人维护 counts）
  - [ ] SubTask 9.5: 指标采集（Prometheus + Grafana）
  - [ ] SubTask 9.6: 客户端 /v1/metrics 批量上报端点
  - [ ] SubTask 9.7: 指标文档化（10 个核心指标定义）
  - [ ] SubTask 9.8: 采纳率服务端可采（recall + read 次数，客户端上报作辅助）
  - [ ] SubTask 9.9: adoption_rate stale 标记（连续 7 天无上报）
  - [ ] SubTask 9.10: 过期归档 + Owner 接管流程
  - [ ] SubTask 9.11: module_stats 从 asset_index 实时派生
  - [ ] SubTask 9.12: teamharness index reconcile 命令
  - [ ] SubTask 9.13: 域内测试（去重 + 归并 + 看板数据 + 指标采集 + 归档）

## 域内验证点
- [ ] PR Review 语义去重（≥0.92 相似度）检测正确
- [ ] LLM 判断"同一规则不同表述" vs "相似但独立"准确
- [ ] 语义归并后原资产移入 archive/<date>/
- [ ] 治理看板展示模块资产数/拆分建议/未登记告警/召回命中率/采纳率
- [ ] 拆分判定基于 asset_index 实时查询（非人维护 counts）
- [ ] Prometheus 指标可查询
- [ ] Grafana 看板可访问
- [ ] 客户端 /v1/metrics 批量上报端点可用
- [ ] 10 个核心指标有文档化定义
- [ ] 采纳率基于服务端可采集信号（recall + read 次数）
- [ ] 连续 7 天无客户端上报时 adoption_rate 标记 stale
- [ ] 过期归档（长期未引用资产）可用
- [ ] Owner 接管流程可用
- [ ] module_stats 从 asset_index 实时派生
- [ ] teamharness index reconcile 命令自动重算 counts

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文

## 领域经验

> 以下为 Agent 9（governance 治理与可观测性）实现过程中沉淀的领域经验，供后续维护参考。
> 通用工程 gotchas 见 `.trae/rules/gotchas.md`，此处仅记录 governance 域内知识。

### PR Review 三阶段去重流程（SubTask 9.1）
去重按"低成本 → 高成本"三阶段串行，每阶段命中即提前返回：
1. **内容级精确匹配**（content_hash 完全一致）→ `merge`，无需 LLM
2. **语义级候选检索**（embedding 余弦相似度）→ 仅同 type + active 资产入候选；无候选 → `keep_separate`
3. **LLM 判断**（仅 ≥0.92 候选触发）→ `merge` / `independent` / `needs_review`（LLM 不可用降级）

默认建议值矩阵：
| 场景 | 建议值 |
|------|--------|
| 内容为空 | `skip` |
| content_hash 精确匹配 | `merge` |
| 无语义候选 | `keep_separate` |
| 有候选但无一 ≥0.92 | `keep_separate`（**非 needs_review**） |
| LLM 全 merge | `merge` |
| LLM 全 independent | `keep_separate` |
| LLM 混合 / 不可用 / 失败 | `needs_review` |

### module_stats 实时派生（SubTask 9.4 + 9.11）
🔴 **红线**：`actual_asset_count` / `actual_submodule_count` 必须从 `asset_index` 表实时聚合（`SELECT COUNT` + `DISTINCT module_path`），**禁止依赖 `ModuleStats.declared_*` 人维护值**。`declared_*` 仅作对账镜像，`counts_consistent` 比对两者不一致时触发告警。
- `actual_submodule_count` 统计：按 `module_path + "/"` 前缀匹配，`rest.split("/", 1)[0]` 取第一段为子模块名，直接子层与孙层第一段自动合并去重。

### 采纳率服务端采集 + stale 标记（SubTask 9.8 + 9.9）
🔴 **红线**：采纳率必须服务端可采集，客户端上报仅作辅助。
- `recall_count_30d`：从 `recall_log` 表聚合，`query=''` 区分 read 事件（空 query）vs list 事件（非空 query）
- `adoption_rate = read_count_30d / recall_count_30d`（无召回 → 0）
- `stale` 判定：连续 7 天无 `adoption_event` 客户端上报 → `stale=True`；有近 7 天上报 → `False`
- 客户端事件基于 `event_id` 幂等去重（重试不重复计数）

### 语义归档（SubTask 9.2）
🟡 **风险项**：归档移入 `archive/<YYYY-MM-DD>/<asset_id>.md`，清单写 `_manifest.json`；TTL 180 天（6 个月）后由 `cleanup_expired` 删归档文件。
- **禁止直接删除原 git_path 文件**：归档时只置 `asset_index.status='deleted'`，原文件由 git 历史保留
- 归档幂等：同 asset_id + 同日期重复归档不报错
- 清单原子写：先写临时文件再 rename，避免半写状态

### Prometheus 可选依赖降级（SubTask 9.5 + 9.6）
`prometheus_client` 未在 `pyproject.toml` dependencies 中，运行时 `try: import prometheus_client; except ImportError:` 降级为 `_NoOpMetric` stub。
- Stub 须与真实 `Gauge` / `Counter` / `Histogram` 构造签名兼容（`*args, **kwargs`）
- `/v1/metrics/prometheus` 端点在 stub 模式下返回空字节（200 OK），不抛 500

### reconcile 对账（SubTask 9.12）
`teamharness index reconcile` 命令比对 `declared_*` vs `actual_*`：
- 一致 → `consistent=True`，不触发动作
- 不一致 → `consistent=False`，调用 `sync_service.reconcile()` 重算 declared counts
- `reconcile_and_fix` 强制重算（即使一致也重算）

### 看板聚合（SubTask 9.3）
`DashboardService.get_dashboard()` 聚合多源数据：
- `module_stats`：每模块资产数 / 子模块数 / by_type / by_status
- `split_suggestions`：资产数超阈值（>50）的模块
- `orphan_asset_alerts`：asset_index 有资产但 module_path 不在 declared ModuleStats 中
- `recall_hit_rates`：每模块召回命中率（recall_log 聚合）
- `adoption_rates`：每资产采纳率
- `repo_size_alerts`：仓库大小超 500MB 告警

### 过期归档 + Owner 接管（SubTask 9.10）
- `archive_stale_assets`：90 天无 recall_log → 归档（移入 archive/<date>/）
- `owner_takeover`：Owner 接管时更新 asset_index.owner 字段，保留原 owner 记录
- `check_repo_size`：扫描仓库目录大小（忽略 .git / node_modules / __pycache__ / .venv / venv），超 500MB 告警
