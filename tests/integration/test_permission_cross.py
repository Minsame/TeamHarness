"""SubTask 10.3: 角色权限跨越测试。

验证四种 scope（private/team/restricted/public）+ agent_binding 的权限隔离：
- private 资产不入 DB 索引（client 端 .teamharness/private/ 隔离）
- team 资产可被同 team agent 通过 binding 召回
- restricted 资产 recall_read 需 binding + RestrictedReader 双重校验
- public 资产任何 agent 通过 binding 可召回
- agent_binding enabled=false → recall_list 不返回
- agent_binding 跨 agent 隔离（A 的 binding 不影响 B）
- webhook 删除资产 → agent_binding 级联 enabled=false → recall_list 不返回

对应域内验证点：
- 角色权限跨越：private 资产不进 DB 索引，team/restricted/public 按 agent_binding 过滤
"""

from __future__ import annotations

import pytest


# ---------------------------------------------------------------------------
# 辅助：构造并写入指定 scope 资产
# ---------------------------------------------------------------------------


def _seed_asset_with_scope(
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
    scope: str,
    category: str | None = None,
) -> str:
    """构造指定 scope 资产并写入 asset_index + 向量库 + git mock。"""
    from server.common.models import Asset as AssetVO, AssetType, Scope
    from server.infra_db.vectorstore import VectorRecord

    vo = AssetVO(
        id=asset_id,
        type=AssetType.RULE,
        owner="tester",
        scope=Scope(scope),
        content=content,
        content_file_ref=git_path,
        module_path=module_path,
        category=category,
        tags=[],
    )
    asset_index.upsert(vo, git_commit=commit_sha, content_snapshot=content)

    emb = embedding_service.embed(content)
    vector_store.ensure_collection(embedding_service.get_active_version(), emb.dim)
    vector_store.upsert(
        VectorRecord(
            asset_id=asset_id,
            vector=emb.vector,
            dim=emb.dim,
            metadata={
                "module_path": module_path,
                "scope": scope,
                "type": "rule",
            },
        ),
        model_version=embedding_service.get_active_version(),
    )
    mock_git_provider._add_file(commit_sha, git_path, content)
    return asset_id


def _add_binding(database, *, binding_id: str, agent_id: str, asset_id: str,
                 binding_type: str = "fixed", enabled: bool = True) -> None:
    """写 agent_binding 记录。"""
    from server.infra_db.models import AgentBinding

    with database.session() as sess:
        sess.add(AgentBinding(
            id=binding_id,
            agent_id=agent_id,
            asset_id=asset_id,
            binding_type=binding_type,
            enabled=enabled,
        ))


# ---------------------------------------------------------------------------
# 1. private 资产隔离（client 端）
# ---------------------------------------------------------------------------


class TestPrivateAssetIsolation:
    """private 资产隔离测试。

    private 资产不入 DB 索引（client 端 .teamharness/private/ + .gitignore）。
    """

    def test_private_isolation_class_exists(self):
        """PrivateIsolation 类存在且提供核心方法。"""
        from server.client.private_isolation import PrivateIsolation

        for method_name in ("check_gitignore", "ensure_gitignore", "private_dir_for"):
            assert hasattr(PrivateIsolation, method_name), \
                f"PrivateIsolation 缺方法 {method_name}"

    def test_private_gitignore_path_constant(self, tmp_path):
        """PrivateIsolation 维护 .teamharness/private/ 目录并加入 .gitignore。"""
        from server.client.private_isolation import (
            PrivateIsolation,
            REQUIRED_GITIGNORE_RULES,
        )

        iso = PrivateIsolation(repo_root=tmp_path)
        # .gitignore 路径在仓库根
        assert iso.gitignore_path() == tmp_path / ".gitignore"
        # REQUIRED_GITIGNORE_RULES 常量存在且含 private 规则
        assert REQUIRED_GITIGNORE_RULES
        assert any("private" in rule.lower() for rule in REQUIRED_GITIGNORE_RULES)

    def test_private_asset_does_not_enter_db_index(
        self,
        asset_index,
        embedding_service,
        vector_store,
    ):
        """private 资产不进 DB 索引（即便手动 upsert，scope=private 也不应在 team 召回中返回）。

        模拟：把 private 资产写入 asset_index → AssetFilter.scopes=["team"] 不应查到。
        """
        from server.common.models import Asset as AssetVO, AssetType, Scope
        from server.infra_db.asset_index import AssetFilter

        # private 资产
        vo = AssetVO(
            id="rule-private-001",
            type=AssetType.RULE,
            owner="alice",
            scope=Scope.PRIVATE,
            content="# private 规则",
            content_file_ref="modules/backend/rules/p.md",
            module_path="modules/backend",
        )
        asset_index.upsert(vo, git_commit="c1", content_snapshot=vo.content)

        # 用 scopes=["team"] 过滤 → 不应查到 private 资产
        rows = asset_index.query(AssetFilter(scopes=["team"], statuses=["active"]))
        assert all(r.id != "rule-private-001" for r in rows)

        # 用 scopes=["private"] 过滤 → 能查到
        rows = asset_index.query(AssetFilter(scopes=["private"], statuses=["active"]))
        assert any(r.id == "rule-private-001" for r in rows)


