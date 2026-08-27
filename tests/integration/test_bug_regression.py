"""SubTask 10.6: Bug 触发测试回归用例。

为以下已知 bug / 遗留待办添加回归测试用例（标注来源）：
- 遗留待办 4：server/client/config.py int(pick(...)) 未容错（env=not-a-number 时抛 ValueError）
  [来源: Agent 4 标记 / 第四波 / 已由 Agent 6 修复 _coerce_int]
- 遗留待办 1：Agent 6 在线召回/category-suggest 切换真实调用验证（之前用占位）
  [来源: overview 遗留待办 / 第四波 / Agent 6 已切换真实调用]
- 遗留待办 2：Agent 5 的 LLMProvider 切换验证（Agent 7 已完成 LLMProvider）
  [来源: overview 遗留待办 / 第四波 / Agent 5 已支持协议注入]
- Agent 9 契约路由缺失：POST /v1/review/dedup + GET /v1/governance/dashboard HTTP 路由未注册
  [来源: Agent 10 / SubTask 10.2 / 第四波 / 已由 Agent 9 补注册路由，xfail 标记已移除]

对应域内验证点：用户报告 bug 的回归测试用例通过
"""

from __future__ import annotations

import inspect


# ---------------------------------------------------------------------------
# 遗留待办 4：config.py int 容错（已修复，回归验证）
# ---------------------------------------------------------------------------


class TestLegacyTodo4ConfigIntCoercion:
    """遗留待办 4 回归：env=not-a-number 时不抛 ValueError。

    原始 bug：server/client/config.py 的 int(pick(...)) 未容错，
    当 TEAMHARNESS_*_INTERVAL 等环境变量设为非数字字符串时抛 ValueError 导致启动崩溃。
    修复：引入 _coerce_int(value, default) 包裹所有 int 字段。
    """

    def test_coerce_int_not_a_number_returns_default(self):
        """_coerce_int 对非数字字符串返回 default，不抛 ValueError。"""
        from server.client.config import _coerce_int

        assert _coerce_int("not-a-number", default=99) == 99

    def test_coerce_int_none_returns_default(self):
        """_coerce_int 对 None 返回 default。"""
        from server.client.config import _coerce_int

        assert _coerce_int(None, default=42) == 42

    def test_coerce_int_valid_string_parsed(self):
        """_coerce_int 对合法数字字符串正确解析。"""
        from server.client.config import _coerce_int

        assert _coerce_int("42", default=99) == 42
        assert _coerce_int(0, default=99) == 0

    def test_coerce_int_float_string_returns_default(self):
        """_coerce_int 对浮点字符串返回 default（int('3.5') 抛 ValueError）。"""
        from server.client.config import _coerce_int

        # int("3.5") 抛 ValueError，_coerce_int 应捕获并返回 default
        assert _coerce_int("3.5", default=7) == 7

    def test_load_client_config_bad_env_no_crash(self, monkeypatch):
        """load_client_config 在坏 env 下不抛异常，回退默认值。

        模拟原始 bug触发场景：TEAMHARNESS_NETWORK_INTERVAL=not-a-number
        """
        from server.client.config import load_client_config

        # 注入非法 env 值
        monkeypatch.setenv("TEAMHARNESS_NETWORK_CHECK_INTERVAL", "not-a-number")
        monkeypatch.setenv("TEAMHARNESS_ADOPTION_FLUSH_INTERVAL", "also-bad")
        monkeypatch.setenv("TEAMHARNESS_REQUEST_TIMEOUT", "!!!")

        # 不抛 ValueError 即修复生效
        cfg = load_client_config(repo_root=".")
        # 非法 env 回退为默认值（非 None / 非 crash）
        assert cfg.network_check_interval_seconds is not None
        assert cfg.adoption_flush_interval_seconds is not None
        assert cfg.request_timeout_seconds is not None

    def test_load_client_config_valid_env(self, monkeypatch):
        """load_client_config 对合法 env 正确解析（回归不破坏正常路径）。"""
        from server.client.config import load_client_config

        monkeypatch.setenv("TEAMHARNESS_NETWORK_CHECK_INTERVAL", "120")
        cfg = load_client_config(repo_root=".")
        assert cfg.network_check_interval_seconds == 120


