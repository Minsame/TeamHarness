# Agent 3: deploy（部署与升级）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
无

## 职责
- All-in-One 单二进制（内嵌 SQLite + PGVector + libgit2）
- docker-compose 一键部署（PG + Qdrant + 三服务 + Gitea）
- 单机模式每日 cron 备份
- 升级流程文档与迁移脚本框架
- API 语义化版本（/v1/ 锁定，/v2/ 破坏性）
- frontmatter schema_version 兼容解析

**含缺陷修复**：7.1 单机部署、7.3 升级策略

## 占位 API 契约

### 本 Agent 提供的 API
```
DeployConfig: get_mode() / get_storage_backend() / get_version()
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
无

## SubTask 列表
- [ ] Task 3: 部署模式 + 升级策略
  - [ ] SubTask 3.1: All-in-One 单二进制（内嵌 SQLite + PGVector + libgit2）
  - [ ] SubTask 3.2: docker-compose 一键部署（PG + Qdrant + 三服务 + Gitea）
  - [ ] SubTask 3.3: 单机模式每日 cron 备份（SQLite + git repo → tar.gz）
  - [ ] SubTask 3.4: API 语义化版本（/v1/ 锁定，/v2/ 破坏性变更）
  - [ ] SubTask 3.5: frontmatter schema_version 兼容解析
  - [ ] SubTask 3.6: 升级流程文档 + 迁移脚本框架
  - [ ] SubTask 3.7: 域内测试（单机模式启动 + docker-compose 启动 + 备份恢复）

## 域内验证点
- [ ] All-in-One 单二进制可独立运行（内嵌 SQLite + PGVector + libgit2）
- [ ] 5 人团队下载单二进制启动后无需外部依赖即可使用
- [ ] docker-compose 一键部署脚本可用（PG + Qdrant + 三服务 + Gitea）
- [ ] 单机模式每日 cron 备份 SQLite + git repo 到 tar.gz
- [ ] API 语义化版本：/v1/ 锁定，破坏性变更走 /v2/
- [ ] frontmatter schema_version 字段兼容解析（旧版本可读）
- [ ] 升级流程文档存在，迁移脚本框架可用

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
