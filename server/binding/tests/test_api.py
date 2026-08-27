"""SubTask 5.11 API 契约 smoke test — 验证公共 API 端点可响应。

作为公共 API 提供方（依赖方：Agent 6 category-suggest/鉴权、Agent 10 集成测试），
本测试验证：
1. FastAPI 路由可正确注册
2. configure_binding_api 注入 Service 后端点可响应
3. 响应格式符合占位 API 契约
"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.binding.api import (
    auth_router,
    binding_router,
    category_router,
    configure_binding_api,
    tool_router,
)
from server.binding.auth_service import AgentApiKeyService
from server.binding.binding_service import BindingService
from server.binding.category_suggest import CategorySuggestService
from server.binding.tool_review import ToolReviewService
from server.binding.tests.conftest import insert_asset


@pytest.fixture
def app_and_client(database, sample_repo):
    """构造含全部 binding 域路由的 FastAPI app + TestClient。"""
    binding = BindingService(database)
    category = CategorySuggestService(database, repo_root=sample_repo, llm=None)
    auth = AgentApiKeyService(database)
    # tool review 需要公钥，这里用 None（签名验证测试在 test_tool_review.py 已覆盖）
    tool = ToolReviewService(database, trusted_reviewers={"alice"}, public_key=None)

    app = FastAPI()
    app.include_router(binding_router)
    app.include_router(category_router)
    app.include_router(auth_router)
    app.include_router(tool_router)
    configure_binding_api(
        binding=binding, category=category, auth=auth, tool=tool
    )
    client = TestClient(app)
    return app, client, binding


class TestBindingAPI:
    """BindingService 公共 API 契约。"""

    def test_create_binding_endpoint(self, app_and_client):
        _, client, binding = app_and_client
        insert_asset(binding._db, id="r1")
        resp = client.post(
            "/v1/binding/create",
            json={
                "agent_id": "a1",
                "asset_id": "r1",
                "type": "fixed",
                "priority": "high",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "binding_id" in data
        assert data["binding_id"].startswith("bind-")

    def test_list_bindings_endpoint(self, app_and_client):
        _, client, binding = app_and_client
        insert_asset(binding._db, id="r1")
        client.post(
            "/v1/binding/create",
            json={"agent_id": "a1", "asset_id": "r1"},
        )
        resp = client.get("/v1/binding/list", params={"agent_id": "a1"})
        assert resp.status_code == 200
        data = resp.json()
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["asset_id"] == "r1"
        # 契约字段齐全
        for field in (
            "id",
            "agent_id",
            "asset_id",
            "binding_type",
            "priority",
            "enabled",
            "binding_version",
        ):
            assert field in data[0]

    def test_auto_bind_endpoint(self, app_and_client):
        _, client, binding = app_and_client
        insert_asset(binding._db, id="r1", category="rule-backend")
        # 注册 routing
        client.post(
            "/v1/binding/routing",
            json={
                "task_type": "pr-review",
                "category": "rule-backend",
                "asset_id": "r1",
            },
        )
        resp = client.post(
            "/v1/binding/auto",
            json={
                "agent_id": "a1",
                "task_type": "pr-review",
                "category": "rule-backend",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["matched_count"] == 1
        assert data["bound_count"] == 1
        assert len(data["binding_ids"]) == 1

    def test_create_binding_invalid_type_400(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/v1/binding/create",
            json={
                "agent_id": "a1",
                "asset_id": "r1",
                "type": "invalid-type",
            },
        )
        assert resp.status_code == 400


class TestCategoryAPI:
    """CategorySuggestService 公共 API 契约。"""

    def test_suggest_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/v1/category/suggest",
            json={"content": "lint 规则", "module_path": "modules/backend"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "candidates" in data
        assert "used_fallback" in data
        assert len(data["candidates"]) == 3
        # 候选字段齐全
        for field in ("category", "confidence", "rationale"):
            assert field in data["candidates"][0]

    def test_validate_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/v1/category/validate",
            json={"category": "rule-backend"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["category"] == "rule-backend"
        assert data["format_valid"] is True
        assert data["registered_in_yaml"] is True
        assert data["module_indexed"] is True
        assert data["ok"] is True

    def test_posthoc_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/v1/category/posthoc",
            json={
                "changed_assets": [["rules/x.md", "rule-unknown"]],
                "commit_sha": "abc123",
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["commit_sha"] == "abc123"
        assert data["checked_assets"] == 1
        assert data["pending_created"] == 1
        assert len(data["pending_ids"]) == 1


class TestAuthAPI:
    """AgentApiKeyService 公共 API 契约。"""

    def test_issue_apikey_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        resp = client.post(
            "/v1/auth/apikey",
            json={"member_id": "alice", "agent_id": "agent-1"},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert "api_key" in data
        assert data["api_key"].startswith("th_")
        assert data["agent_id"] == "agent-1"
        assert "key_id" in data
        assert "key_prefix" in data

    def test_lookup_apikey_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        # 先颁发
        issue = client.post(
            "/v1/auth/apikey",
            json={"member_id": "alice", "agent_id": "agent-1"},
        ).json()
        # 反查
        resp = client.post(
            "/v1/auth/apikey/lookup",
            json={"api_key": issue["api_key"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["agent_id"] == "agent-1"

    def test_rotate_apikey_endpoint(self, app_and_client):
        _, client, _ = app_and_client
        issue = client.post(
            "/v1/auth/apikey",
            json={"member_id": "alice", "agent_id": "agent-1"},
        ).json()
        resp = client.post(
            "/v1/auth/apikey/rotate",
            json={"key_id": issue["key_id"]},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["api_key"] != issue["api_key"]
        assert data["agent_id"] == "agent-1"


class TestToolReviewAPI:
    """ToolReviewService 公共 API 契约。"""

    def test_review_endpoint_rejects_unsigned(self, app_and_client):
        _, client, _ = app_and_client
        content = (
            "---\nid: tool-x\ntype: tool\n---\nprint('hello')"
        )
        resp = client.post(
            "/v1/tool/review",
            json={
                "pr_id": "pr-1",
                "asset_path": "tools/x.py",
                "commit_sha": "abc",
                "content": content,
                "approvers": ["alice"],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["decision"] == "rejected"
        assert data["signature_present"] is False
        assert "record_id" in data


class TestServiceNotConfigured:
    """未注入 Service → 503。"""

    def test_binding_not_configured_503(self):
        # 确保全局变量已重置（即使前测试 teardown 失败也安全）
        import server.binding.api as api_mod

        api_mod._BINDING = None
        api_mod._CATEGORY = None
        api_mod._AUTH = None
        api_mod._TOOL = None

        from server.binding.api import binding_router

        app = FastAPI()
        app.include_router(binding_router)
        client = TestClient(app)
        resp = client.post(
            "/v1/binding/create",
            json={"agent_id": "a1", "asset_id": "r1"},
        )
        assert resp.status_code == 503