# ---------------------------------------------------------------------------
# 遗留待办 1：Agent 6 在线召回真实调用验证（已切换，回归验证）
# ---------------------------------------------------------------------------


class TestLegacyTodo1RecallClientRealCall:
    """遗留待办 1 回归：recall_client 切换真实 HTTP 调用（非占位）。

    原始状态：Agent 6 的 RecallClient 使用占位实现。
    切换后：_call_remote_list / _call_remote_read 通过 httpx 发真实 HTTP 请求，
    Agent 4 未就绪时自动降级到 mock_recall_list。
    """

    def test_recall_client_has_real_http_methods(self):
        """RecallClient 含真实 HTTP 调用方法（非占位）。"""
        from server.client.recall_client import RecallClient

        for method in ("_call_remote_list", "_call_remote_read"):
            assert hasattr(RecallClient, method), \
                f"RecallClient 缺方法 {method}（遗留待办 1 未切换真实调用）"

    def test_recall_client_source_uses_httpx(self):
        """recall_client 源码含 httpx（真实 HTTP 客户端）。"""
        from server.client import recall_client

        source = inspect.getsource(recall_client)
        assert "import httpx" in source or "from httpx" in source, \
            "recall_client 未使用 httpx（遗留待办 1 未切换真实调用）"

    def test_recall_client_has_fallback_degradation(self):
        """RecallClient 含降级路径（HTTP 失败时降级到 mock）。"""
        from server.client import recall_client

        source = inspect.getsource(recall_client)
        # 降级方法存在
        assert "mock_recall_list" in source or "fallback" in source.lower() or \
            "degrade" in source.lower(), \
            "recall_client 缺降级路径（HTTP 失败时无兜底）"


# ---------------------------------------------------------------------------
# 遗留待办 2：Agent 5 LLMProvider 切换验证（协议注入机制）
# ---------------------------------------------------------------------------


class TestLegacyTodo2LLMProviderSwitch:
    """遗留待办 2 回归：Agent 5 的 LLMProvider 注入切换机制。

    原始状态：Agent 5 的 CategorySuggestService 用占位 LLM。
    切换后：通过 LLMChatProtocol 协议注入，Agent 7 的 LLMProvider 可直接注入。
    """

    def test_llm_protocol_definable(self):
        """LLMChatProtocol 协议存在（Agent 7 LLMProvider 可注入）。"""
        from server.binding.llm import LLMChatProtocol

        assert LLMChatProtocol is not None

    def test_category_suggest_with_injected_llm_not_fallback(self):
        """注入真实 LLM 后 category_suggest 不走 fallback。

        直接调用 call_llm_for_category_suggestions，注入返回符合 schema 格式的 mock LLM，
        验证 used_fallback=False（LLMProvider 注入切换生效）。
        """
        from unittest.mock import MagicMock

        from server.binding.llm import call_llm_for_category_suggestions

        # 构造 mock LLM，返回符合 {"candidates":[...]} schema 的 JSON 字符串
        mock_llm = MagicMock()

        def _chat(messages, *, schema=None, **kwargs):
            return {
                "content": '{"candidates":[{"category":"rule-backend","confidence":0.9,"rationale":"匹配后端规则"}]}',
                "usage": {"total_tokens": 100},
                "model": "mock",
            }

        mock_llm.chat = MagicMock(side_effect=_chat)

        result = call_llm_for_category_suggestions(
            llm=mock_llm,
            content="# 后端 API 规范：所有接口必须返回 JSON",
            module_path="modules/backend",
        )
        # 注入了 LLM 且返回合法 JSON → 不应走 fallback
        assert result.used_fallback is False, \
            f"注入 LLM 后仍走 fallback（遗留待办 2 LLMProvider 切换未生效），error={result.error}"
        # 至少有 1 个候选
        assert len(result.candidates) >= 1
        assert result.candidates[0].category == "rule-backend"

    def test_category_suggest_with_none_llm_uses_fallback(self):
        """llm=None 时退化为启发式（fallback），标记 used_fallback=True。"""
        from server.binding.llm import call_llm_for_category_suggestions

        result = call_llm_for_category_suggestions(
            llm=None,
            content="一些内容",
            module_path="modules/backend",
        )
        assert result.used_fallback is True, \
            "llm=None 时未退化为 fallback"
        assert len(result.candidates) >= 1

    def test_budget_manager_in_budget_module(self):
        """BudgetManager 在 budget 模块（非 llm_provider 模块）。

        修正 SubTask 10.5 中 test_gap_2_1 的发现：
        BudgetManager 实际在 server.distill_personal.budget，不在 llm_provider。
        """
        from server.distill_personal.budget import BudgetManager

        assert BudgetManager is not None
        # 含默认预算配置方法
        assert hasattr(BudgetManager, "check_budget") or \
            hasattr(BudgetManager, "get_budget") or \
            hasattr(BudgetManager, "consume"), \
            "BudgetManager 缺预算管理方法（缺陷 2.1 LLM 成本归属）"


