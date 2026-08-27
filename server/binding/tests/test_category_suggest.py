"""SubTask 5.3 + 5.4 + 5.5 测试：category 推断 + 校验 + post-hoc。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from server.binding.category_suggest import (
    CategorySuggestService,
    CategoryValidationResult,
    PostHocReport,
)
from server.binding.llm import (
    CategoryCandidate,
    LLMChatProtocol,
    SuggestResult,
    call_llm_for_category_suggestions,
)


# ---------------------------------------------------------------------------
# Mock LLM
# ---------------------------------------------------------------------------


class MockLLMGood:
    """返回 3 个合法候选的 mock LLM。"""

    def chat(
        self, messages: list[dict[str, str]], *, schema: dict | None = None
    ) -> dict[str, Any]:
        return {
            "content": """{
  "candidates": [
    {"category": "rule-backend", "confidence": 0.92, "rationale": "内容含 lint 规则"},
    {"category": "rule-api", "confidence": 0.75, "rationale": "提到 API 规范"},
    {"category": "memory-backend", "confidence": 0.6, "rationale": "含经验教训"}
  ]
}""",
            "usage": {"total_tokens": 100},
        }


class MockLLMBadJSON:
    """返回非合法 JSON 的 mock LLM。"""

    def chat(
        self, messages: list[dict[str, str]], *, schema: dict | None = None
    ) -> dict[str, Any]:
        return {"content": "这不是 JSON", "usage": {}}


class MockLLMEmpty:
    """返回空候选列表的 mock LLM。"""

    def chat(
        self, messages: list[dict[str, str]], *, schema: dict | None = None
    ) -> dict[str, Any]:
        return {"content": """{"candidates": []}""", "usage": {}}


# ---------------------------------------------------------------------------
# SubTask 5.3: category 自动推断
# ---------------------------------------------------------------------------


class TestCategorySuggest:
    """LLM 推荐 3 候选 + 一键采纳。"""

    def test_suggest_with_llm_returns_3_candidates(self, database, sample_repo):
        """LLM 注入 → 返回 3 个候选，used_fallback=False。"""
        svc = CategorySuggestService(
            database, repo_root=sample_repo, llm=MockLLMGood()
        )
        result = svc.suggest(content="lint 规则：禁止使用 print", module_path="modules/backend")
        assert len(result.candidates) == 3
        assert result.candidates[0].category == "rule-backend"
        assert result.candidates[0].confidence == 0.92
        assert result.used_fallback is False
        assert result.error == ""

    def test_suggest_without_llm_falls_back(self, database, sample_repo):
        """未注入 LLM → 启发式 fallback，used_fallback=True。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        result = svc.suggest(content="这是 lint 规则", module_path="modules/backend")
        assert result.used_fallback is True
        assert len(result.candidates) == 3
        # 启发式首候选 module 应为 backend（取 module_path 末段）
        assert result.candidates[0].category.endswith("-backend")

    def test_suggest_llm_bad_json_falls_back(self, database, sample_repo):
        """LLM 返回非 JSON → 退化为启发式 + 记录 error。"""
        svc = CategorySuggestService(
            database, repo_root=sample_repo, llm=MockLLMBadJSON()
        )
        result = svc.suggest(content="lint 规则", module_path="modules/backend")
        assert result.used_fallback is True
        assert "llm 调用失败" in result.error
        assert len(result.candidates) == 3

    def test_suggest_llm_empty_candidates_falls_back(self, database, sample_repo):
        """LLM 返回空候选 → 退化为启发式。"""
        svc = CategorySuggestService(
            database, repo_root=sample_repo, llm=MockLLMEmpty()
        )
        result = svc.suggest(content="lint 规则", module_path="modules/backend")
        assert result.used_fallback is True
        assert len(result.candidates) == 3

    def test_suggest_module_path_empty_uses_general(self, database, sample_repo):
        """module_path 为空 → 启发式用 general。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        result = svc.suggest(content="lint 规则", module_path="")
        assert result.candidates[0].category.endswith("-general")

    def test_adopt_candidate_writes_yaml(self, database, sample_repo):
        """一键采纳：写入 categories.yaml。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        candidate = CategoryCandidate(
            category="rule-api",
            confidence=0.8,
            rationale="API 规则",
        )
        registry = svc.adopt_candidate(candidate, description="API 设计规范")
        assert "rule-api" in registry.categories
        # 验证 yaml 文件已更新
        yaml_text = (sample_repo / ".teamharness" / "categories.yaml").read_text(
            encoding="utf-8"
        )
        assert "rule-api" in yaml_text
        assert "API 设计规范" in yaml_text

    def test_adopt_candidate_invalid_category_raises(self, database, sample_repo):
        """采纳非法 category → ValueError。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        candidate = CategoryCandidate(
            category="invalid-category-with-uppercase",
            confidence=0.5,
            rationale="",
        )
        with pytest.raises(ValueError, match="非法 category"):
            svc.adopt_candidate(candidate)

    def test_adopt_candidate_no_repo_raises(self, database):
        """无 repo_root → RuntimeError。"""
        svc = CategorySuggestService(database, repo_root=None, llm=None)
        candidate = CategoryCandidate(
            category="rule-backend", confidence=0.9, rationale=""
        )
        with pytest.raises(RuntimeError, match="未配置 repo_root"):
            svc.adopt_candidate(candidate)


# ---------------------------------------------------------------------------
# SubTask 5.4: category 校验
# ---------------------------------------------------------------------------


class TestCategoryValidate:
    """两级 <type>-<module> + INDEX.md module 登记校验。"""

    def test_validate_legal_registered_indexed(self, database, sample_repo):
        """全合法：格式 + categories.yaml 登记 + INDEX.md module 登记。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        v = svc.validate("rule-backend")
        assert v.format_valid is True
        assert v.registered_in_yaml is True
        assert v.module_indexed is True
        assert v.ok is True
        assert v.violations == []

    def test_validate_invalid_format(self, database, sample_repo):
        """格式非法（大写）。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        v = svc.validate("Rule-Backend")
        assert v.format_valid is False
        assert v.ok is False
        assert any("命名规范" in s for s in v.violations)

    def test_validate_unknown_type(self, database, sample_repo):
        """type 非法。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        v = svc.validate("unknown-backend")
        assert v.format_valid is False
        assert v.ok is False

    def test_validate_not_registered_in_yaml(self, database, sample_repo):
        """格式合法但未在 categories.yaml 登记。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        v = svc.validate("rule-frontend")  # frontend 未在 yaml 登记
        assert v.format_valid is True
        assert v.registered_in_yaml is False
        assert v.ok is False
        assert any("未在 categories.yaml 登记" in s for s in v.violations)

    def test_validate_module_not_in_index(self, database, sample_repo):
        """category 在 yaml 登记但 module 未在 INDEX.md 登记。

        构造：把 rule-notindexed 加入 yaml，但 INDEX.md 无 module=notindexed。
        """
        # 先把 rule-notindexed 加入 yaml
        yaml_path = sample_repo / ".teamharness" / "categories.yaml"
        yaml_path.write_text(
            """categories:
  - name: rule-notindexed
    description: 测试
    modules: [notindexed]