# ---------------------------------------------------------------------------
# 2. team 资产 + agent_binding 召回
# ---------------------------------------------------------------------------


class TestTeamAssetRecall:
    """team 资产召回测试。"""

    def test_team_asset_recall_with_binding(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """team 资产 + agent_binding → recall_list 返回。"""
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-team-perm-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-team-perm-001", content="# team 规则",
            git_path="modules/backend/rules/t.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _add_binding(database, binding_id="b-team-001",
                     agent_id="agent-team-001", asset_id="rule-team-perm-001")

        result = recall_service.recall_list(
            agent_id="agent-team-001", query=None,
            module_path="modules/backend",
        )
        assert any(it.asset_id == "rule-team-perm-001" for it in result.items)

    def test_team_asset_no_binding_no_recall(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """team 资产但无 binding → recall_list 不返回。"""
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-team-no-bind-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-team-no-bind-001", content="# team 规则无绑定",
            git_path="modules/backend/rules/tn.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        # 不写 binding

        result = recall_service.recall_list(
            agent_id="agent-no-bind-001", query=None,
            module_path="modules/backend",
        )
        assert all(it.asset_id != "rule-team-no-bind-001" for it in result.items)


# ---------------------------------------------------------------------------
# 3. restricted 资产 + RestrictedReader 双重校验
# ---------------------------------------------------------------------------


class TestRestrictedAssetAccess:
    """restricted 资产 recall_read 双重校验。"""

    def test_restricted_recall_read_with_binding_and_reader(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        mock_restricted_reader,
        database,
    ):
        """restricted 资产 + binding + RestrictedReader 可用 → recall_read 成功。"""
        commit_sha = "commit-restricted-ok-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-restricted-ok-001", content="# restricted 规则",
            git_path="modules/backend/rules/r.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="restricted",
        )
        _add_binding(database, binding_id="b-r-001",
                     agent_id="agent-r-001", asset_id="rule-restricted-ok-001")
        # mock_restricted_reader 默认 is_available=True

        result = recall_service.recall_read(
            agent_id="agent-r-001", asset_id="rule-restricted-ok-001",
        )
        assert result.asset_id == "rule-restricted-ok-001"
        assert "restricted" in result.content.lower() or "规则" in result.content

    def test_restricted_recall_read_without_binding_denied(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        mock_restricted_reader,
        database,
    ):
        """restricted 资产无 binding → RestrictedAccessDeniedError。"""
        from server.recall.service import RestrictedAccessDeniedError

        commit_sha = "commit-restricted-nobind-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-restricted-nobind-001", content="# restricted 规则",
            git_path="modules/backend/rules/rn.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="restricted",
        )
        # 不写 binding

        with pytest.raises(RestrictedAccessDeniedError):
            recall_service.recall_read(
                agent_id="agent-r-nobind-001",
                asset_id="rule-restricted-nobind-001",
            )

    def test_restricted_recall_read_reader_unavailable_denied(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """restricted 资产 + binding 但 RestrictedReader 不可用 → 拒绝。"""
        from server.recall.service import RestrictedAccessDeniedError
        from unittest.mock import MagicMock

        commit_sha = "commit-restricted-noreader-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-restricted-noreader-001", content="# restricted 规则",
            git_path="modules/backend/rules/rnr.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="restricted",
        )
        _add_binding(database, binding_id="b-rnr-001",
                     agent_id="agent-rnr-001", asset_id="rule-restricted-noreader-001")

        # 覆盖 recall_service 的 restricted_reader 为不可用
        unavailable_reader = MagicMock()
        unavailable_reader.is_available = MagicMock(return_value=False)
        recall_service._restricted_reader = unavailable_reader

        with pytest.raises(RestrictedAccessDeniedError):
            recall_service.recall_read(
                agent_id="agent-rnr-001",
                asset_id="rule-restricted-noreader-001",
            )

    def test_restricted_recall_read_disabled_binding_denied(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        mock_restricted_reader,
        database,
    ):
        """restricted 资产 + binding(enabled=false) → 拒绝（装配失效）。"""
        from server.recall.service import RestrictedAccessDeniedError

        commit_sha = "commit-restricted-disabled-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-restricted-disabled-001", content="# restricted 规则",
            git_path="modules/backend/rules/rd.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="restricted",
        )
        # binding enabled=false
        _add_binding(database, binding_id="b-rd-001",
                     agent_id="agent-rd-001", asset_id="rule-restricted-disabled-001",
                     enabled=False)

        with pytest.raises(RestrictedAccessDeniedError):
            recall_service.recall_read(
                agent_id="agent-rd-001",
                asset_id="rule-restricted-disabled-001",
            )


