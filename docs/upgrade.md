# TeamHarness 升级流程文档（SubTask 3.6）

> 适用版本：1.0.0 及以上。本文档覆盖单机模式、All-in-One 模式、docker-compose 模式的升级流程，
> 含迁移脚本使用、回滚步骤、frontmatter schema 兼容性说明、API 版本切换引导。

---

## 0. 升级前必读

### 0.1 升级铁律

1. **升级前必须备份**（单机 / All-in-One 模式）。无备份不允许升级。
2. **生产环境先在 staging 验证**。任何 minor / major 升级必须在 staging 跑通完整流程后再上生产。
3. **不在周五下班后升级**。升级窗口建议工作日上午，留出半天排查时间。
4. **每次只跨一个 minor 版本**。1.0.0 → 1.5.0 应依次走 1.0.0 → 1.1.0 → … → 1.5.0，禁止跳跨。
5. **major 版本升级必须读 Release Notes**。破坏性变更需客户端配合改造。

### 0.2 版本号语义

TeamHarness 采用语义化版本（SemVer）：

- **major（如 1.x → 2.x）**：破坏性变更。API / frontmatter schema / DB schema 不兼容。
  - API 走 `/v2/`，`/v1/` 保留至少 1 个版本周期（默认 6 个月）
  - frontmatter schema_version 升级，旧版本数据由 `SchemaMigrator` 自动迁移
  - DB schema 不可降级（major 版本不提供 down_fn）
- **minor（如 1.0 → 1.1）**：兼容性新增。API 只允许追加字段，frontmatter 加字段带默认值。
- **patch（如 1.0.0 → 1.0.1）**：缺陷修复。无 schema / API 变更。

---

## 1. 升级流程总览

```
当前版本 → [备份] → [pre_upgrade_check] → [停止服务] → [替换二进制/镜像]
  → [migrate --from <当前> --to <目标>] → [post_upgrade_check] → [启动服务] → [验证]
                                                                    ↓ 失败
                                                              [回滚到备份]
```

### 1.1 单机模式升级（single-machine）

```bash
# 1. 备份（铁律：无备份不升级）
python -m server.deploy.backup \
    --sqlite-path /var/lib/teamharness/data/teamharness.db \
    --repo-path /var/lib/teamharness/repo \
    --output-dir /var/lib/teamharness/backups \
    --retention 7

# 2. 升级前自检
python -m server.deploy.migrations pre-check

# 3. 停止服务
systemctl stop teamharness

# 4. 替换二进制 / 更新代码
#    方式 A：pip install --upgrade teamharness==1.1.0
#    方式 B：git pull && pip install -r requirements.txt

# 5. 执行迁移（先 dry-run 看影响）
python -m server.deploy.migrations --from-version 1.0.0 --to-version 1.1.0 --dry-run
python -m server.deploy.migrations --from-version 1.0.0 --to-version 1.1.0

# 6. 升级后自检
python -m server.deploy.migrations post-check

# 7. 启动服务
systemctl start teamharness

# 8. 验证（curl /v1/system/info，确认 version 已更新）
curl http://localhost:8080/v1/system/info | jq .version
```

### 1.2 All-in-One 单二进制升级（all-in-one）

```bash
# 1. 备份
TEAMHARNESS_DATA_DIR=/var/lib/teamharness/data \
python -m server.deploy.backup \
    --sqlite-path /var/lib/teamharness/data/teamharness.db \
    --repo-path /var/lib/teamharness/repo \
    --output-dir /var/lib/teamharness/backups

# 2. 下载新版本单二进制
curl -L -o teamharness-all-in-one.new \
    https://github.com/your-org/teamharness/releases/download/v1.1.0/teamharness-all-in-one-linux-amd64
chmod +x teamharness-all-in-one.new

# 3. 停止旧进程
kill $(cat /var/run/teamharness.pid)  # 或按 PID 终止

# 4. 替换二进制
mv teamharness-all-in-one.new teamharness-all-in-one

# 5. 启动（内嵌迁移会自动执行）
./teamharness-all-in-one --data-dir /var/lib/teamharness/data &

# 6. 验证
curl http://localhost:8080/v1/system/info | jq .version
```

### 1.3 docker-compose 模式升级

```bash
# 1. 备份 PG（卷快照或 pg_dump）
docker compose exec postgres pg_dump -U teamharness teamharness > backup-$(date +%Y%m%d).sql

# 2. 拉取新镜像
docker compose pull

# 3. 滚动重启（先 asset → recall → distill，每步验证）
docker compose up -d --no-deps asset-service
docker compose up -d --no-deps recall-service
docker compose up -d --no-deps distill-service

# 4. 执行迁移（在 asset-service 容器内）
docker compose exec asset-service python -m server.deploy.migrations \
    --from-version 1.0.0 --to-version 1.1.0

# 5. 升级后自检
docker compose exec asset-service python -m server.deploy.migrations post-check

# 6. 验证
curl http://localhost:8080/v1/system/info | jq .version
```

---

## 2. 迁移脚本框架

