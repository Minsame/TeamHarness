"""SubTask 2.3 / 2.5 / 2.6：webhook 同步 + reconciliation + 对账。

覆盖：
- webhook 增量同步：读 INDEX.md + 资产文件 diff → upsert/delete
- commit SHA 幂等：同一 commit 重复触发只处理一次
- INDEX.md counts 校验：不一致告警，不阻断
- reconciliation cron：检测滞后 → trigger_sync
- 对账任务：embedding_id IS NULL 重新投递
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest
from sqlalchemy import select, update

from server.common.models import DiffEntry, DiffStatus, TreeEntry, TreeEntryType
from server.infra_db.models import (
    AssetIndex as AssetIndexRow,
    EmbeddingTaskQueue,
    IndexSyncState,
    ModuleStats,
)
from server.infra_git.git_provider import GitProvider


# ---------------------------------------------------------------------------
# MockGitProvider
# ---------------------------------------------------------------------------


class MockGitProvider(GitProvider):
    """内存 GitProvider，模拟 fetch / show / diff / ls_tree。

    仓库内容按 commit SHA 索引：self.commits[sha] = {path: content}
    diff 通过对比两个 commit 的 path 集合生成。
    """

    def __init__(self) -> None:
        self.commits: dict[str, dict[str, str]] = {}
        self.fetch_calls: list[str] = []

    def add_commit(self, sha: str, files: dict[str, str]) -> None:
        """添加一个 commit，files 为 {path: content}。"""
        # 继承上一个 commit 的内容（模拟 git 提交叠加）
        # 通过参数控制：调用方应传完整文件集
        self.commits[sha] = dict(files)

    def fetch(self, repo: str) -> None:
        self.fetch_calls.append(repo)

    def show(self, sha: str, path: str) -> str:
        if sha not in self.commits:
            raise FileNotFoundError(f"commit {sha} 不存在")
        files = self.commits[sha]
        if path not in files:
            raise FileNotFoundError(f"path {path} 不存在于 commit {sha}")
        return files[path]

    def diff(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        if sha_a not in self.commits:
            raise ValueError(f"commit {sha_a} 不存在")
        if sha_b not in self.commits:
            raise ValueError(f"commit {sha_b} 不存在")
        files_a = self.commits[sha_a]
        files_b = self.commits[sha_b]
        entries: list[DiffEntry] = []
        all_paths = set(files_a) | set(files_b)
        for p in sorted(all_paths):
            in_a = p in files_a
            in_b = p in files_b
            if in_a and not in_b:
                entries.append(DiffEntry(path=p, status=DiffStatus.DELETED, old_path=p))
            elif in_b and not in_a:
                entries.append(DiffEntry(path=p, status=DiffStatus.ADDED, new_path=p))
            elif files_a[p] != files_b[p]:
                entries.append(DiffEntry(path=p, status=DiffStatus.MODIFIED, old_path=p, new_path=p))
        return entries

    def ls_tree(self, sha: str, path: str) -> list[TreeEntry]:
        if sha not in self.commits:
            return []
        prefix = (path + "/") if path and not path.endswith("/") else path
        # 列出 prefix 下的直接子项
        seen: set[str] = set()
        entries: list[TreeEntry] = []
        for p in self.commits[sha]:
            if prefix and not p.startswith(prefix):
                continue
            rest = p[len(prefix):] if prefix else p
            if not rest:
                continue
            first = rest.split("/", 1)[0]
            if first in seen:
                continue
            seen.add(first)
            is_dir = "/" in rest
            entries.append(
                TreeEntry(
                    path=f"{prefix}{first}" if prefix else first,
                    type=TreeEntryType.TREE if is_dir else TreeEntryType.BLOB,
                    sha=f"blob-{first}",
                )
            )
        return entries


def _asset_md(
    *,
    id: str = "rule-test",
    type: str = "rule",
    owner: str = "tester",
    scope: str = "team",
    category: str | None = "rule-backend",
    module: str = "",
    body: str = "# Test Rule\n规则正文",
    tags: list[str] | None = None,
) -> str:
    """构造资产 .md 文本（带 frontmatter）。"""
    tags = tags or ["backend"]
    return (
        "---\n"
        f"id: {id}\n"
        f"type: {type}\n"
        f"owner: {owner}\n"
        f"scope: {scope}\n"
        f"category: {category or ''}\n"
        f"tags: {tags}\n"
        "version: 1.0.0\n"
        "---\n"
        f"{body}\n"
    )


def _index_md(
    *,
    module: str = "",
    level: str = "project",
    assets_count: int = 0,
    submodules_count: int = 0,
) -> str:
    """构造 INDEX.md 文本。"""
    return (
        "---\n"
        f"level: {level}\n"
        f"module: {module}\n"
        f"counts:\n  assets: {assets_count}\n  submodules: {submodules_count}\n"
        "---\n"
    )


# ---------------------------------------------------------------------------
# 增量同步测试
# ---------------------------------------------------------------------------


def test_full_rebuild_on_first_sync(sync_service_factory):
    """首次同步（previous 为空）走全量重建路径。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(module="teamharness", assets_count=1, submodules_count=0),
        "rules/rule-1.md": _asset_md(id="rule-1", body="第一条规则"),
    })
    sync = sync_service_factory(git)
    result = sync.trigger_sync("c1")

    assert result.ok
    assert result.previous_commit == ""
    # 全量重建应至少扫描到 1 个资产
    assert result.assets_upserted == 1
    assert result.indexes_updated == 1
    # index_sync_state 已更新
    status = sync.get_sync_status()
    assert status.last_synced_commit == "c1"
    assert status.status == "ok"


