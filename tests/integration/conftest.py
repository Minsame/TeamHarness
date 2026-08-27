"""集成测试共享 fixtures（Agent 10）。

装配跨模块依赖：
- 基础设施：Database / VectorStore / EmbeddingService / AssetIndex / CountsChecker
- 同步层：mock GitProvider + 真实 SyncService
- 召回层：真实 RecallService（注入 mock restricted_reader）
- 装配层：真实 BindingService / CategorySuggestService / AgentApiKeyService / ToolReviewService
- 提炼层：PersonalDistill（mock LLM） + 真实 TeamDistill
- 治理层：真实 DashboardService / GovernanceMetrics / PRReviewDedupService

所有 fixture 用 SQLite 内存库 + InMemoryVectorStore，无外部依赖。
"""

from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import MagicMock

import pytest

# 复用 infra_db 与 recall 域内测试的 fixture（pytest 自动发现同包 conftest）
from server.infra_db.tests.conftest import (  # noqa: F401
    TEST_DB_ASYNC_URL,
    TEST_DB_SYNC_URL,
    asset_index,
    counts_checker,
    database,
    embedding_service,
    make_asset,
    outbox_worker,
    vector_store,
)


# ---------------------------------------------------------------------------
# 环境变量清理（autouse，保证测试隔离）
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clean_env():
    """清理可能影响测试的环境变量。"""
    for key in (
        "DATABASE_URL",
        "DATABASE_SYNC_URL",
        "DB_ECHO",
        "TEAMHARNESS_DEPLOY_MODE",
        "TEAMHARNESS_IN_DOCKER",
        "TEAMHARNESS_ALL_IN_ONE",
        "GIT_PROVIDER",
        "TEAMHARNESS_META_DB",
        "TEAMHARNESS_VECTOR_STORE",
        "TEAMHARNESS_GIT_PROVIDER",
        "EMBEDDING_ACTIVE_VERSION",
        "EMBEDDING_SHADOW_VERSION",
        "EMBEDDING_DIM",
    ):
        os.environ.pop(key, None)
    yield


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
# mock GitProvider（带 ls_tree 真实语义）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_git_provider():
    """mock GitProvider：fetch/show/ls_tree/diff 全部可控。

    用 dict 维护 (sha, path) → content 映射，ls_tree 按真实目录树语义返回
    指定 root 下的直接子条目（子目录归并为 TREE，文件为 BLOB）。
    """
    from server.common.models import TreeEntry, TreeEntryType

    git = MagicMock()
    git.fetch = MagicMock(return_value=None)
    git._files: dict[tuple[str, str], str] = {}

    def _show(sha: str, path: str) -> str:
        return git._files.get((sha, path), "")

    def _ls_tree(sha: str, root: str):
        """按真实目录树语义返回 root 下的直接子条目。"""
        children: dict[str, TreeEntryType] = {}
        prefix = (root + "/") if root else ""
        for (file_sha, file_path) in git._files:
            if file_sha != sha:
                continue
            if not file_path.startswith(prefix):
                continue
            rest = file_path[len(prefix):]
            if not rest:
                continue
            parts = rest.split("/", 1)
            first = parts[0]
            if first in children:
                continue
            if len(parts) > 1 and parts[1]:
                children[first] = TreeEntryType.TREE
            else:
                children[first] = TreeEntryType.BLOB
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
    git.diff = MagicMock(return_value=[])

    def _add_file(sha: str, path: str, content: str) -> None:
        git._files[(sha, path)] = content

    def _add_diff(from_sha: str, to_sha: str, diffs: list) -> None:
        """覆盖 diff 返回值。diffs 为 DiffEntry 列表。"""
        git.diff = MagicMock(return_value=diffs)

    git._add_file = _add_file
    git._add_diff = _add_diff
    return git


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
# 真实 SyncService 实例
# ---------------------------------------------------------------------------


@pytest.fixture
def sync_service(database, asset_index, embedding_service, counts_checker, mock_git_provider):
    """真实 SyncService 实例（注入 mock GitProvider）。"""
    from server.infra_db.sync import SyncService

    return SyncService(
        database=database,
        git_provider=mock_git_provider,
        asset_index=asset_index,
        embedding_service=embedding_service,
        counts_checker=counts_checker,
        repo_root=".",  # 测试用占位
    )


# ---------------------------------------------------------------------------
# 真实 RecallService 实例
# ---------------------------------------------------------------------------


