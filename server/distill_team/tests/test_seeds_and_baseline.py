"""SubTask 8.6 + 8.10 + 8.11：种子库 + 模型一致性测试集 + 反向验证基线测试。"""

from __future__ import annotations

from pathlib import Path

import pytest

from server.distill_team.consistency_test import ConsistencyTestSet
from server.distill_team.reverse_baseline import ReverseBaseline
from server.distill_team.seed_library import SeedLibrary, SeedPrompt


# ---------------------------------------------------------------------------
# 8.6 种子 Prompt 库
# ---------------------------------------------------------------------------


@pytest.fixture
def seeds_repo(tmp_path: Path) -> Path:
    """构造带 2 个种子文件的仓库。"""
    repo = tmp_path / "repo"
    seeds_dir = repo / "prompts" / "seeds"
    seeds_dir.mkdir(parents=True)
    (seeds_dir / "seed-1.md").write_text(
        "---\n"
        "id: seed-pr-review\n"
        "category: rule-backend\n"
        "scenario: PR Review 检查清单\n"
        "title: PR Review\n"
        "tags: [pr-review, lint]\n"
        "---\n"
        "# PR Review\n"
        "应当检查测试\n",
        encoding="utf-8",
    )
    (seeds_dir / "seed-2.md").write_text(
        "---\n"
        "id: seed-commit-msg\n"
        "category: rule-git\n"
        "scenario: Git commit 规范\n"
        "title: Commit Message\n"
        "tags: [git, commit]\n"
        "---\n"
        "# Commit Message\n"
        "subject 不超 50 字\n",
        encoding="utf-8",
    )
    return repo


