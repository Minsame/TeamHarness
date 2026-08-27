---
id: seed-pr-review-checklist
category: rule-backend
scenario: PR Review 阶段统一检查清单
title: PR Review 通用检查清单
tags: [pr-review, lint, checklist]
---

# PR Review 通用检查清单

## 必查项

1. **测试覆盖**：新增/修改逻辑是否有对应测试？是否覆盖边界值与异常输入？
2. **错误处理**：异常路径是否被捕获？是否有静默失败？
3. **安全**：是否引入硬编码密钥/SQL 注入/路径穿越？
4. **性能**：是否在循环中执行 N+1 查询？是否有不必要的全量加载？
5. **可读性**：命名是否语义清晰？是否有过度抽象？

## 禁止项

- 不得在 main 分支直接提交
- 不得绕过 CI 强制门禁
- 不得删除已有测试用例（除非该功能已废弃）

## 建议项

- 复杂逻辑应附 inline 注释说明 why，而非 what
- 公共 API 变更应同步更新文档