@pytest.fixture
def recall_service(
    database,
    asset_index,
    embedding_service,
    vector_store,
    mock_git_provider,
    sync_service,
    mock_restricted_reader,
    tmp_path,
):
    """RecallService 实例（注入真实 SyncService 与 mock git/restricted）。"""
    from server.recall.service import RecallService

    head_sha = "commit-head-002"

    def _head() -> str:
        return head_sha

    svc = RecallService(
        database=database,
        asset_index=asset_index,
        embedding_service=embedding_service,
        sync_service=sync_service,
        vector_store=vector_store,
        git_provider=mock_git_provider,
        repo_root=str(tmp_path),
        restricted_reader=mock_restricted_reader,
        head_resolver=_head,
        repo_url="https://example.com/repo.git",
        offline_root=str(tmp_path),
    )
    svc._test_head_sha = head_sha
    return svc


# ---------------------------------------------------------------------------
# 装配真实 BindingService / CategorySuggestService / AgentApiKeyService / ToolReviewService
# ---------------------------------------------------------------------------


@pytest.fixture
def binding_service(database):
    """真实 BindingService 实例。"""
    from server.binding.binding_service import BindingService

    return BindingService(database=database)


@pytest.fixture
def category_suggest_service(database, tmp_path):
    """真实 CategorySuggestService 实例（mock LLM）。"""
    from server.binding.category_suggest import CategorySuggestService

    # mock LLM，返回固定候选
    mock_llm = MagicMock()

    def _chat(messages, *, schema=None, **kwargs):
        return {
            "content": '[{"category":"rule-backend","confidence":0.9,"rationale":"匹配后端规则"}]',
            "usage": {"total_tokens": 100},
            "model": "mock",
        }

    mock_llm.chat = MagicMock(side_effect=_chat)
    return CategorySuggestService(
        database=database,
        repo_root=tmp_path,
        llm=mock_llm,
    )


@pytest.fixture
def auth_service(database):
    """真实 AgentApiKeyService 实例。"""
    from server.binding.auth_service import AgentApiKeyService

    return AgentApiKeyService(database=database)


@pytest.fixture
def tool_review_service(database):
    """真实 ToolReviewService 实例。"""
    from server.binding.tool_review import ToolReviewService

    return ToolReviewService(database=database)


# ---------------------------------------------------------------------------
# mock LLM（用于 PersonalDistill / PRReviewDedup / TeamDistill）
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_llm():
    """mock LLM Provider，实现 LLMChatLike 协议。

    - chat(messages, schema, ...) → {"content": str, "usage": dict, "model": str}
    - schema 非 None 时返回符合 schema 的 JSON 字符串
    """
    llm = MagicMock()

    def _chat(messages, *, schema=None, model=None, temperature=0.2, max_tokens=None):
        # 默认返回一段结构化资产
        if schema is not None:
            # 简单识别 schema 类型
            properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
            if "decision" in properties:
                # PRReviewDedup schema
                content = '{"decision":"independent","rationale":"不同语境，保留独立"}'
            elif "signals" in properties or "score" in properties:
                content = '{"score": 0.7, "rationale": "mock 评分"}'
            else:
                content = '{"ok": true, "data": "mock"}'
        else:
            content = "mock LLM response"
        return {
            "content": content,
            "usage": {"prompt_tokens": 50, "completion_tokens": 30, "total_tokens": 80},
            "model": model or "mock-llm",
        }

    llm.chat = MagicMock(side_effect=_chat)
    return llm


# ---------------------------------------------------------------------------
# PersonalDistill 实例（mock LLM）
# ---------------------------------------------------------------------------


@pytest.fixture
def personal_distill(mock_llm, tmp_path):
    """PersonalDistill 实例（注入 mock LLM）。"""
    from server.distill_personal.budget import BudgetManager, PendingCandidateStore
    from server.distill_personal.metrics import SignalReporter
    from server.distill_personal.personal_distill import PersonalDistill

    budget_mgr = BudgetManager(default_daily_budget=100_000)
    pending_store = PendingCandidateStore(repo_root=tmp_path)
    signal_reporter = SignalReporter(member_id="alice")

    return PersonalDistill(
        llm=mock_llm,
        budget_mgr=budget_mgr,
        pending_store=pending_store,
        signal_reporter=signal_reporter,
        owner="alice",
        module_path="modules/backend",
        member_id="alice",
        repo_root=tmp_path,
    )


# ---------------------------------------------------------------------------
# 真实 TeamDistill 实例
# ---------------------------------------------------------------------------


