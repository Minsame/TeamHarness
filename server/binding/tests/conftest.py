"""binding 域测试 fixtures。

复用 infra_db 域的 database fixture 模式（SQLite 内存库）。
扩展：注入 binding 域全部 Service（BindingService / CategorySuggestService /
AgentApiKeyService / ToolReviewService）+ Ed25519 密钥对（tool 签名验证用）。
"""

from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

import pytest


# 测试用 SQLite 内存库（与 infra_db 域一致）
TEST_DB_SYNC_URL = "sqlite:///:memory:"
TEST_DB_ASYNC_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture
def database() -> Iterator:
    """提供初始化 schema 的 Database（SQLite 内存库，含 binding 域新表）。"""
    for k in ("DATABASE_URL", "DATABASE_SYNC_URL", "DB_ECHO"):
        os.environ.pop(k, None)
    from server.infra_db.db import create_database
    from server.infra_db.schema_initializer import init_schema

    db = create_database(sync_url=TEST_DB_SYNC_URL, async_url=TEST_DB_ASYNC_URL)
    # init_schema 通过 Base.metadata.create_all 创建全部表（含 binding 域新表）
    init_schema(db.sync_engine, with_pg_ddl=False)
    yield db
    import asyncio

    try:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(db.close())
        loop.close()
    except Exception:
        pass


@pytest.fixture
def binding_service(database):
    """BindingService 实例。"""
    from server.binding.binding_service import BindingService

    return BindingService(database)


@pytest.fixture
def category_service(database, sample_repo):
    """CategorySuggestService 实例（注入 sample_repo + 占位 LLM）。"""
    from server.binding.category_suggest import CategorySuggestService

    return CategorySuggestService(database, repo_root=sample_repo, llm=None)


@pytest.fixture
def category_service_no_repo(database):
    """无 repo_root 的 CategorySuggestService（测试无配置场景）。"""
    from server.binding.category_suggest import CategorySuggestService

    return CategorySuggestService(database, repo_root=None, llm=None)


@pytest.fixture
def auth_service(database):
    """AgentApiKeyService 实例。"""
    from server.binding.auth_service import AgentApiKeyService

    return AgentApiKeyService(database)


@pytest.fixture
def asset_index(database, embedding_service):
    """AssetIndex 实例（复用 infra_db 域 embedding_service 配置）。

    供验证 Agent 2 AssetIndex.delete 内置级联与 BindingService 互补用。
    """
    from server.infra_db.asset_index import AssetIndex

    return AssetIndex(
        database,
        active_embedding_version=embedding_service.get_active_version(),
        shadow_embedding_version=embedding_service.get_shadow_version(),
    )


@pytest.fixture
def embedding_service():
    """EmbeddingService（默认哈希 embedding，测试用）。"""
    from server.infra_db.embedding import EmbeddingService

    return EmbeddingService(active_version="v1", shadow_version="", dim=32)


@pytest.fixture
def ed25519_keypair():
    """生成 Ed25519 密钥对，返回 (private_pem, public_raw)。

    若未安装 cryptography → 跳过该测试。
    """
    pytest.importorskip("cryptography")
    from server.binding.tool_review import generate_ed25519_keypair

    return generate_ed25519_keypair()


@pytest.fixture
def tool_review_service(database, ed25519_keypair):
    """ToolReviewService 实例（注入 trusted_reviewers + 公钥）。"""
    from server.binding.tool_review import ToolReviewService

    _, pub_raw = ed25519_keypair
    return ToolReviewService(
        database,
        trusted_reviewers={"alice", "bob"},
        public_key=pub_raw,
        min_trusted_reviewers=1,
    )


@pytest.fixture
def sample_repo(tmp_path: Path) -> Path:
    """构造带 categories.yaml + INDEX.md 的最小仓库（用于 category 校验）。

    结构：
        repo/
        ├── .teamharness/categories.yaml     (登记 rule-backend / skill-db)
        ├── INDEX.md                          (项目级，登记 module=teamharness-shared)
        ├── rules/global.md                   (资产 rule-global)
        └── modules/
            └── backend/
                ├── INDEX.md                  (模块级，module=backend)
                └── rules/backend.md          (资产 rule-backend)
    """
    repo = tmp_path / "repo"

    # categories.yaml
    (repo / ".teamharness").mkdir(parents=True)
    (repo / ".teamharness" / "categories.yaml").write_text(
        """categories:
  - name: rule-backend
    description: 后端规则
    modules: [backend]
  - name: skill-db
    description: 数据库技能
    modules: [db]
""",
        encoding="utf-8",
    )

    # 项目级 INDEX.md（module=teamharness-shared）
    (repo / "rules").mkdir(parents=True)
    (repo / "rules" / "global.md").write_text(
        "---\nid: rule-global\ntype: rule\ncategory: rule-teamharness-shared\n---\n# 全局规则\n",
        encoding="utf-8",
    )
    (repo / "INDEX.md").write_text(
        """---
level: project
parent: null
module: teamharness-shared
assets:
  - id: rule-global
    path: rules/global.md
    type: rule
    purpose: 全局规则
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

    # 模块级 INDEX.md（module=backend）
    backend = repo / "modules" / "backend"
    (backend / "rules").mkdir(parents=True)
    (backend / "rules" / "backend.md").write_text(
        "---\nid: rule-backend\ntype: rule\ncategory: rule-backend\n---\n# 后端规则\n",
        encoding="utf-8",
    )
    (backend / "INDEX.md").write_text(
        """---
level: module
parent: ../../INDEX.md
module: backend
assets:
  - id: rule-backend
    path: rules/backend.md
    type: rule
    purpose: 后端规则
submodules: []
counts:
  assets: 1
  submodules: 0
---
""",
        encoding="utf-8",
    )
    return repo


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def make_asset(
    *,
    id: str = "asset-1",
    type: str = "rule",
    owner: str = "tester",
    scope: str = "team",
    module_path: str = "",
    category: str | None = None,
    content: str = "test content",
    git_path: str = "rules/test.md",
    version: str = "0.0.1",
):
    """构造 Asset VO（测试用）。"""
    from server.common.models import Asset as AssetVO, AssetType, Scope

    return AssetVO(
        id=id,
        type=AssetType(type),
        owner=owner,
        scope=Scope(scope),
        content=content,
        content_file_ref=git_path,
        module_path=module_path,
        category=category,
        version=version,
    )


def insert_asset(
    database,
    *,
    id: str = "asset-1",
    type: str = "rule",
    owner: str = "tester",
    scope: str = "team",
    module_path: str = "modules/backend",
    category: str = "rule-backend",
    git_path: str = "modules/backend/rules/test.md",
    git_commit: str = "abc123",
    version: str = "0.0.1",
    status: str = "active",
):
    """直接向 asset_index 表插入一行（绕过 AssetIndex.upsert，测试用）。"""
    from datetime import datetime, timezone

    from server.infra_db.models import AssetIndex as AssetIndexRow

    with database.session() as sess:
        row = AssetIndexRow(
            id=id,
            type=type,
            owner=owner,
            scope=scope,
            content_hash=None,
            embedding_id=None,
            active_embedding_version="v1",
            version=version,
            tags="[]",
            related_to="[]",
            git_path=git_path,
            git_commit=git_commit,
            module_path=module_path,
            category=category,
            status=status,
            schema_version=1,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
            indexed_at=datetime.now(timezone.utc),
            content_snapshot="",
        )
        sess.add(row)
    return id
