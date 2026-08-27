---
id: seed-commit-message-convention
category: rule-git
scenario: Git commit message 规范统一
title: Git Commit Message 规范
tags: [git, commit, convention]
---

# Git Commit Message 规范

## 格式

```
<type>(<scope>): <subject>

<body>

<footer>
```

## type 取值

- feat：新功能
- fix：bug 修复
- refactor：重构（无功能变更）
- test：测试相关
- docs：文档
- chore：构建/依赖/杂项

## 规则

1. subject 不超过 50 字符，祈使句（如 "add" 而非 "added"）
2. body 解释 why，不复述 what
3. 破坏性变更在 footer 标注 `BREAKING CHANGE:`
4. 引用 issue 用 `Closes #123` / `Refs #456`

## 禁止项

- 不得用 "update" / "fix bug" 等无信息量 subject
- 不得在一个 commit 混合多个不相关变更
