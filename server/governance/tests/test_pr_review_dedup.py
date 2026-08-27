"""SubTask 9.1 + 9.13: PR Review 语义去重测试。

覆盖：
- content_hash 精确匹配 → merge 建议
- 语义相似度 ≥0.92 → LLM 判断归并/独立
- LLM 不可用 → needs_review 降级
- 无候选 → keep_separate
- 空内容 → skip
- LLM 响应解析容错（markdown 代码块包裹）
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from server.governance.pr_review_dedup import (
    PRReviewDedupService,
    SIMILARITY_THRESHOLD,
)
from server.infra_db.vectorstore import VectorRecord


# ---------------------------------------------------------------------------
# Mock LLM（实现 LLMChatLike 协议）
# ---------------------------------------------------------------------------


class MockLLM:
    """可控的 LLM mock，返回预设 decision/rationale。"""

    def __init__(
        self,
        *,
        decision: str = "merge",
        rationale: str = "语义一致，建议归并",
        fail: bool = False,
        raw_content: str | None = None,
    ) -> None:
        self.decision = decision
        self.rationale = rationale
        self.fail = fail
        self.raw_content = raw_content
        self.call_count = 0

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict[str, Any]:
        self.call_count += 1
        if self.fail:
            raise RuntimeError("LLM 调用模拟失败")
        if self.raw_content is not None:
            content = self.raw_content
        else:
            content = json.dumps(
                {"decision": self.decision, "rationale": self.rationale},
                ensure_ascii=False,
            )
        return {"content": content, "usage": {}, "model": "mock-llm"}


# ---------------------------------------------------------------------------
# 辅助：向 vector_store 注入向量
# ---------------------------------------------------------------------------


def _add_vector(vector_store, embedding_service, *, asset_id: str, content: str):
    """计算 embedding 并写入 vector_store。"""
    emb = embedding_service.embed(content)
    active_version = embedding_service.get_active_version()
    vector_store.ensure_collection(active_version, emb.dim)
    vector_store.upsert(
        VectorRecord(asset_id=asset_id, vector=emb.vector, dim=emb.dim),
        model_version=active_version,
    )


# ---------------------------------------------------------------------------
# content_hash 精确匹配
# ---------------------------------------------------------------------------


class TestContentHashExactMatch:
    """内容级去重：content_hash 精确匹配。"""

    def test_content_hash_match_suggests_merge(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """相同 content_hash → merge 建议。"""
        # 已有资产
        upsert_helper(
            asset_index,
            id="existing-rule",
            content="# lint rule\n禁止使用 print",
            content_hash="hash-abc-123",
        )
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        result = svc.review_pr(
            pr_id="pr-1",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": "# lint rule\n禁止使用 print",
                    "content_hash": "hash-abc-123",
                    "git_path": "rules/new-rule.md",
                }
            ],
        )
        assert result.pr_id == "pr-1"
        assert len(result.suggestions) == 1
        s = result.suggestions[0]
        assert s.suggestion == "merge"
        assert len(s.duplicates) == 1
        assert s.duplicates[0].asset_id == "existing-rule"
        assert s.duplicates[0].similarity == 1.0
        assert s.duplicates[0].llm_decision == "merge"

    def test_content_hash_no_match_falls_through(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """content_hash 不匹配 → 不触发精确匹配。"""
        upsert_helper(
            asset_index,
            id="existing-rule",
            content="# lint rule",
            content_hash="hash-existing",
        )
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        result = svc.review_pr(
            pr_id="pr-1",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": "完全不同的内容",
                    "content_hash": "hash-new",
                    "git_path": "rules/new.md",
                }
            ],
        )
        # 无 content_hash 匹配，且向量库无候选 → keep_separate
        assert result.suggestions[0].suggestion == "keep_separate"


# ---------------------------------------------------------------------------
# 语义相似度 ≥0.92 → LLM 判断
# ---------------------------------------------------------------------------


class TestSemanticDedup:
    """语义级去重：embedding + 相似度 + LLM 判断。"""

    def test_semantic_match_triggers_llm_judge(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """语义相似度 ≥0.92 → 触发 LLM 判断（相同内容 → 相似度=1.0）。"""
        existing_content = "# lint 规则\n禁止使用 print 语句调试"
        upsert_helper(
            asset_index,
            id="existing-rule",
            content=existing_content,
        )
        # 注入向量（让 vector_store 能检索到）
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content=existing_content,
        )

        llm = MockLLM(decision="merge", rationale="同一规则的不同表述")
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=llm,
        )
        result = svc.review_pr(
            pr_id="pr-2",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": existing_content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        assert len(s.duplicates) == 1
        assert s.duplicates[0].asset_id == "existing-rule"
        assert s.duplicates[0].similarity >= SIMILARITY_THRESHOLD
        assert s.duplicates[0].llm_decision == "merge"
        assert llm.call_count == 1
        assert s.suggestion == "merge"

    def test_llm_returns_independent(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """LLM 判断为 independent → keep_separate。"""
        existing_content = "# 错误处理规则\n所有 API 必须捕获异常"
        upsert_helper(
            asset_index,
            id="existing-rule",
            content=existing_content,
        )
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content=existing_content,
        )

        llm = MockLLM(decision="independent", rationale="相似但独立，互补场景")
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=llm,
        )
        result = svc.review_pr(
            pr_id="pr-3",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": existing_content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        assert s.duplicates[0].llm_decision == "independent"
        assert s.suggestion == "keep_separate"

    def test_llm_unavailable_degrades_to_needs_review(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """LLM=None → 候选存在时降级为 needs_review。"""
        existing_content = "# 规则\n禁止使用 print"
        upsert_helper(
            asset_index,
            id="existing-rule",
            content=existing_content,
        )
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content=existing_content,
        )

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,  # LLM 未注入
        )
        result = svc.review_pr(
            pr_id="pr-4",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": existing_content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        # LLM 未注入 → decision=needs_review
        assert s.duplicates[0].llm_decision == "needs_review"
        assert s.suggestion == "needs_review"
        assert "LLM Provider 未注入" in s.llm_error

    def test_llm_failure_degrades_to_needs_review(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """LLM 调用异常 → needs_review（不阻断 PR）。"""
        existing_content = "# 规则\n禁止使用 print"
        upsert_helper(
            asset_index,
            id="existing-rule",
            content=existing_content,
        )
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content=existing_content,
        )

        llm = MockLLM(fail=True)
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=llm,
        )
        result = svc.review_pr(
            pr_id="pr-5",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": existing_content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        assert s.duplicates[0].llm_decision == "needs_review"
        assert s.suggestion == "needs_review"
        assert "LLM 调用失败" in s.llm_error

    def test_llm_markdown_codeblock_response_parsed(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """LLM 返回 ```json 代码块包裹 → 能正确解析。"""
        existing_content = "# 规则\n禁止使用 print"
        upsert_helper(
            asset_index,
            id="existing-rule",
            content=existing_content,
        )
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content=existing_content,
        )

        llm = MockLLM(
            raw_content='```json\n{"decision": "merge", "rationale": "代码块包裹"}\n```'
        )
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=llm,
        )
        result = svc.review_pr(
            pr_id="pr-6",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": existing_content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        assert s.duplicates[0].llm_decision == "merge"
        assert s.suggestion == "merge"


# ---------------------------------------------------------------------------
# 边界与异常
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """边界与异常用例。"""

    def test_empty_content_skipped(self, database, asset_index, embedding_service, vector_store):
        """空内容 → skip。"""
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        result = svc.review_pr(
            pr_id="pr-empty",
            assets=[
                {
                    "id": "empty-rule",
                    "type": "rule",
                    "content": "",
                    "content_hash": "",
                    "git_path": "rules/empty.md",
                }
            ],
        )
        s = result.suggestions[0]
        assert s.suggestion == "skip"
        assert "内容为空" in s.llm_error

    def test_no_candidates_keep_separate(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """向量库无候选 → keep_separate。"""
        upsert_helper(
            asset_index,
            id="existing-rule",
            content="完全不同的内容 abc",
        )
        _add_vector(
            vector_store,
            embedding_service,
            asset_id="existing-rule",
            content="完全不同的内容 abc",
        )

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        result = svc.review_pr(
            pr_id="pr-no-candidate",
            assets=[
                {
                    "id": "new-rule",
                    "type": "rule",
                    "content": "完全不同的内容 xyz",
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        # 向量不相似 → 无候选 → keep_separate
        s = result.suggestions[0]
        assert s.suggestion == "keep_separate"
        assert len(s.duplicates) == 0

    def test_exclude_self_in_semantic_search(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """新资产 id 与已有资产相同时 → 排除自身。"""
        content = "# 规则\n禁止使用 print"
        upsert_helper(asset_index, id="rule-1", content=content)
        _add_vector(
            vector_store, embedding_service, asset_id="rule-1", content=content
        )

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        result = svc.review_pr(
            pr_id="pr-self",
            assets=[
                {
                    "id": "rule-1",  # 与已有资产同 id
                    "type": "rule",
                    "content": content,
                    "content_hash": "",
                    "git_path": "rules/rule-1.md",
                }
            ],
        )
        # 排除自身 → 无候选 → keep_separate
        s = result.suggestions[0]
        assert s.suggestion == "keep_separate"

    def test_cross_type_not_matched(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """不同 type 的资产不参与语义去重。"""
        content = "# 规则\n禁止使用 print"
        upsert_helper(asset_index, id="rule-1", content=content, type="rule")
        _add_vector(
            vector_store, embedding_service, asset_id="rule-1", content=content
        )

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        # 新资产 type=memory，已有资产 type=rule → 不应匹配
        result = svc.review_pr(
            pr_id="pr-cross-type",
            assets=[
                {
                    "id": "new-memory",
                    "type": "memory",
                    "content": content,
                    "content_hash": "",
                    "git_path": "memory/new.md",
                }
            ],
        )
        s = result.suggestions[0]
        # 向量库有候选但 type 不匹配 → 过滤后无候选
        assert s.suggestion == "keep_separate"

    def test_result_llm_stats_tracked(
        self, database, asset_index, embedding_service, vector_store, upsert_helper
    ):
        """result.llm_calls / llm_errors 正确统计。"""
        content = "# 规则\n禁止使用 print"
        upsert_helper(asset_index, id="existing", content=content)
        _add_vector(
            vector_store, embedding_service, asset_id="existing", content=content
        )

        llm = MockLLM(fail=True)
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=llm,
        )
        result = svc.review_pr(
            pr_id="pr-stats",
            assets=[
                {
                    "id": "new",
                    "type": "rule",
                    "content": content,
                    "content_hash": "",
                    "git_path": "rules/new.md",
                }
            ],
        )
        # LLM 调用了 1 次，失败 1 次
        assert result.llm_calls == 1
        assert result.llm_errors == 1


# ---------------------------------------------------------------------------
# HTTP 端点 POST /v1/review/dedup（SubTask 9.1 + Agent 10 契约回归）
# ---------------------------------------------------------------------------


class TestPRReviewDedupEndpoint:
    """POST /v1/review/dedup HTTP 端点。

    覆盖：
    - happy path：注入服务后返回 200 + 去重结果
    - 服务未注入 → 503
    - 边界：仅传 pr_id（assets 缺省为空）
    - 边界：content_hash 精确匹配通过端点闭环
    全局服务变量用 monkeypatch 注入，测试结束自动清理（遵守
    gotchas.md「测试全局状态隔离」规则）。
    """

    def _build_app(self):
        from fastapi import FastAPI

        from server.governance.metrics import governance_router

        app = FastAPI()
        app.include_router(governance_router)
        return app

    def test_post_review_dedup_happy_path(
        self,
        database,
        asset_index,
        embedding_service,
        vector_store,
        upsert_helper,
        monkeypatch,
    ):
        """注入服务后 POST /v1/review/dedup 返回 200 + 去重结果。"""
        from fastapi.testclient import TestClient

        # 既有资产（content_hash 精确匹配路径）
        upsert_helper(
            asset_index,
            id="existing-rule",
            content="# lint rule\n禁止使用 print",
            content_hash="hash-abc-123",
        )
        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        # 注入全局服务实例（monkeypatch 自动清理）
        monkeypatch.setattr("server.governance.metrics._GOVERNANCE_DEDUP", svc)

        client = TestClient(self._build_app())
        resp = client.post(
            "/v1/review/dedup",
            json={
                "pr_id": "pr-ep-1",
                "assets": [
                    {
                        "id": "new-rule",
                        "type": "rule",
                        "content": "# lint rule\n禁止使用 print",
                        "content_hash": "hash-abc-123",
                        "git_path": "rules/new.md",
                    }
                ],
            },
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_id"] == "pr-ep-1"
        assert len(data["suggestions"]) == 1
        s = data["suggestions"][0]
        assert s["suggestion"] == "merge"
        assert len(s["duplicates"]) == 1
        assert s["duplicates"][0]["asset_id"] == "existing-rule"
        assert s["duplicates"][0]["similarity"] == 1.0

    def test_post_review_dedup_service_not_configured_returns_503(self, monkeypatch):
        """服务未注入时 POST /v1/review/dedup 返回 503（非 404/500）。"""
        from fastapi.testclient import TestClient

        monkeypatch.setattr("server.governance.metrics._GOVERNANCE_DEDUP", None)
        client = TestClient(self._build_app())

        resp = client.post(
            "/v1/review/dedup",
            json={"pr_id": "pr-1", "assets": []},
        )
        assert resp.status_code == 503

    def test_post_review_dedup_accepts_only_pr_id(
        self,
        database,
        asset_index,
        embedding_service,
        vector_store,
        monkeypatch,
    ):
        """仅传 pr_id（缺 assets）→ assets 默认空，返回 200 + 空建议。"""
        from fastapi.testclient import TestClient

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        monkeypatch.setattr("server.governance.metrics._GOVERNANCE_DEDUP", svc)

        client = TestClient(self._build_app())
        # 仅传 pr_id，不传 assets
        resp = client.post("/v1/review/dedup", json={"pr_id": "pr-only"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_id"] == "pr-only"
        assert data["suggestions"] == []
        assert data["total_duplicates"] == 0

    def test_post_review_dedup_ignores_extra_field(
        self,
        database,
        asset_index,
        embedding_service,
        vector_store,
        monkeypatch,
    ):
        """请求体含未声明字段（如 asset_ids）→ Pydantic 忽略，按 assets 缺省处理。

        兼容 Agent 10 回归用例的请求体 {"pr_id":..., "asset_ids":[]}。
        """
        from fastapi.testclient import TestClient

        svc = PRReviewDedupService(
            database=database,
            asset_index=asset_index,
            embedding_service=embedding_service,
            vector_store=vector_store,
            llm=None,
        )
        monkeypatch.setattr("server.governance.metrics._GOVERNANCE_DEDUP", svc)

        client = TestClient(self._build_app())
        resp = client.post(
            "/v1/review/dedup",
            json={"pr_id": "pr-extra", "asset_ids": []},
        )
        assert resp.status_code == 200
        data = resp.json()
        assert data["pr_id"] == "pr-extra"
        assert data["suggestions"] == []