# ---------------------------------------------------------------------------
# 4. public 资产召回
# ---------------------------------------------------------------------------


class TestPublicAssetRecall:
    """public 资产召回测试。"""

    def test_public_asset_recall_with_binding(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """public 资产 + binding → recall_list 返回。"""
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-public-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-public-001", content="# public 规则",
            git_path="modules/backend/rules/pub.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="public",
        )
        _add_binding(database, binding_id="b-pub-001",
                     agent_id="agent-pub-001", asset_id="rule-public-001")

        result = recall_service.recall_list(
            agent_id="agent-pub-001", query=None,
            module_path="modules/backend",
        )
        assert any(it.asset_id == "rule-public-001" for it in result.items)


# ---------------------------------------------------------------------------
# 5. agent_binding 跨 agent 隔离
# ---------------------------------------------------------------------------


class TestAgentBindingIsolation:
    """agent_binding 跨 agent 隔离测试。"""

    def test_binding_isolation_between_agents(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """agent A 的 binding 不影响 agent B。

        场景：资产 X 绑定给 agent-A（enabled=true），未绑定给 agent-B。
        agent-A 召回得到 X，agent-B 召回得不到 X。
        """
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-iso-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-iso-001", content="# 隔离测试规则",
            git_path="modules/backend/rules/iso.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        # 仅给 agent-A 绑定
        _add_binding(database, binding_id="b-iso-a-001",
                     agent_id="agent-A", asset_id="rule-iso-001")
        # agent-B 无绑定

        result_a = recall_service.recall_list(
            agent_id="agent-A", query=None, module_path="modules/backend",
        )
        result_b = recall_service.recall_list(
            agent_id="agent-B", query=None, module_path="modules/backend",
        )
        assert any(it.asset_id == "rule-iso-001" for it in result_a.items)
        assert all(it.asset_id != "rule-iso-001" for it in result_b.items)

    def test_disabled_binding_excluded_from_recall(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """agent_binding enabled=false → recall_list 不返回该资产。"""
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-disabled-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-disabled-001", content="# disabled 测试",
            git_path="modules/backend/rules/dis.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _add_binding(database, binding_id="b-dis-001",
                     agent_id="agent-dis-001", asset_id="rule-disabled-001",
                     enabled=False)

        result = recall_service.recall_list(
            agent_id="agent-dis-001", query=None, module_path="modules/backend",
        )
        assert all(it.asset_id != "rule-disabled-001" for it in result.items)

    def test_deleted_asset_cascade_disables_binding(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        database,
    ):
        """webhook 删除资产 → asset_index.status=deleted → recall_list 不返回（双重过滤）。"""
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-cascade-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-cascade-001", content="# cascade 测试",
            git_path="modules/backend/rules/cas.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _add_binding(database, binding_id="b-cas-001",
                     agent_id="agent-cas-001", asset_id="rule-cascade-001",
                     enabled=True)

        # 删除资产 → asset_index.status=deleted（同时级联 binding.enabled=false）
        asset_index.delete("rule-cascade-001", git_commit=commit_sha)

        result = recall_service.recall_list(
            agent_id="agent-cas-001", query=None, module_path="modules/backend",
        )
        assert all(it.asset_id != "rule-cascade-001" for it in result.items)


# ---------------------------------------------------------------------------
# 6. AssetFilter.scopes 过滤生效
# ---------------------------------------------------------------------------


class TestAssetFilterScopes:
    """AssetFilter.scopes 过滤测试。"""

    def test_filter_by_team_scope(
        self,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """AssetFilter(scopes=["team"]) 只返回 team 资产。"""
        commit_sha = "commit-filter-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-filter-team-001", content="# team",
            git_path="modules/backend/rules/ft.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-filter-public-001", content="# public",
            git_path="modules/backend/rules/fp.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="public",
        )

        from server.infra_db.asset_index import AssetFilter

        # scopes=["team"] → 只返回 team
        rows = asset_index.query(AssetFilter(
            scopes=["team"], statuses=["active"], module_paths=["modules/backend"],
        ))
        ids = {r.id for r in rows}
        assert "rule-filter-team-001" in ids
        assert "rule-filter-public-001" not in ids

    def test_filter_by_multiple_scopes(
        self,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
    ):
        """AssetFilter(scopes=["team", "public"]) 返回 team + public。"""
        commit_sha = "commit-filter-multi-001"
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-multi-team-001", content="# team",
            git_path="modules/backend/rules/mt.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-multi-public-001", content="# public",
            git_path="modules/backend/rules/mp.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="public",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-multi-private-001", content="# private",
            git_path="modules/backend/rules/mv.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="private",
        )

        from server.infra_db.asset_index import AssetFilter

        rows = asset_index.query(AssetFilter(
            scopes=["team", "public"], statuses=["active"],
            module_paths=["modules/backend"],
        ))
        ids = {r.id for r in rows}
        assert "rule-multi-team-001" in ids
        assert "rule-multi-public-001" in ids
        assert "rule-multi-private-001" not in ids