# ---------------------------------------------------------------------------
# Agent 9 契约路由注册回归（已修复，回归验证）
# ---------------------------------------------------------------------------


class TestAgent9ContractRoutes:
    """Agent 9 契约路由注册回归：两个 HTTP 路由已注册。

    路由（Agent 9 已补注册到 governance_router）：
    1. POST /v1/review/dedup — PR Review 语义去重（服务类 PRReviewDedupService 已实现）
    2. GET /v1/governance/dashboard — 治理看板（服务类 DashboardService 已实现）

    原契约缺失（服务类已就绪但 HTTP 路由未注册）已由 Agent 9 修复，
    本用例移除 xfail 标记后作正向回归验证：路由注册 → 非 404。
    """

    def test_review_dedup_http_route_registered(self):
        """POST /v1/review/dedup 路由应注册到 governance router。"""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from server.governance.metrics import governance_router

        app = FastAPI()
        app.include_router(governance_router)
        client = TestClient(app)

        # POST /v1/review/dedup 应返回非 404（405 也算路由已注册）
        resp = client.post("/v1/review/dedup", json={"pr_id": "test", "asset_ids": []})
        assert resp.status_code != 404, \
            "POST /v1/review/dedup 路由未注册（返回 404）"

    def test_governance_dashboard_http_route_registered(self):
        """GET /v1/governance/dashboard 路由应注册到 governance router。"""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from server.governance.metrics import governance_router

        app = FastAPI()
        app.include_router(governance_router)
        client = TestClient(app)

        # GET /v1/governance/dashboard 应返回非 404
        resp = client.get("/v1/governance/dashboard")
        assert resp.status_code != 404, \
            "GET /v1/governance/dashboard 路由未注册（返回 404）"

    def test_pr_review_dedup_service_class_exists(self):
        """PRReviewDedupService 服务类已实现。"""
        from server.governance.pr_review_dedup import PRReviewDedupService

        assert PRReviewDedupService is not None
        # 服务类含 review 方法
        all_methods = [m for m in dir(PRReviewDedupService) if not m.startswith("_")]
        assert any("review" in m.lower() or "dedup" in m.lower() for m in all_methods), \
            f"PRReviewDedupService 缺 review/dedup 方法，实际：{all_methods}"

    def test_dashboard_service_class_exists(self):
        """DashboardService 服务类已实现。"""
        from server.governance.dashboard import DashboardService

        assert DashboardService is not None
        # 服务类含 get_dashboard 方法
        assert hasattr(DashboardService, "get_dashboard"), \
            "DashboardService 缺 get_dashboard 方法"
        assert hasattr(DashboardService, "get_overview"), \
            "DashboardService 缺 get_overview 方法"

    def test_governance_metrics_routes_registered(self, database):
        """governance router 已注册的 /v1/metrics* 路由（对照验证）。"""
        from fastapi import FastAPI
        from starlette.testclient import TestClient

        from server.governance.metrics import GovernanceMetrics, build_router

        # 用 database fixture（SQLite 内存库）构造 GovernanceMetrics
        metrics = GovernanceMetrics(database)
        router = build_router(metrics)

        app = FastAPI()
        app.include_router(router)
        client = TestClient(app)

        # POST /v1/metrics 应注册（对照基准）
        resp = client.post("/v1/metrics", json={"events": [], "agent_id": ""})
        assert resp.status_code != 404, "POST /v1/metrics 路由未注册（对照基准失败）"

        # GET /v1/metrics/dashboard 应注册
        resp = client.get("/v1/metrics/dashboard")
        assert resp.status_code != 404, "GET /v1/metrics/dashboard 路由未注册（对照基准失败）"