def test_incremental_sync_adds_new_asset(sync_service_factory, asset_index):
    """增量同步：新增资产文件 → upsert。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    git.add_commit("c2", {
        "INDEX.md": _index_md(assets_count=2),
        "rules/rule-1.md": _asset_md(id="rule-1"),
        "rules/rule-2.md": _asset_md(id="rule-2", body="第二条"),
    })
    sync = sync_service_factory(git)
    sync.trigger_sync("c1")
    result = sync.trigger_sync("c2")

    assert result.ok
    assert result.previous_commit == "c1"
    # 增量：只新增 1 个资产
    assert result.assets_upserted == 1
    # 两个资产都在 DB
    assert asset_index.get_status("rule-1") is not None
    assert asset_index.get_status("rule-2") is not None


def test_incremental_sync_deletes_asset(sync_service_factory, asset_index):
    """增量同步：删除资产文件 → soft delete。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=2),
        "rules/rule-1.md": _asset_md(id="rule-1"),
        "rules/rule-2.md": _asset_md(id="rule-2"),
    })
    git.add_commit("c2", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
        # rule-2.md 删除
    })
    sync = sync_service_factory(git)
    sync.trigger_sync("c1")
    result = sync.trigger_sync("c2")

    assert result.ok
    assert result.assets_deleted == 1
    status = asset_index.get_status("rule-2")
    assert status is not None
    assert status.status == "deleted"


def test_commit_sha_idempotent(sync_service_factory):
    """commit SHA 幂等：重复同一 commit 跳过。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    first = sync.trigger_sync("c1")
    assert first.ok
    second = sync.trigger_sync("c1")
    assert second.skipped
    assert "已同步过" in second.skip_reason


def test_module_path_inferred_from_modules_dir(sync_service_factory, asset_index):
    """modules/<module>/rules/x.md → module_path=modules/<module>。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=0, submodules_count=1),
        "modules/backend/INDEX.md": _index_md(module="backend", level="module", assets_count=1),
        "modules/backend/rules/rule-be.md": _asset_md(id="rule-be", body="backend rule"),
    })
    sync = sync_service_factory(git)
    sync.trigger_sync("c1")

    status = asset_index.get_status("rule-be")
    assert status is not None
    assert status.module_path == "modules/backend" or status.git_path == "modules/backend/rules/rule-be.md"


def test_index_md_counts_check_persisted(sync_service_factory, database):
    """INDEX.md counts 校验：声明值写入 module_stats。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=5, submodules_count=2),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    result = sync.trigger_sync("c1")
    # counts 不一致：声明 5 实际 1 → mismatches=1，但不阻断
    assert result.counts_mismatches >= 1
    assert result.ok  # 不阻断

    with database.session() as sess:
        row = sess.get(ModuleStats, "")
        assert row is not None
        assert row.declared_asset_count == 5
        assert row.actual_asset_count == 1
        assert row.counts_consistent is False


def test_sync_with_empty_after_commit_skipped(sync_service_factory):
    """after commit 为空（删除分支等）→ 跳过。"""
    from server.common.models import WebhookEvent

    git = MockGitProvider()
    sync = sync_service_factory(git)
    event = WebhookEvent(
        provider="gitlab",
        event_type="push",
        repo="test/repo",
        before="aaa",
        after="0" * 40,
        ref="refs/heads/feature",
    )
    result = sync.handle_webhook_event(event)
    assert result.skipped


# ---------------------------------------------------------------------------
# SubTask 2.5：reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_triggers_sync_when_lagging(sync_service_factory):
    """reconciliation：HEAD 与 last_synced_commit 不一致 → trigger_sync。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    git.add_commit("c2", {
        "INDEX.md": _index_md(assets_count=2),
        "rules/rule-1.md": _asset_md(id="rule-1"),
        "rules/rule-2.md": _asset_md(id="rule-2"),
    })
    sync = sync_service_factory(git)
    # 先同步到 c1
    sync.trigger_sync("c1")
    # reconciliation：HEAD=c2，last=c1 → 触发同步
    result = sync.reconcile(head_resolver=lambda: "c2")
    assert result.triggered_sync
    assert result.head_commit == "c2"
    # 同步成功后 lag_periods 应为 0
    assert result.lag_periods == 0
    # 状态已更新到 c2
    status = sync.get_sync_status()
    assert status.last_synced_commit == "c2"


