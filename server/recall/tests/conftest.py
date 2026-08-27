"""recall 域内测试 fixtures。

复用 infra_db 的 database / asset_index / embedding_service / vector_store fixture，
并补：
- mock GitProvider（fetch/show/ls_tree）
- mock SyncService（get_sync_status 返回可控值）
- mock RestrictedReader
- RecallService 实例（注入所有依赖）
- 预置资产 + 装配（asset_index + agent_binding + 向量库）
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import MagicMock

import pytest

# 复用 infra_db 的 conftest 中的 fixture（pytest 自动发现同包 conftest）
# 这里显式 import 以便路径明确
from server.infra_db.tests.conftest import (  # noqa: F401
    TEST_DB_ASYNC_URL,
    TEST_DB_SYNC_URL,
    asset_index,
    counts_checker,
    database,
    embedding_service,
    make_asset,
    outbox_worker,
    sync_service_factory,
    vector_store,
)


# ---------------------------------------------------------------------------
# trace_id 上下文清理（autouse，保证测试隔离）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_trace_id():
    """每个测试前清理 trace_id contextvar，避免跨用例污染。"""
    from server.recall import tracing

    tracing.set_trace_id("")
    yield
    tracing.set_trace_id("")


# ---------------------------------------------------------------------------
# mock GitProvider
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_git_provider():
    """mock GitProvider：fetch/show/ls_tree 全部可控。

    用一个 dict 维护 (sha, path) → content 映射，ls_tree 按真实目录树语义返回
    指定 root 下的直接子条目（子目录返回 TREE，文件返回 BLOB）。
    """
    from server.common.models import TreeEntry, TreeEntryType

    git = MagicMock()
    git.fetch = MagicMock(return_value=None)
    # (sha, path) → content
    git._files: dict[tuple[str, str], str] = {}

    def _show(sha: str, path: str) -> str:
        return git._files.get((sha, path), "")

    def _ls_tree(sha: str, root: str):
        """按真实目录树语义返回 root 下的直接子条目。

        遍历所有 (sha, path) 文件，找出以 root + "/" 开头的，
        返回下一级子路径（子目录归并为 TREE，文件为 BLOB）。
        """
        children: dict[str, TreeEntryType] = {}
        prefix = (root + "/") if root else ""
        for (file_sha, file_path) in git._files:
            if file_sha != sha:
                continue
            if not file_path.startswith(prefix):
                continue
            # 去掉前缀后的剩余路径
            rest = file_path[len(prefix):]
            if not rest:
                continue
            parts = rest.split("/", 1)
            first = parts[0]
            if first in children:
                continue
            # 若有子路径，说明是目录；否则是文件
            if len(parts) > 1 and parts[1]:
                children[first] = TreeEntryType.TREE
            else:
                children[first] = TreeEntryType.BLOB
        # 构造完整 path（root/first）作为 TreeEntry.path，便于递归 ls_tree
        return [
            TreeEntry(
                path=f"{root}/{name}" if root else name,
                type=ttype,
                sha=f"{ttype.value}-{root}/{name}" if root else f"{ttype.value}-{name}",
            )
            for name, ttype in children.items()
        ]

    git.show = MagicMock(side_effect=_show)
    git.ls_tree = MagicMock(side_effect=_ls_tree)

    def _add_file(sha: str, path: str, content: str) -> None:
        git._files[(sha, path)] = content

    git._add_file = _add_file
    return git


# ---------------------------------------------------------------------------
# mock SyncService
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_sync_service():
    """mock SyncService：get_sync_status 返回可控的 SyncStatus。"""
    from server.infra_db.sync import SyncStatus

    svc = MagicMock()
    default_status = SyncStatus(
        last_synced_commit="commit-synced-001",
        last_synced_at=datetime.now(timezone.utc) - timedelta(seconds=30),
        status="ok",
        lag_periods=0,
    )
    svc._status = default_status

    def _get_sync_status():
        return svc._status

    svc.get_sync_status = MagicMock(side_effect=_get_sync_status)
    svc.trigger_sync = MagicMock(return_value=MagicMock(commit_sha="commit-synced-001"))
    svc.reconcile = MagicMock(return_value=MagicMock())
    return svc


# ---------------------------------------------------------------------------
# mock RestrictedReader
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_restricted_reader():
    """mock RestrictedReader：默认 available + 返回固定明文。"""
    reader = MagicMock()
    reader.is_available = MagicMock(return_value=True)
    reader.read = MagicMock(return_value="# restricted content")
    reader.list_files = MagicMock(return_value=[])
    return reader


# ---------------------------------------------------------------------------
# RecallService 实例
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_service(
    database,
    asset_index,
    embedding_service,
    vector_store,
    mock_git_provider,
    mock_sync_service,
    mock_restricted_reader,
    tmp_path,
):
    """RecallService 实例（注入所有 mock 依赖）。

    head_resolver 返回固定 HEAD sha "commit-head-002"。
    repo_root 指向 tmp_path（用于离线降级测试）。
    """
    from server.recall.service import RecallService

    head_sha = "commit-head-002"

    def _head() -> str:
        return head_sha

    svc = RecallService(
        database=database,
        asset_index=asset_index,
        embedding_service=embedding_service,
        sync_service=mock_sync_service,
        vector_store=vector_store,
        git_provider=mock_git_provider,
        repo_root=str(tmp_path),
        restricted_reader=mock_restricted_reader,
        head_resolver=_head,
        repo_url="https://example.com/repo.git",
        offline_root=str(tmp_path),
    )
    svc._test_head_sha = head_sha  # 测试断言用
    return svc


# ---------------------------------------------------------------------------
# 预置资产 + 装配
# ---------------------------------------------------------------------------


@pytest.fixture
def seeded_assets(database, asset_index, embedding_service, vector_store, mock_git_provider):
    """预置 4 个资产 + agent_binding + 向量库写入。

    资产：
    - rule-backend-lint（modules/backend，active，team，binding=fixed+enabled）
    - rule-frontend-lint（modules/frontend，active，team，binding=on-demand+enabled）
    - rule-deleted（modules/backend，deleted，team，binding=enabled 但资产已删）
    - rule-disabled-binding（modules/backend，active，team，binding=enabled=false）

    agent_id="builder-01"，agent_binding 写入后召回应只返回前两个。
    """
    from server.common.models import Asset as AssetVO, AssetType, Scope
    from server.infra_db.models import AgentBinding
    from server.infra_db.vectorstore import VectorRecord

    # commit SHA 基准
    commit_sha = "commit-synced-001"

    assets = [
        {
            "id": "rule-backend-lint",
            "type": "rule",
            "owner": "tester",
            "scope": Scope.TEAM,
            "module_path": "modules/backend",
            "category": "rule-backend",
            "git_path": "modules/backend/rules/lint.md",
            "content": "# 后端 lint 规则\n所有函数需类型标注\n禁止 print 调试\n",
            "tags": ["lint", "backend"],
            "binding_type": "fixed",
            "enabled": True,
        },
        {
            "id": "rule-frontend-lint",
            "type": "rule",
            "owner": "tester",
            "scope": Scope.TEAM,
            "module_path": "modules/frontend",
            "category": "rule-frontend",
            "git_path": "modules/frontend/rules/lint.md",
            "content": "# 前端 lint 规则\n组件命名用 PascalCase\n禁止 any 类型\n",
            "tags": ["lint", "frontend"],
            "binding_type": "on-demand",
            "enabled": True,
        },
        {
            "id": "rule-deleted",
            "type": "rule",
            "owner": "tester",
            "scope": Scope.TEAM,
            "module_path": "modules/backend",
            "category": "rule-backend",
            "git_path": "modules/backend/rules/deleted.md",
            "content": "# 已删除的规则\n",
            "tags": [],
            "binding_type": "on-demand",
            "enabled": True,
            "status": "deleted",
        },
        {
            "id": "rule-disabled-binding",
            "type": "rule",
            "owner": "tester",
            "scope": Scope.TEAM,
            "module_path": "modules/backend",
            "category": "rule-backend",
            "git_path": "modules/backend/rules/disabled.md",
            "content": "# 装配失效的规则\n",
            "tags": [],
            "binding_type": "on-demand",
            "enabled": False,  # 装配失效
        },
    ]

    agent_id = "builder-01"
    for a in assets:
        vo = AssetVO(
            id=a["id"],
            type=AssetType(a["type"]),
            owner=a["owner"],
            scope=a["scope"],
            content=a["content"],
            content_file_ref=a["git_path"],
            module_path=a["module_path"],
            category=a["category"],
            tags=a["tags"],
        )
        asset_index.upsert(vo, git_commit=commit_sha, content_snapshot=a["content"])
        # 若标记 deleted，调 delete（soft delete + 同事务级联 enabled=false）
        if a.get("status") == "deleted":
            asset_index.delete(a["id"], git_commit=commit_sha)
        # 写 agent_binding（disabled-binding 单独 enabled=false；deleted 资产 delete 时已级联 enabled=false）
        if a["id"] == "rule-disabled-binding":
            with database.session() as sess:
                sess.add(
                    AgentBinding(
                        id=f"binding-{a['id']}",
                        agent_id=agent_id,
                        asset_id=a["id"],
                        binding_type=a["binding_type"],
                        enabled=False,
                    )
                )
        elif a["id"] != "rule-deleted":
            # delete() 已为 rule-deleted 写入 enabled=false binding；其他资产这里写 enabled=true
            with database.session() as sess:
                # 检查是否已有 binding（delete 时可能已写）
                from sqlalchemy import select

                existing = sess.execute(
                    select(AgentBinding)
                    .where(AgentBinding.agent_id == agent_id)
                    .where(AgentBinding.asset_id == a["id"])
                ).scalar_one_or_none()
                if existing is None:
                    sess.add(
                        AgentBinding(
                            id=f"binding-{a['id']}",
                            agent_id=agent_id,
                            asset_id=a["id"],
                            binding_type=a["binding_type"],
                            enabled=True,
                        )
                    )
        # 写向量库
        emb = embedding_service.embed(a["content"])
        vector_store.ensure_collection(embedding_service.get_active_version(), emb.dim)
        vector_store.upsert(
            VectorRecord(
                asset_id=a["id"],
                vector=emb.vector,
                dim=emb.dim,
                metadata={
                    "module_path": a["module_path"],
                    "category": a["category"],
                    "type": a["type"],
                    "owner": a["owner"],
                    "scope": a["scope"].value,
                    "model_version": embedding_service.get_active_version(),
                },
            ),
            model_version=embedding_service.get_active_version(),
        )
        # 同时写 git provider 内容（供 recall_read / 降级路径用）
        mock_git_provider._add_file(commit_sha, a["git_path"], a["content"])

    return {
        "agent_id": agent_id,
        "commit_sha": commit_sha,
        "assets": assets,
    }


# ---------------------------------------------------------------------------
# FastAPI TestClient
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_client(recall_service, seeded_assets):
    """FastAPI TestClient，绑定 recall_service 实例。"""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from server.recall.api import build_router

    app = FastAPI()
    app.include_router(build_router(service=recall_service))
    return TestClient(app)