### 2.1 框架位置

```
server/deploy/migrations.py
├── Migration              # 迁移声明 dataclass
├── MigrationKind          # 迁移类型枚举（schema/data/config/api/index）
├── MigrationContext       # 执行上下文（db / config / data_dir / state）
├── MigrationRegistry      # 注册表（按 from_version 索引）
├── register_migration     # 装饰器（注册到全局 REGISTRY）
├── migrate()              # 主执行函数
├── pre_upgrade_check      # 升级前自检
├── post_upgrade_check     # 升级后自检
└── main()                 # CLI 入口
```

### 2.2 注册自定义迁移

各 Agent 在自己域内通过装饰器注册迁移，无需修改 `migrations.py`：

```python
# server/infra_db/migrations_v1_1.py
from server.deploy.migrations import MigrationKind, register_migration

@register_migration(
    "1.0.0",
    "1.1.0",
    kind=MigrationKind.SCHEMA,
    description="新增 module_stats 表",
)
def migrate_1_0_0_to_1_1_0(context):
    cursor = context.db.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS module_stats (
            module_path TEXT PRIMARY KEY,
            asset_count INTEGER DEFAULT 0,
            submodule_count INTEGER DEFAULT 0,
            last_synced_at TEXT
        )
    """)
    context.db.commit()
```

注册后，`python -m server.deploy.migrations --list` 会自动列出，`migrate()` 会自动应用。

### 2.3 迁移类型