@pytest.fixture
def team_distill(
    database,
    asset_index,
    embedding_service,
    vector_store,
    mock_llm,
    tmp_path,
):
    """真实 TeamDistill 实例。"""
    from server.distill_team.service import TeamDistill

    head_sha = "commit-head-002"

    def _head() -> str:
        return head_sha

    return TeamDistill(
        database=database,
        asset_index=asset_index,
        embedding_service=embedding_service,
        vector_store=vector_store,
        head_resolver=_head,
        repo_root=tmp_path,
        llm=mock_llm,
    )


# ---------------------------------------------------------------------------
# 真实 DashboardService / GovernanceMetrics / PRReviewDedupService
# ---------------------------------------------------------------------------


@pytest.fixture
def dashboard_service(database, tmp_path):
    """真实 DashboardService 实例。"""
    from server.governance.dashboard import DashboardService

    return DashboardService(database=database, archive_root=str(tmp_path / "archive"))


@pytest.fixture
def governance_metrics(database):
    """真实 GovernanceMetrics 实例。"""
    from server.governance.metrics import GovernanceMetrics

    return GovernanceMetrics(database=database)


@pytest.fixture
def pr_review_dedup_service(
    database,
    asset_index,
    embedding_service,
    vector_store,
    mock_llm,
):
    """真实 PRReviewDedupService 实例。"""
    from server.governance.pr_review_dedup import PRReviewDedupService

    return PRReviewDedupService(
        database=database,
        asset_index=asset_index,
        embedding_service=embedding_service,
        vector_store=vector_store,
        llm=mock_llm,
    )


# ---------------------------------------------------------------------------
# 辅助：构造并写入资产 + 向量库 + git_provider 内容
# ---------------------------------------------------------------------------


def seed_asset(
    *,
    asset_index,
    embedding_service,
    vector_store,
    mock_git_provider,
    asset_id: str,
    content: str,
    git_path: str,
    module_path: str,
    commit_sha: str,
    owner: str = "tester",
    scope: str = "team",
    asset_type: str = "rule",
    category: str | None = None,
    tags: list[str] | None = None,
) -> str:
    """构造资产 VO → upsert 到 asset_index → 写向量库 → 写 git_provider mock。

    返回 asset_id。供集成测试快速预置资产用。
    """
    from server.common.models import Asset as AssetVO, AssetType, Scope
    from server.infra_db.vectorstore import VectorRecord

    vo = AssetVO(
        id=asset_id,
        type=AssetType(asset_type),
        owner=owner,
        scope=Scope(scope),
        content=content,
        content_file_ref=git_path,
        module_path=module_path,
        category=category,
        tags=tags or [],
    )
    asset_index.upsert(vo, git_commit=commit_sha, content_snapshot=content)

    # 写向量库
    emb = embedding_service.embed(content)
    vector_store.ensure_collection(embedding_service.get_active_version(), emb.dim)
    vector_store.upsert(
        VectorRecord(
            asset_id=asset_id,
            vector=emb.vector,
            dim=emb.dim,
            metadata={
                "module_path": module_path,
                "category": category,
                "type": asset_type,
                "owner": owner,
                "scope": scope,
                "model_version": embedding_service.get_active_version(),
            },
        ),
        model_version=embedding_service.get_active_version(),
    )

    # 写 git provider mock 内容（供 recall_read / 降级路径用）
    mock_git_provider._add_file(commit_sha, git_path, content)
    return asset_id


# ---------------------------------------------------------------------------
# 辅助：构造 Session（一级提炼输入）
# ---------------------------------------------------------------------------


def make_session(*, session_id: str = "sess-1", turns: list[dict] | None = None):
    """构造 Session 对象（PersonalDistill 输入）。"""
    from server.distill_personal.session_provider import Session, SessionTurn

    default_turns = turns or [
        {"role": "user", "content": "如何避免 SQLAlchemy CircularForeignKey？"},
        {
            "role": "assistant",
            "content": "用 mapped_column 加 use_alter=True，或拆分为单独 ALTER TABLE 语句。",
        },
        {"role": "user", "content": "那如何处理双向 relationship？"},
        {
            "role": "assistant",
            "content": "用 back_populates 显式声明双向，避免 backref 自动生成冲突。",
        },
    ]
    return Session(
        session_id=session_id,
        turns=[
            SessionTurn(role=t["role"], content=t["content"], timestamp="")
            for t in default_turns
        ],
        started_at="",
        ended_at="",
        source_path="",
        completed=True,
    )