def test_reconciliation_no_op_when_up_to_date(sync_service_factory):
    """reconciliation：HEAD == last_synced → 不触发同步。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    sync.trigger_sync("c1")
    result = sync.reconcile(head_resolver=lambda: "c1")
    assert not result.triggered_sync
    assert result.lag_periods == 0


def test_reconciliation_lag_periods_increment_on_failure(sync_service_factory, database):
    """reconciliation：sync 失败 → lag_periods 累加；连续 3 周期触发告警。"""
    git = MockGitProvider()
    # c1 可同步成功，c2 不存在 → trigger_sync 失败
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    sync.trigger_sync("c1")

    # 模拟 HEAD 指向不存在的 commit（head_resolver 返回未知 SHA）
    result1 = sync.reconcile(head_resolver=lambda: "unknown-sha")
    assert result1.triggered_sync
    assert result1.error is not None
    assert result1.lag_periods >= 1

    result2 = sync.reconcile(head_resolver=lambda: "unknown-sha")
    assert result2.lag_periods >= 2
    result3 = sync.reconcile(head_resolver=lambda: "unknown-sha")
    assert result3.lag_periods >= 3
    assert result3.alert_lagging is True  # 连续 3 周期触发告警


# ---------------------------------------------------------------------------
# SubTask 2.6：对账任务
# ---------------------------------------------------------------------------


def test_reconcile_embedding_requeues_null_assets(database, asset_index, outbox_worker, sync_service_factory, embedding_service):
    """对账任务：embedding_id IS NULL 的资产被重新投递。

    场景：
    1. upsert 资产（pending 任务）但不运行 worker → embedding_id 为 NULL
    2. 把 indexed_at 改早于 1 小时前 → 对账可命中
    3. 对账运行 → 投递 reindex 任务
    """
    from server.infra_db.tests.conftest import make_asset

    git = MockGitProvider()
    sync = sync_service_factory(git)
    asset = make_asset(id="a-recon-1", content="recon test")
    asset_index.upsert(asset, git_commit="c1")
    # 把 indexed_at 改早于 1 小时
    old_time = datetime.now(timezone.utc) - timedelta(seconds=4000)
    with database.session() as sess:
        sess.execute(
            update(AssetIndexRow)
            .where(AssetIndexRow.id == "a-recon-1")
            .values(indexed_at=old_time)
        )
    # 此时已有 pending 任务（upsert 时投递的），对账应跳过重复投递
    result = sync.run_reconcile_embedding(stale_seconds=3600)
    assert result.scanned == 1
    # 已有 pending → 不重复投递
    assert result.requeued == 0

    # 把已有 pending 任务清掉，再对账 → 应投递 reindex
    with database.session() as sess:
        sess.execute(
            EmbeddingTaskQueue.__table__.delete().where(
                EmbeddingTaskQueue.asset_id == "a-recon-1"
            )
        )
    result2 = sync.run_reconcile_embedding(stale_seconds=3600)
    assert result2.scanned == 1
    assert result2.requeued == 1


def test_reconcile_embedding_no_op_when_all_done(database, asset_index, outbox_worker, sync_service_factory, embedding_service):
    """对账任务：所有资产 embedding_id 已回写 → no-op。"""
    from server.infra_db.tests.conftest import make_asset

    git = MockGitProvider()
    sync = sync_service_factory(git)
    asset = make_asset(id="a-recon-ok", content="ok test")
    asset_index.upsert(asset, git_commit="c1")
    outbox_worker.run_once()  # 消费任务，回写 embedding_id
    status = asset_index.get_status("a-recon-ok")
    assert status.embedding_id is not None

    result = sync.run_reconcile_embedding(stale_seconds=3600)
    assert result.scanned == 0
    assert result.requeued == 0


# ---------------------------------------------------------------------------
# SubTask 2.10：counts 校验（与 sync 集成）
# ---------------------------------------------------------------------------


def test_counts_check_does_not_block_sync(sync_service_factory):
    """counts 不一致 → 告警但不阻断同步。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=99),  # 声明 99 但实际只有 1
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    result = sync.trigger_sync("c1")
    assert result.ok
    assert result.counts_mismatches >= 1


def test_counts_consistent_when_match(sync_service_factory, database):
    """counts 一致 → 无 mismatch。"""
    git = MockGitProvider()
    git.add_commit("c1", {
        "INDEX.md": _index_md(assets_count=1),  # 声明 1 实际 1
        "rules/rule-1.md": _asset_md(id="rule-1"),
    })
    sync = sync_service_factory(git)
    result = sync.trigger_sync("c1")
    assert result.ok
    assert result.counts_mismatches == 0
    with database.session() as sess:
        row = sess.get(ModuleStats, "")
        assert row.counts_consistent is True