""",
            encoding="utf-8",
        )
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        v = svc.validate("rule-notindexed")
        assert v.format_valid is True
        assert v.registered_in_yaml is True
        assert v.module_indexed is False
        assert v.ok is False
        assert any("<module>" in s for s in v.violations)

    def test_validate_pr_blocking(self, database, sample_repo):
        """PR Review 批量校验：违规非空 → 阻断合入。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        changed = [
            ("rules/a.md", "rule-backend"),  # 合法
            ("rules/b.md", "Rule-Bad"),  # 格式非法
            ("rules/c.md", "rule-notregistered"),  # 未登记
        ]
        violations = svc.validate_pr(changed)
        assert len(violations) == 2
        paths = {v.asset_path for v in violations}
        assert "rules/b.md" in paths
        assert "rules/c.md" in paths


# ---------------------------------------------------------------------------
# SubTask 5.5: 快速模式 post-hoc 校验
# ---------------------------------------------------------------------------


class TestPostHocCheck:
    """push main 后 post-hoc 校验：未登记创建 pending + 告警。"""

    def test_posthoc_creates_pending_for_unregistered_module(
        self, database, sample_repo
    ):
        """未在 INDEX.md 登记 → 创建 pending 行。"""
        alerts: list[tuple[str, dict]] = []

        def alert_sink(topic: str, payload: dict) -> None:
            alerts.append((topic, payload))

        svc = CategorySuggestService(
            database, repo_root=sample_repo, llm=None, alert_sink=alert_sink
        )
        # rule-unknown 未在 INDEX.md 登记 module=unknown
        changed = [("rules/x.md", "rule-unknown")]
        report = svc.posthoc_check(changed, commit_sha="abc123")
        assert report.checked_assets == 1
        assert report.pending_created == 1
        assert report.alerts_sent == 1
        assert len(report.pending_ids) == 1
        # 告警已触发
        assert alerts[0][0] == "pending_category_created"
        assert alerts[0][1]["category"] == "rule-unknown"
        # DB 中存在 pending 行
        pendings = svc.list_pending()
        assert len(pendings) == 1
        assert pendings[0].status == "pending"
        assert pendings[0].alert_sent is True

    def test_posthoc_no_pending_for_legal_category(self, database, sample_repo):
        """合法 category 不创建 pending。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        changed = [("rules/x.md", "rule-backend")]  # 合法
        report = svc.posthoc_check(changed, commit_sha="abc")
        assert report.pending_created == 0
        assert svc.list_pending() == []

    def test_posthoc_idempotent_same_commit(self, database, sample_repo):
        """同一 (asset_path, commit_sha, category) 重复 post-hoc → 跳过。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        changed = [("rules/x.md", "rule-unknown")]
        r1 = svc.posthoc_check(changed, commit_sha="abc")
        r2 = svc.posthoc_check(changed, commit_sha="abc")
        assert r1.pending_created == 1
        assert r2.pending_created == 0  # 第二次跳过
        # DB 中仍只有 1 行
        assert len(svc.list_pending()) == 1

    def test_posthoc_alert_sink_failure_does_not_block(self, database, sample_repo):
        """alert_sink 抛异常不阻塞 post-hoc 流程。"""
        def bad_sink(topic: str, payload: dict) -> None:
            raise RuntimeError("sink 故障")

        svc = CategorySuggestService(
            database, repo_root=sample_repo, llm=None, alert_sink=bad_sink
        )
        changed = [("rules/x.md", "rule-unknown")]
        report = svc.posthoc_check(changed, commit_sha="abc")
        # pending 仍创建，但 alerts_sent=0
        assert report.pending_created == 1
        assert report.alerts_sent == 0
        assert len(report.pending_ids) == 1

    def test_posthoc_mixed_assets(self, database, sample_repo):
        """混合：1 合法 + 2 未登记 → 创建 2 pending。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        changed = [
            ("rules/ok.md", "rule-backend"),  # 合法
            ("rules/bad1.md", "rule-unknown1"),  # 未登记 module
            ("rules/bad2.md", "rule-unknown2"),  # 未登记 module
        ]
        report = svc.posthoc_check(changed, commit_sha="abc")
        assert report.checked_assets == 3
        assert report.pending_created == 2

    def test_resolve_pending(self, database, sample_repo):
        """人工补登记后 resolve pending。"""
        svc = CategorySuggestService(database, repo_root=sample_repo, llm=None)
        changed = [("rules/x.md", "rule-unknown")]
        report = svc.posthoc_check(changed, commit_sha="abc")
        pid = report.pending_ids[0]
        assert svc.resolve_pending(pid) is True
        # 状态已变 resolved
        pending = svc.list_pending(status="pending")
        assert len(pending) == 0
        resolved = svc.list_pending(status="resolved")
        assert len(resolved) == 1
        assert resolved[0].resolved_at is not None
        # 二次 resolve 返回 False
        assert svc.resolve_pending(pid) is False
