# Agent 1: infra-git（仓库与 Git Provider）

## 层级
第1层子agent，可再启动1层子子agent

## 依赖
无

## 职责
- Git Provider 抽象层（GitLab/Gitea/libgit2 切换，含 VectorStore Provider）
- 分层仓库结构（项目级/模块级/子模块级递归）
- INDEX.md 规范与防孤岛 CI 校验
- webhook 接收端点（secret 签名校验）
- Trae 深度适配（frontmatter 双区设计、会话路径自动探测）
- categories.yaml 受控词汇表管理
- DREAMS.md 按月切分与归档
- shallow clone 支持

**含缺陷修复**：4.1 多软件适配收敛、4.3 restricted 读权限（git-crypt/独立仓库）、2.2 仓库 GC

## 占位 API 契约

### 本 Agent 提供的 API
```
GitProvider: fetch(repo) / show(sha, path) / diff(sha_a, sha_b) / ls_tree(sha, path)
WebhookReceiver: POST /v1/webhook/git (接收 GitLab/Gitea webhook)
```

### 本 Agent 依赖的 API（其他 Agent 提供，先写占位函数）
无

## SubTask 列表
- [ ] Task 1: Git Provider 抽象层 + 分层仓库结构
  - [ ] SubTask 1.1: 实现 GitProvider 接口（fetch/show/diff/ls_tree），含 GitLab/Gitea/libgit2 三实现
  - [ ] SubTask 1.2: 实现分层仓库结构（项目级/模块级/子模块级递归 + INDEX.md 规范）
  - [ ] SubTask 1.3: 实现防孤岛 CI 校验脚本（资产文件存在但 INDEX.md 未登记 → 阻断）
  - [ ] SubTask 1.4: 实现 webhook 接收端点（secret 签名校验）
  - [ ] SubTask 1.5: Trae 深度适配（frontmatter 双区设计 + 会话路径自动探测）
  - [ ] SubTask 1.6: categories.yaml 受控词汇表管理 + PR 校验
  - [ ] SubTask 1.7: DREAMS.md 按月切分 + 归档压缩
  - [ ] SubTask 1.8: restricted 读权限（git-crypt 加密目录 或 独立仓库支持）
  - [ ] SubTask 1.9: shallow clone 支持 + 仓库大小告警（500MB）
  - [ ] SubTask 1.10: 域内测试（GitProvider 各实现 + 防孤岛校验 + frontmatter 解析）

## 域内验证点
- [ ] GitProvider 接口三实现（GitLab/Gitea/libgit2）均可 fetch/show/diff/ls_tree
- [ ] 分层仓库结构（项目级/模块级/子模块级）递归正确
- [ ] INDEX.md 规范完整（level/parent/module/assets/submodules/counts）
- [ ] 防孤岛 CI 校验：资产文件存在但 INDEX.md 未登记 → 阻断合入
- [ ] webhook 接收端点校验 secret 签名，拒绝未签名请求
- [ ] Trae frontmatter 双区设计（coding 字段与 teamharness 字段分离，互不干扰）
- [ ] Trae 会话路径自动探测（discover_sessions_root 按 OS 查找）
- [ ] categories.yaml 受控词汇表 PR 校验（两级 <type>-<module>，<module> 须 INDEX.md 登记）
- [ ] DREAMS.md 按月切分（DREAMS/2026-08.md），历史归档压缩
- [ ] restricted 读权限：git-crypt 加密目录或独立仓库，普通 clone 不可见明文
- [ ] shallow clone（--depth=1）支持可用
- [ ] 仓库大小达 500MB 触发告警

## 规则摘要
- 沟通语言：中文
- 未经允许不可执行 git 提交和推送
- 汇报需包含根因分析
- 代码注释用中文