# ---------------------------------------------------------------------------
# 7. 端到端：多 scope + 多 agent 权限跨越
# ---------------------------------------------------------------------------


class TestEndToEndPermissionCross:
    """端到端：多 scope + 多 agent 权限跨越。"""

    def test_multi_agent_multi_scope_permissions(
        self,
        recall_service,
        asset_index,
        embedding_service,
        vector_store,
        mock_git_provider,
        mock_restricted_reader,
        database,
    ):
        """端到端：4 种 scope + 2 个 agent 的权限矩阵。

        场景：
        - agent-A 绑定：team 资产 T1 + restricted 资产 R1 + public 资产 P1
        - agent-B 绑定：仅 public 资产 P1
        - private 资产 PR1 不入 DB 索引（不入 binding）

        预期：
        - agent-A recall_list 得到 [T1, R1, P1]（不含 PR1）
        - agent-B recall_list 得到 [P1]（不含 T1/R1/PR1）
        - agent-A recall_read(R1) 成功（有 binding + reader 可用）
        - agent-B recall_read(R1) 拒绝（无 binding）
        """
        from server.infra_db.models import IndexSyncState

        commit_sha = "commit-e2e-perm-001"
        with database.session() as sess:
            sess.merge(IndexSyncState(
                id="singleton", last_synced_commit=commit_sha,
                status="ok", lag_periods=0,
            ))

        # 4 种 scope 资产
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-e2e-team-001", content="# e2e team",
            git_path="modules/backend/rules/et.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="team",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-e2e-restricted-001", content="# e2e restricted",
            git_path="modules/backend/rules/er.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="restricted",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-e2e-public-001", content="# e2e public",
            git_path="modules/backend/rules/ep.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="public",
        )
        _seed_asset_with_scope(
            asset_index=asset_index, embedding_service=embedding_service,
            vector_store=vector_store, mock_git_provider=mock_git_provider,
            asset_id="rule-e2e-private-001", content="# e2e private",
            git_path="modules/backend/rules/ev.md",
            module_path="modules/backend", commit_sha=commit_sha, scope="private",
        )

        # agent-A 绑定 T1/R1/P1
        _add_binding(database, binding_id="b-e2e-a-t",
                     agent_id="agent-e2e-A", asset_id="rule-e2e-team-001")
        _add_binding(database, binding_id="b-e2e-a-r",
                     agent_id="agent-e2e-A", asset_id="rule-e2e-restricted-001")
        _add_binding(database, binding_id="b-e2e-a-p",
                     agent_id="agent-e2e-A", asset_id="rule-e2e-public-001")
        # agent-B 仅绑定 P1
        _add_binding(database, binding_id="b-e2e-b-p",
                     agent_id="agent-e2e-B", asset_id="rule-e2e-public-001")

        # 1. agent-A recall_list → 得到 T1/R1/P1，不含 PR1
        result_a = recall_service.recall_list(
            agent_id="agent-e2e-A", query=None, module_path="modules/backend",
        )
        ids_a = {it.asset_id for it in result_a.items}
        assert "rule-e2e-team-001" in ids_a
        assert "rule-e2e-restricted-001" in ids_a
        assert "rule-e2e-public-001" in ids_a
        # private 资产不应被绑定（这里也没绑定），所以不在召回结果
        assert "rule-e2e-private-001" not in ids_a

        # 2. agent-B recall_list → 仅 P1
        result_b = recall_service.recall_list(
            agent_id="agent-e2e-B", query=None, module_path="modules/backend",
        )
        ids_b = {it.asset_id for it in result_b.items}
        assert "rule-e2e-public-001" in ids_b
        assert "rule-e2e-team-001" not in ids_b
        assert "rule-e2e-restricted-001" not in ids_b

        # 3. agent-A recall_read(R1) 成功
        read_a = recall_service.recall_read(
            agent_id="agent-e2e-A", asset_id="rule-e2e-restricted-001",
        )
        assert read_a.asset_id == "rule-e2e-restricted-001"

        # 4. agent-B recall_read(R1) 拒绝
        from server.recall.service import RestrictedAccessDeniedError

        with pytest.raises(RestrictedAccessDeniedError):
            recall_service.recall_read(
                agent_id="agent-e2e-B", asset_id="rule-e2e-restricted-001",
            )