class TestSeedLibrary:
    """种子 Prompt 库测试。"""

    def test_list_seeds_empty_dir(self, tmp_path):
        lib = SeedLibrary(tmp_path)
        assert lib.list_seeds() == []

    def test_list_seeds(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        seeds = lib.list_seeds()
        assert len(seeds) == 2
        ids = {s.seed_id for s in seeds}
        assert ids == {"seed-pr-review", "seed-commit-msg"}

    def test_get_seed_by_id(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        seed = lib.get_seed("seed-pr-review")
        assert seed is not None
        assert seed.category == "rule-backend"
        assert "应当检查测试" in seed.content

    def test_get_seed_not_found(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        assert lib.get_seed("nonexistent") is None

    def test_match_seed_by_category(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        matched = lib.match_seed(category="rule-backend")
        assert len(matched) == 1
        assert matched[0].seed_id == "seed-pr-review"

    def test_match_seed_by_tags(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        matched = lib.match_seed(tags=["git"])
        assert len(matched) == 1
        assert matched[0].seed_id == "seed-commit-msg"

    def test_match_seed_by_scenario_keyword(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        matched = lib.match_seed(scenario_keyword="PR Review")
        assert len(matched) == 1
        assert matched[0].seed_id == "seed-pr-review"

    def test_seed_strips_frontmatter(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        seed = lib.get_seed("seed-pr-review")
        assert seed is not None
        assert "---" not in seed.content
        assert seed.content.startswith("# PR Review")


# ---------------------------------------------------------------------------
# 8.10 模型一致性测试集
# ---------------------------------------------------------------------------


class TestConsistencyTestSet:
    """模型一致性测试集测试。"""

    def test_count_is_20(self):
        ts = ConsistencyTestSet()
        assert ts.count() == 20

    def test_list_fixtures_returns_all(self):
        ts = ConsistencyTestSet()
        fixtures = ts.list_fixtures()
        assert len(fixtures) == 20
        # 检查 fixture_id 唯一
        ids = [f.fixture_id for f in fixtures]
        assert len(set(ids)) == 20

    def test_get_fixture_by_id(self):
        ts = ConsistencyTestSet()
        f = ts.get_fixture("TC-001")
        assert f is not None
        assert f.expected_decision == "PROMOTE"
        assert len(f.assets) == 3

    def test_get_fixture_not_found(self):
        ts = ConsistencyTestSet()
        assert ts.get_fixture("TC-999") is None

    def test_by_decision_promote(self):
        ts = ConsistencyTestSet()
        promote = ts.by_decision("PROMOTE")
        # 至少 8 个 PROMOTE（TC-001~008 + 013/014/017/019）
        assert len(promote) >= 8

    def test_by_decision_skip(self):
        ts = ConsistencyTestSet()
        skip = ts.by_decision("SKIP")
        # 至少 6 个 SKIP（TC-009~012 + 015/016/018/020）
        assert len(skip) >= 6

    def test_by_cold_start(self):
        ts = ConsistencyTestSet()
        cold = ts.by_cold_start()
        # TC-017~020 共 4 个冷启动场景
        assert len(cold) == 4
        for f in cold:
            assert f.expected_cold_start is True
            assert f.expected_confidence == "low"

    def test_convention_fixtures(self):
        ts = ConsistencyTestSet()
        conv = [f for f in ts.list_fixtures() if f.is_convention]
        # TC-013~016 + TC-019 共 5 个 convention 场景（TC-019 同时是冷启动）
        assert len(conv) == 5

    def test_each_fixture_has_assets(self):
        ts = ConsistencyTestSet()
        for f in ts.list_fixtures():
            assert len(f.assets) >= 1
            for a in f.assets:
                assert "id" in a
                assert "owner" in a
                assert "content" in a


# ---------------------------------------------------------------------------
# 8.11 反向验证基线
# ---------------------------------------------------------------------------


class TestReverseBaseline:
    """反向验证基线测试。"""

    def test_match_returns_all_seeds(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        matches = baseline.match(prompt_content="# PR Review\n应当检查")
        # 返回所有种子（按相似度排序）
        assert len(matches) == 2

    def test_match_higher_similarity_for_relevant_seed(self, seeds_repo):
        """与 PR Review 相关内容 → seed-pr-review 相似度更高。"""
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        matches = baseline.match(prompt_content="# PR Review\n应当检查测试\n禁止硬编码")
        # 排序后第一个应是最相似的
        top = matches[0]
        assert top.seed.seed_id == "seed-pr-review"
        assert top.similarity > 0.0

    def test_is_calibrated_true_when_match(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib, similarity_threshold=0.05)
        # PR Review 内容与种子匹配
        result = baseline.is_calibrated(
            prompt_content="# PR Review\n应当检查",
            threshold=0.05,
        )
        assert result is True

    def test_is_calibrated_false_when_no_match(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib, similarity_threshold=0.9)
        # 不相关内容 → 不匹配
        result = baseline.is_calibrated(
            prompt_content="完全无关的内容 xyz",
            threshold=0.9,
        )
        assert result is False

    def test_empty_prompt_returns_zero_similarity(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        matches = baseline.match(prompt_content="")
        for m in matches:
            assert m.similarity == 0.0
            assert m.passed is False

    def test_filter_by_category(self, seeds_repo):
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        # 仅匹配 rule-backend 类别
        matches = baseline.match(
            prompt_content="# PR Review",
            prompt_category="rule-backend",
        )
        # 仅 1 个种子符合 category
        assert len(matches) == 1
        assert matches[0].seed.category == "rule-backend"

    def test_filter_by_category_fallback_to_all(self, seeds_repo):
        """未知 category → 回退到全部种子。"""
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        matches = baseline.match(
            prompt_content="# test",
            prompt_category="nonexistent",
        )
        assert len(matches) == 2  # 全部种子

    def test_chinese_text_similarity(self, seeds_repo):
        """中文内容相似度计算。"""
        lib = SeedLibrary(seeds_repo)
        baseline = ReverseBaseline(lib)
        # 内容含"PR Review" + "应当检查测试"
        matches = baseline.match(prompt_content="PR Review 应当检查测试覆盖")
        top = matches[0]
        assert top.similarity > 0.0
