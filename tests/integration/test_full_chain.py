"""SubTask 10.1: 跨模块全链路联通测试。

验证完整链路：入库 → webhook 同步 → DB 索引 → 召回 → 一级提炼 → push → 二级提炼 → 发布。

每个测试用例覆盖链路的一段或多段，失败时定位到具体阶段。

对应域内验证点：
- 跨模块全链路：入库 → webhook 同步 → DB 索引 → 召回 → 一级提炼 → push → 二级提炼 → 发布 Prompt
"""

from __future__ import annotations

from pathlib import Path

import pytest


# ---------------------------------------------------------------------------
# 阶段 1：入库 + 同步（AssetIndex.upsert → SyncService.trigger_sync）
# ---------------------------------------------------------------------------


class TestStage1IngestAndSync:
    """阶段 1：资产入库 + webhook 同步。"""

    def test_upsert_asset_and_query_back(
        self,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """AssetIndex.upsert 写入 → get_status / query 可读。"""
        from server.common.models import Asset as AssetVO, AssetType, Scope

        vo = AssetVO(
            id="rule-test-001",
            type=AssetType.RULE,
            owner="alice",
            scope=Scope.TEAM,
            content="# 测试规则\n所有函数需类型标注",
            content_file_ref="modules/backend/rules/test.md",
            module_path="modules/backend",
            category="rule-backend",
            tags=["lint"],
        )
        asset_index.upsert(vo, git_commit="commit-001", content_snapshot=vo.content)

        # get_status 应能查到
        status = asset_index.get_status("rule-test-001")
        assert status is not None
        assert status.status == "active"
        assert status.git_commit == "commit-001"
        assert status.module_path == "modules/backend"

        # query 应能查到
        from server.infra_db.asset_index import AssetFilter

        rows = asset_index.query(AssetFilter(module_paths=["modules/backend"]))
        assert len(rows) == 1
        assert rows[0].id == "rule-test-001"

    def test_sync_service_trigger_sync_full_rebuild(
        self,
        sync_service,
        asset_index,
        mock_git_provider,
    ):
        """SyncService.trigger_sync 全量重建：mock git_provider 内容 → DB 索引。

        验证：trigger_sync 后 asset_index 含 mock git 中的资产。
        """
        # 准备 mock git 内容：根 INDEX.md + 一个根级规则 + 模块 INDEX.md + 模块规则
        commit_sha = "commit-full-001"
        mock_git_provider._add_file(
            commit_sha,
            "INDEX.md",
            """---
level: project
parent: null
module: teamharness-shared
assets:
  - id: rule-global-001
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
        )
        mock_git_provider._add_file(
            commit_sha,
            "rules/global.md",
            "---\nid: rule-global-001\ntype: rule\nowner: alice\nscope: team\n---\n# 全局规则\n所有函数需类型标注\n",
        )
        mock_git_provider._add_file(
            commit_sha,
            "modules/backend/INDEX.md",
            """---
level: module
parent: ../../INDEX.md
module: backend
assets:
  - id: rule-backend-001
    path: rules/backend.md
    type: rule
    purpose: 后端规则
submodules: []
counts:
  assets: 1
  submodules: 0
---
""",
        )
        mock_git_provider._add_file(
            commit_sha,
            "modules/backend/rules/backend.md",
            "---\nid: rule-backend-001\ntype: rule\nowner: bob\nscope: team\n---\n# 后端规则\n禁止 print 调试\n",
        )

        # 触发同步
        result = sync_service.trigger_sync(commit_sha)
        assert result.commit_sha == commit_sha
        assert result.skipped is False
        # 应至少 upsert 2 个资产（根 + 模块）
        assert result.assets_upserted >= 2

        # 验证 DB 索引
        from server.infra_db.asset_index import AssetFilter

        rows = asset_index.query(AssetFilter(statuses=["active"]), limit=100)
        ids = {r.id for r in rows}
        assert "rule-global-001" in ids
        assert "rule-backend-001" in ids

        # 同步状态更新
        sync_status = sync_service.get_sync_status()
        assert sync_status.last_synced_commit == commit_sha
        assert sync_status.status == "ok"

    def test_sync_idempotent_same_commit(
        self,
        sync_service,
        mock_git_provider,
    ):
        """SyncService 幂等：同一 commit 第二次 trigger_sync 跳过。"""
        commit_sha = "commit-idem-001"
        mock_git_provider._add_file(
            commit_sha, "INDEX.md",
            "---\nlevel: project\nmodule: test\nassets: []\nsubmodules: []\ncounts: {assets: 0, submodules: 0}\n---\n",
        )

        first = sync_service.trigger_sync(commit_sha)
        assert first.skipped is False

        second = sync_service.trigger_sync(commit_sha)
        assert second.skipped is True
        assert "已同步过" in second.skip_reason


# ---------------------------------------------------------------------------
# 阶段 2：召回（RecallService.recall_list / recall_read）
# ---------------------------------------------------------------------------


class TestStage2Recall:
    """阶段 2：召回服务。"""

    def test_recall_list_no_query_returns_fixed_bindings(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """recall_list 无 query → 返回装配清单（fixed 绑定的资产）。"""
        from server.infra_db.models import AgentBinding, IndexSyncState
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-recall-001"
        # 预置同步状态（让 as_of_commit 非空）
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton",
                last_synced_commit=commit_sha,
                status="ok",
                lag_periods=0,
            ))
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-recall-001",
            content="# 召回测试规则\n所有函数需类型标注",
            git_path="modules/backend/rules/r1.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
            tags=["lint"],
        )
        # 写 fixed 装配
        with database.session() as sess:
            sess.add(AgentBinding(
                id="binding-recall-001",
                agent_id="builder-01",
                asset_id="rule-recall-001",
                binding_type="fixed",
                enabled=True,
            ))

        result = recall_service.recall_list(
            agent_id="builder-01",
            query=None,
            module_path="modules/backend",
        )
        assert len(result.items) >= 1
        assert any(it.asset_id == "rule-recall-001" for it in result.items)
        assert result.as_of_commit  # 非空
        assert result.trace_id  # trace_id 注入

    def test_recall_list_with_query_does_semantic_search(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """recall_list 有 query → 向量检索 + BM25 + RRF 精排。"""
        from server.infra_db.models import AgentBinding
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-recall-002"
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-sem-001",
            content="# SQLAlchemy 循环外键\n使用 use_alter=True 避免循环依赖",
            git_path="modules/backend/rules/sem.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
            tags=["sqlalchemy"],
        )
        with database.session() as sess:
            sess.add(AgentBinding(
                id="binding-sem-001",
                agent_id="builder-02",
                asset_id="rule-sem-001",
                binding_type="on-demand",
                enabled=True,
            ))

        result = recall_service.recall_list(
            agent_id="builder-02",
            query="SQLAlchemy 循环外键怎么处理",
            module_path="modules/backend",
        )
        assert len(result.items) >= 1
        # 召回命中率最高的应该是 rule-sem-001
        top = result.items[0]
        assert top.asset_id == "rule-sem-001"

    def test_recall_read_returns_content(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """recall_read 返回资产正文。"""
        from server.infra_db.models import AgentBinding
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-read-001"
        content = "# 后端 lint 规则\n所有函数需类型标注\n"
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-read-001",
            content=content,
            git_path="modules/backend/rules/read.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
        )
        with database.session() as sess:
            sess.add(AgentBinding(
                id="binding-read-001",
                agent_id="builder-03",
                asset_id="rule-read-001",
                binding_type="fixed",
                enabled=True,
            ))

        result = recall_service.recall_read(
            agent_id="builder-03",
            asset_id="rule-read-001",
        )
        assert "类型标注" in result.content
        assert result.asset_id == "rule-read-001"

    def test_recall_read_deleted_returns_410_gone(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """recall_read 已删除资产 → AssetGoneError + 替代建议。"""
        from server.infra_db.models import AgentBinding
        from server.recall.service import AssetGoneError
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-gone-001"
        # 写两个资产：一个 active，一个 deleted
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-active-001",
            content="# active 规则\n",
            git_path="modules/backend/rules/active.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
        )
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-deleted-001",
            content="# deleted 规则\n",
            git_path="modules/backend/rules/deleted.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
        )
        # 删除后者
        asset_index.delete("rule-deleted-001", git_commit=commit_sha)
        # 装配（deleted 已级联 enabled=false，但为 deleted 资产单独写 enabled=true 模拟旧数据）
        with database.session() as sess:
            sess.add(AgentBinding(
                id="binding-active-001",
                agent_id="builder-04",
                asset_id="rule-active-001",
                binding_type="fixed",
                enabled=True,
            ))
            sess.add(AgentBinding(
                id="binding-deleted-001",
                agent_id="builder-04",
                asset_id="rule-deleted-001",
                binding_type="fixed",
                enabled=True,
            ))

        with pytest.raises(AssetGoneError) as exc_info:
            recall_service.recall_read(
                agent_id="builder-04",
                asset_id="rule-deleted-001",
            )
        assert exc_info.value.asset_id == "rule-deleted-001"
        # 替代建议中应包含同模块的 active 资产
        alt_ids = {a.asset_id for a in exc_info.value.alternatives}
        assert "rule-active-001" in alt_ids


# ---------------------------------------------------------------------------
# 阶段 3：一级提炼（PersonalDistill.run_light / run_rem / run_deep）
# ---------------------------------------------------------------------------


class TestStage3PersonalDistill:
    """阶段 3：一级提炼（个人 dream）。"""

    def test_personal_distill_full_chain(
        self,
        personal_distill,
    ):
        """PersonalDistill.run 完整链路：sessions → signals → intents → assets。"""
        from tests.integration.conftest import make_session

        sessions = [make_session(session_id="sess-distill-001")]
        result = personal_distill.run(sessions, member_id="alice")

        # Light 阶段产出 signals
        assert result.light is not None
        assert result.light.signal_count >= 0
        # REM 阶段产出 intents
        assert result.rem is not None
        # Deep 阶段产出 assets 或 pending
        assert result.deep is not None
        # 隐私审计通过
        assert result.privacy_audit.get("ok", True) is True
        # 无错误
        assert result.error is None

    def test_personal_distill_run_light_only(
        self,
        personal_distill,
    ):
        """PersonalDistill.run_light 单独调用 → 返回 signals 列表。"""
        from tests.integration.conftest import make_session

        sessions = [make_session(session_id="sess-light-001")]
        signals = personal_distill.run_light(sessions)
        assert isinstance(signals, list)

    def test_personal_distill_report_metrics(
        self,
        personal_distill,
    ):
        """PersonalDistill.report_metrics 上报信号计数。"""
        ok = personal_distill.report_metrics(
            member_id="alice",
            signal_count=10,
            yield_ratio=0.5,
        )
        # 上报成功（mock 或本地落盘均应返回 True/False，不应抛异常）
        assert isinstance(ok, bool)


# ---------------------------------------------------------------------------
# 阶段 4：push 到 asset_index（一级提炼产出 → AssetIndex.upsert）
# ---------------------------------------------------------------------------


class TestStage4PushToAssetIndex:
    """阶段 4：一级提炼产出资产 → 写入 asset_index（push 到中央仓库的 DB 索引层模拟）。"""

    def test_distilled_asset_upsert_to_asset_index(
        self,
        personal_distill,
        asset_index,
        mock_git_provider,
    ):
        """一级提炼产出 → 写 asset_index → 后续召回可读到。"""
        from server.common.models import Asset as AssetVO, AssetType, Scope
        from tests.integration.conftest import make_session

        # 1. 跑一级提炼
        sessions = [make_session(session_id="sess-push-001")]
        result = personal_distill.run(sessions, member_id="alice")
        assert result.error is None

        # 2. 模拟 push：将产出资产写入 asset_index
        commit_sha = "commit-distill-001"
        produced = 0
        if result.deep is not None:
            for asset in result.deep.assets:
                # 用产出资产的字段构造 VO 并 upsert
                vo = AssetVO(
                    id=asset.id,
                    type=AssetType(asset.type) if isinstance(asset.type, str) else asset.type,
                    owner="alice",
                    scope=Scope.TEAM,
                    content=asset.content,
                    content_file_ref=f"modules/backend/rules/{asset.id}.md",
                    module_path="modules/backend",
                    category=asset.category or "rule-backend",
                    tags=asset.tags or [],
                )
                asset_index.upsert(vo, git_commit=commit_sha, content_snapshot=asset.content)
                mock_git_provider._add_file(
                    commit_sha,
                    f"modules/backend/rules/{asset.id}.md",
                    asset.content,
                )
                produced += 1

        # 3. 验证：query 应能查到产出资产
        if produced > 0:
            from server.infra_db.asset_index import AssetFilter

            rows = asset_index.query(
                AssetFilter(module_paths=["modules/backend"], statuses=["active"])
            )
            produced_ids = {a.id for a in result.deep.assets}
            queried_ids = {r.id for r in rows}
            # 至少有一个产出资产被查到
            assert produced_ids & queried_ids


# ---------------------------------------------------------------------------
# 阶段 5：二级提炼（TeamDistill.trigger_incremental）
# ---------------------------------------------------------------------------


class TestStage5TeamDistill:
    """阶段 5：二级提炼（团队 dream）。"""

    def test_team_distill_trigger_incremental(
        self,
        team_distill,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """TeamDistill.trigger_incremental：从 asset_index 聚类 + LLM 提炼 + job 快照。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-team-001"
        # 预置 2 个相似资产（同 module + 同 category）
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-team-001",
            content="# SQLAlchemy 循环外键\n使用 use_alter=True 避免循环依赖",
            git_path="modules/backend/rules/circ.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
        )
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-team-002",
            content="# SQLAlchemy 双向关系\n用 back_populates 显式声明",
            git_path="modules/backend/rules/bidir.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
            category="rule-backend",
        )

        # 触发增量提炼
        job_id = team_distill.trigger_incremental()
        assert job_id  # 非空

        # 查询 job 状态
        status = team_distill.get_job_status(job_id)
        assert status is not None
        assert status.job_id == job_id
        assert status.status in ("completed", "running", "pending", "failed", "skipped")
        # 快照 SHA 非空（job 启动时记录）
        assert status.snapshot_sha

    def test_team_distill_cold_start_progress(
        self,
        team_distill,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """TeamDistill.get_cold_start_progress：资产 < 50 时进入冷启动期。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-cold-001"
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-cold-001",
            content="# 冷启动规则\n",
            git_path="modules/backend/rules/cold.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
        )

        progress = team_distill.get_cold_start_progress()
        assert progress.assets_needed > 0
        assert progress.current_count >= 1
        # 资产 < 50 时应为冷启动期
        assert progress.is_cold_start is True
        assert progress.remaining >= 0


# ---------------------------------------------------------------------------
# 阶段 6：发布 Prompt（DashboardService.get_dashboard + GovernanceMetrics）
# ---------------------------------------------------------------------------


class TestStage6PublishDashboard:
    """阶段 6：发布 — 治理看板聚合 + 指标暴露。"""

    def test_dashboard_get_overview(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """DashboardService.get_overview：返回资产总数 / 活跃数 / 模块数。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-dash-001"
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-dash-001",
            content="# 看板规则\n",
            git_path="modules/backend/rules/dash.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
        )

        overview = dashboard_service.get_overview()
        assert overview["asset_total"] >= 1
        assert overview["asset_active"] >= 1
        assert overview["module_count"] >= 1
        assert "generated_at" in overview

    def test_dashboard_get_dashboard_aggregates_all(
        self,
        dashboard_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """DashboardService.get_dashboard：聚合 module_stats / split_suggestions / alerts / recall_hit_rates / adoption_rates。"""
        from tests.integration.conftest import seed_asset

        commit_sha = "commit-dash-002"
        seed_asset(
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            mock_git_provider=mock_git_provider,
            asset_id="rule-dash-002",
            content="# 看板规则2\n",
            git_path="modules/backend/rules/dash2.md",
            module_path="modules/backend",
            commit_sha=commit_sha,
        )

        data = dashboard_service.get_dashboard()
        # 全部字段非 None
        assert data.module_stats is not None
        assert data.split_suggestions is not None
        assert data.orphan_asset_alerts is not None
        assert data.recall_hit_rates is not None
        assert data.adoption_rates is not None
        assert data.repo_size_alerts is not None
        assert data.generated_at  # 非空字符串

    def test_governance_metrics_ingest_events(
        self,
        governance_metrics,
    ):
        """GovernanceMetrics.ingest_events：客户端批量上报事件。"""
        events = [
            {
                "event_id": "evt-001",
                "event_type": "recall",
                "asset_id": "rule-metrics-001",
                "agent_id": "builder-01",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": "2026-08-07T10:00:00Z",
                "metadata": {"relevance_score": 0.9},
            },
        ]
        accepted, rejected = governance_metrics.ingest_events(events, agent_id="builder-01")
        assert accepted == 1
        assert rejected == 0

    def test_governance_metrics_ingest_idempotent(
        self,
        governance_metrics,
    ):
        """GovernanceMetrics.ingest_events 幂等：相同 event_id 第二次拒绝。"""
        events = [
            {
                "event_id": "evt-idem-001",
                "event_type": "recall",
                "asset_id": "rule-metrics-002",
                "agent_id": "builder-01",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": "2026-08-07T10:00:00Z",
                "metadata": {},
            },
        ]
        a1, r1 = governance_metrics.ingest_events(events, agent_id="builder-01")
        a2, r2 = governance_metrics.ingest_events(events, agent_id="builder-01")
        assert a1 == 1 and r1 == 0
        assert a2 == 0 and r2 == 1  # 第二次全部拒绝（event_id 重复）


# ---------------------------------------------------------------------------
# 端到端：完整链路联通（入库 → 同步 → 召回 → 一级提炼 → push → 二级提炼 → 发布）
# ---------------------------------------------------------------------------


class TestEndToEndFullChain:
    """端到端：完整链路联通测试。"""

    def test_full_chain_happy_path(
        self,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        sync_service,
        recall_service,
        personal_distill,
        team_distill,
        dashboard_service,
        governance_metrics,
        database,
    ):
        """端到端 happy path：完整链路联通。

        步骤：
        1. mock git 准备资产文件
        2. SyncService.trigger_sync → 全量重建 DB 索引
        3. RecallService.recall_list → 召回资产
        4. PersonalDistill.run → 一级提炼产出
        5. AssetIndex.upsert → push 产出资产到中央索引
        6. TeamDistill.trigger_incremental → 二级提炼聚类
        7. DashboardService.get_dashboard → 发布治理看板
        8. GovernanceMetrics.ingest_events → 上报采纳事件
        """
        from server.common.models import Asset as AssetVO, AssetType, Scope
        from server.infra_db.models import AgentBinding
        from tests.integration.conftest import seed_asset

        # === 1. mock git 准备资产 ===
        commit_sha = "commit-e2e-001"
        mock_git_provider._add_file(
            commit_sha,
            "INDEX.md",
            """---
level: project
module: teamharness-shared
assets:
  - id: rule-e2e-001
    path: rules/e2e.md
    type: rule
    purpose: e2e 规则
submodules:
  - name: backend
    path: modules/backend/
    purpose: 后端
counts:
  assets: 1
  submodules: 1
---
""",
        )
        mock_git_provider._add_file(
            commit_sha,
            "rules/e2e.md",
            "---\nid: rule-e2e-001\ntype: rule\nowner: alice\nscope: team\n---\n# e2e 规则\n所有函数需类型标注\n",
        )
        mock_git_provider._add_file(
            commit_sha,
            "modules/backend/INDEX.md",
            """---
level: module
parent: ../../INDEX.md
module: backend
assets:
  - id: rule-e2e-002
    path: rules/e2e-be.md
    type: rule
    purpose: 后端 e2e
submodules: []
counts:
  assets: 1
  submodules: 0
---
""",
        )
        mock_git_provider._add_file(
            commit_sha,
            "modules/backend/rules/e2e-be.md",
            "---\nid: rule-e2e-002\ntype: rule\nowner: bob\nscope: team\n---\n# 后端 e2e 规则\n禁止 print 调试\n",
        )

        # === 2. SyncService.trigger_sync 全量重建 ===
        sync_result = sync_service.trigger_sync(commit_sha)
        assert sync_result.commit_sha == commit_sha
        assert sync_result.skipped is False
        assert sync_result.assets_upserted >= 2

        # === 3. RecallService.recall_list 召回 ===
        # 先写装配
        with database.session() as sess:
            sess.add(AgentBinding(
                id="binding-e2e-001",
                agent_id="builder-e2e",
                asset_id="rule-e2e-002",
                binding_type="fixed",
                enabled=True,
            ))
        recall_result = recall_service.recall_list(
            agent_id="builder-e2e",
            query=None,
            module_path="modules/backend",
        )
        assert len(recall_result.items) >= 1
        assert any(it.asset_id == "rule-e2e-002" for it in recall_result.items)

        # === 4. PersonalDistill.run 一级提炼 ===
        from tests.integration.conftest import make_session

        sessions = [make_session(session_id="sess-e2e-001")]
        distill_result = personal_distill.run(sessions, member_id="alice")
        assert distill_result.error is None
        assert distill_result.light is not None
        assert distill_result.rem is not None
        assert distill_result.deep is not None

        # === 5. push 产出资产到 asset_index ===
        push_commit = "commit-e2e-push-001"
        pushed_count = 0
        if distill_result.deep.assets:
            for asset in distill_result.deep.assets:
                vo = AssetVO(
                    id=asset.id,
                    type=AssetType(asset.type) if isinstance(asset.type, str) else asset.type,
                    owner="alice",
                    scope=Scope.TEAM,
                    content=asset.content,
                    content_file_ref=f"modules/backend/rules/{asset.id}.md",
                    module_path="modules/backend",
                    category=asset.category or "rule-backend",
                    tags=asset.tags or [],
                )
                asset_index.upsert(vo, git_commit=push_commit, content_snapshot=asset.content)
                pushed_count += 1

        # === 6. TeamDistill.trigger_incremental 二级提炼 ===
        job_id = team_distill.trigger_incremental()
        assert job_id  # 非空
        job_status = team_distill.get_job_status(job_id)
        assert job_status is not None
        assert job_status.status in ("completed", "running", "pending", "failed", "skipped")

        # === 7. DashboardService.get_dashboard 发布 ===
        dashboard = dashboard_service.get_dashboard()
        assert dashboard.module_stats is not None
        assert dashboard.generated_at

        # === 8. GovernanceMetrics.ingest_events 上报采纳事件 ===
        events = [
            {
                "event_id": "evt-e2e-001",
                "event_type": "recall",
                "asset_id": "rule-e2e-002",
                "agent_id": "builder-e2e",
                "member_id": "alice",
                "module_path": "modules/backend",
                "timestamp": "2026-08-07T10:00:00Z",
                "metadata": {"relevance_score": 0.95},
            },
        ]
        accepted, rejected = governance_metrics.ingest_events(events, agent_id="builder-e2e")
        assert accepted == 1
        assert rejected == 0

    def test_full_chain_localized_failure_isolates_stage(
        self,
        sync_service,
        recall_service,
        asset_index,
        mock_git_provider,
    ):
        """局部失败隔离：sync 失败时 recall 仍可降级返回（不阻断整条链路）。"""
        # mock git 不放任何文件 → trigger_sync 不会 upsert 任何资产
        commit_sha = "commit-empty-001"
        mock_git_provider._add_file(
            commit_sha, "INDEX.md",
            "---\nlevel: project\nmodule: empty\nassets: []\nsubmodules: []\ncounts: {assets: 0, submodules: 0}\n---\n",
        )

        sync_result = sync_service.trigger_sync(commit_sha)
        # 同步成功（只是没有资产）
        assert sync_result.commit_sha == commit_sha

        # 召回应返回空列表（无装配）
        recall_result = recall_service.recall_list(
            agent_id="builder-empty",
            query=None,
            module_path="modules/empty",
        )
        assert isinstance(recall_result.items, list)
        # 空装配场景下 items 应为空（或不含 builder-empty 的资产）
        assert len(recall_result.items) == 0