| 类型 | 用途 | 责任 Agent |
|------|------|-----------|
| `SCHEMA` | DB 表结构变更（与 Alembic 互补） | Agent 2 |
| `DATA` | 数据迁移（如 frontmatter 字段补全） | 各资产域 |
| `CONFIG` | 配置文件迁移（.teamharness/*.yaml） | Agent 3 |
| `API` | API 版本切换引导（v1 → v2） | Agent 3 + 调用方 |
| `INDEX` | 索引重建（embedding 模型切换后） | Agent 2 |

### 2.4 幂等与断点续传

- **幂等**：迁移函数必须可重复执行。已应用步骤通过 `context.last_applied_version` 跳过。
- **断点续传**：失败时记录 `MigrationResult.failed_at`，重启后从此 from_version 继续。
- **dry-run**：`--dry-run` 只打印不执行，用于预演。

### 2.5 降级策略

- **patch / minor 版本**：不支持自动降级（DB schema 已变更）。回滚靠**备份恢复**。
- **major 版本**：可选提供 `down_fn`（破坏性变更的逆操作）。但建议**永远靠备份恢复**，不用 down_fn。

---

## 3. frontmatter schema_version 兼容解析

### 3.1 兼容原则

- **旧版本可读**：v1 frontmatter 在 v2 代码下必须可解析。`SchemaMigrator` 自动补默认字段。
- **不删字段**：旧字段在新版本中保留（即使不再使用），避免破坏旧客户端。
- **顺序迁移**：v1 → v2 → v3 → current，不可跳跨。

### 3.2 当前 schema_version 历史

| schema_version | 引入版本 | 新增字段 | 默认值 |
|----------------|---------|---------|--------|
| 1 | 1.0.0 | id / type / owner / scope / tags / version / related_to | — |
| 2 | （未发布） | module_path / category | "" / None |
| 3 | （未发布） | distillation_metadata | {score:0, confidence:null, cold_start:false, source_refs:[]} |

### 3.3 解析示例

```python
from server.deploy.schema_version import parse_asset_frontmatter

# 解析旧 v1 frontmatter（缺失 schema_version 自动视为 1）
content = """\
---
id: rule-backend-lint
type: rule
owner: alice
scope: team
tags: [backend, lint]
version: 1.0.0
---

# 后端 lint 规范
"""
fm, body, trace = parse_asset_frontmatter(content)
# fm 包含所有 v1 字段，schema_version=1
# 若 SCHEMA_VERSION_CURRENT >= 2，trace 会显示迁移轨迹
```

### 3.4 注册自定义迁移

```python
from server.deploy.schema_version import register_migration

@register_migration(3, 4, "v4: 新增 adoption_rate 字段")
def _migrate_v3_to_v4(data):
    data.setdefault("adoption_rate", 0.0)
    data["schema_version"] = 4
    return data
```

---

## 4. API 语义化版本

### 4.1 路由策略

- `/v1/*`：**锁定版本**。只允许追加兼容字段，禁止删字段或改语义。
  - 违反时 `assert_backward_compatible()` 抛 500 错误（CI 阶段拦截）
- `/v2/*`：**破坏性变更版本**。允许改签名、删字段、改语义。
  - v2 与 v1 并存至少 1 个版本周期（默认 6 个月）
- 未带版本前缀的请求自动重定向到默认版本（v1）。
  - 例：`POST /recall/list` → `POST /v1/recall/list`

### 4.2 客户端切换 v2

```python
# 客户端代码：显式调用 v2 端点
import httpx
resp = httpx.post(
    "http://server:8080/v2/recall/list",  # 注意 /v2/ 前缀
    json={"agent_id": "builder-01", "query": "lint rule"},
    headers={"X-API-Key": "..."},
)
```

### 4.3 弃用流程

1. **T+0**：v2 发布，v1 进入"弃用预告"（响应头 `Deprecation: true`，`Sunset: <date>`）。
2. **T+3 个月**：v1 标记为 sunset（响应头 `Sunset` 携带具体日期）。
3. **T+6 个月**：v1 下线，返回 410 Gone + 升级文档链接。

---

## 5. 回滚流程

### 5.1 单机 / All-in-One 模式回滚

```bash
# 1. 停止服务
systemctl stop teamharness

# 2. 列出备份
python -m server.deploy.backup list --output-dir /var/lib/teamharness/backups

# 3. 从备份恢复（覆盖现有数据）
python -m server.deploy.backup restore \
    --backup-path /var/lib/teamharness/backups/teamharness-backup-20250101-030000.tar.gz \
    --sqlite-dest /var/lib/teamharness/data/teamharness.db \
    --repo-dest /var/lib/teamharness/repo \
    --overwrite

# 4. 降级二进制 / 代码到升级前版本
git checkout v1.0.0  # 或重新下载旧版本单二进制

# 5. 启动服务
systemctl start teamharness

# 6. 验证版本回退
curl http://localhost:8080/v1/system/info | jq .version  # 应为 1.0.0
```

### 5.2 docker-compose 模式回滚

```bash
# 1. 从 pg_dump 恢复
cat backup-20250101.sql | docker compose exec -T postgres psql -U teamharness teamharness

# 2. 切换镜像 tag 到旧版本
# 编辑 .env：TEAMHARNESS_VERSION=1.0.0
docker compose pull
docker compose up -d

# 3. 验证
curl http://localhost:8080/v1/system/info | jq .version
```

---

## 6. 边界场景

### 6.1 跨多个版本升级（1.0.0 → 1.5.0）

```bash
# 错误：直接跨版本
python -m server.deploy.migrations --from-version 1.0.0 --to-version 1.5.0
# MigrationRegistry.chain() 会自动串联中间版本，但建议分步执行以便验证

# 正确：分步
for target in 1.1.0 1.2.0 1.3.0 1.4.0 1.5.0; do
    python -m server.deploy.migrations --from-version $(prev) --to-version $target
    python -m server.deploy.migrations post-check
done
```

### 6.2 部分迁移失败

```bash
# 迁移在 1.2.0 → 1.3.0 失败
python -m server.deploy.migrations --from-version 1.0.0 --to-version 1.3.0
# 输出：迁移失败：1.2.0 → 1.3.0，错误：...

# 修复后从此处续传（已应用的 1.0.0/1.1.0/1.2.0 会跳过）
python -m server.deploy.migrations --from-version 1.0.0 --to-version 1.3.0
```

### 6.3 frontmatter 跨版本数据混合

同一仓库内可能存在不同 schema_version 的资产（成员升级不同步）。
`parse_asset_frontmatter()` 逐条解析时自动迁移到当前版本，不影响 git 协作。

### 6.4 All-in-One 与 docker-compose 互转

- **All-in-One → docker-compose**：导出 SQLite 数据 → `pg_loader` 导入 PG → 切换模式环境变量
- **docker-compose → All-in-One**：`pg_dump` → 转换为 SQLite insert 脚本 → 启动 All-in-One

互转工具不在本框架范围内，需单独脚本。

### 6.5 升级期间 webhook 处理

升级期间服务停止，Gitea webhook 会失败重试。Gitea 默认重试 5 次（间隔递增），
服务恢复后下一次 push 会触发新的 webhook；中间丢失的 push 由 `reconciliation cron`
（每 5 分钟）补偿。

---

## 7. 升级检查清单

升级前确认（全部 ✅ 才允许升级）：

- [ ] 已读 Release Notes，了解所有 breaking changes
- [ ] 已在 staging 环境跑通完整升级流程
- [ ] 已执行备份（单机 / All-in-One）或 pg_dump（docker-compose）
- [ ] 已通知所有客户端使用者升级窗口
- [ ] `pre_upgrade_check` 全部 PASS
- [ ] 已确认回滚方案（备份路径 / 旧版本 tag）

升级后确认（全部 ✅ 才算升级成功）：

- [ ] `post_upgrade_check` 全部 PASS
- [ ] `/v1/system/info` 返回的 version 与目标一致
- [ ] 召回接口冒烟测试通过（`POST /v1/recall/list` 返回 200）
- [ ] webhook 接收测试通过（推送测试 commit，asset-service 收到事件）
- [ ] 客户端 `teamharness sync` 冒烟通过
- [ ] 监控指标正常（Prometheus / Grafana 无异常告警）

---

## 8. 联系与升级支持

- 升级遇到问题：先查 `/var/log/teamharness/*.log` 与 `MigrationResult.error`
- 不可恢复的故障：从备份恢复，联系 oncall
- 文档反馈：在仓库提交 PR 修改本文档
