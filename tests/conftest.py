"""pytest 共享 fixture。"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """构造一个最小可用的分层仓库样本（counts 一致、无孤儿）。

    结构：
        repo/
        ├── INDEX.md              (项目级，登记 backend 模块 + 全局 1 个资产)
        ├── rules/
        │   └── global-lint.md    (登记)
        └── modules/
            └── backend/
                ├── INDEX.md      (模块级，登记 1 个资产)
                ├── rules/
                │   └── backend-lint.md  (登记)
                └── submodules/
                    └── auth/
                        └── INDEX.md  (子模块级，无资产)

    用于覆盖项目级 / 模块级 / 子模块级递归 + counts 一致性。
    防孤岛违规场景见 ``repo_with_orphan`` fixture。
    """
    repo = tmp_path / "repo"

    # 项目级 INDEX.md
    (repo / "rules").mkdir(parents=True)
    (repo / "rules" / "global-lint.md").write_text(
        "---\nid: rule-global-lint\ntype: rule\n---\n# 全局 lint 规则\n", encoding="utf-8"
    )
    (repo / "INDEX.md").write_text(
        """---
level: project
parent: null
module: teamharness-shared
assets:
  - id: rule-global-lint
    path: rules/global-lint.md
    type: rule
    purpose: 全局 lint 规范
submodules:
  - name: backend
    path: modules/backend/
    purpose: 后端模块
counts:
  assets: 1
  submodules: 1
---
""",
        encoding="utf-8",
    )

    # 模块级 INDEX.md
    backend = repo / "modules" / "backend"
    (backend / "rules").mkdir(parents=True)
    (backend / "rules" / "backend-lint.md").write_text(
        "---\nid: rule-backend-lint\ntype: rule\n---\n# 后端 lint\n", encoding="utf-8"
    )
    (backend / "INDEX.md").write_text(
        """---
level: module
parent: ../../INDEX.md
module: backend
assets:
  - id: rule-backend-lint
    path: rules/backend-lint.md
    type: rule
    purpose: 后端 lint 规范
submodules:
  - name: auth
    path: submodules/auth/
    purpose: 认证子模块
counts:
  assets: 1
  submodules: 1
---
""",
        encoding="utf-8",
    )

    # 子模块级 INDEX.md（无资产，counts.assets=0）
    auth = backend / "submodules" / "auth"
    auth.mkdir(parents=True)
    (auth / "INDEX.md").write_text(
        """---
level: submodule
parent: ../../INDEX.md
module: auth
assets: []
submodules: []
counts:
  assets: 0
  submodules: 0
---
""",
        encoding="utf-8",
    )

    return repo


@pytest.fixture
def repo_with_orphan(sample_repo: Path) -> Path:
    """在 sample_repo 基础上新增一个未登记资产（防孤岛违规场景）。

    注：该 fixture 故意制造 counts 不一致（INDEX.md counts.assets=1，
    但实际有 2 个文件），符合"防孤岛 + counts 不一致"双重违规场景。
    """
    (sample_repo / "memory").mkdir(parents=True, exist_ok=True)
    (sample_repo / "memory" / "orphan.md").write_text(
        "---\nid: mem-orphan\ntype: memory\n---\n# 未登记\n", encoding="utf-8"
    )
    return sample_repo


@pytest.fixture
def empty_repo(tmp_path: Path) -> Path:
    """空仓库（无 INDEX.md）。"""
    repo = tmp_path / "empty_repo"
    repo.mkdir()
    return repo


@pytest.fixture(autouse=True)
def _reset_webhook_state():
    """每个 webhook 测试前清理全局状态，保证用例隔离。"""
    from server.infra_git import webhook as wh

    wh._WEBHOOK_SECRET = ""
    wh._SYNC_HANDLER = None
    wh._TRACKER = wh.ProcessedTracker()
    yield
    wh._WEBHOOK_SECRET = ""
    wh._SYNC_HANDLER = None
    wh._TRACKER = wh.ProcessedTracker()
