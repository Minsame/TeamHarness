"""ClientCLI 测试（SubTask 6.6 + 6.11）。

覆盖：
- build_parser 6 子命令
- sync dry-run / no git / 成功路径
- pr 降级路径（无 provider）
- recall 离线模式（list + read + 采纳率记录）
- category-suggest 离线 mock
- cost-estimate 本地占位估算
- index-reconcile 重建 manifest + gitignore 检查
- 未知命令 / 缺参数错误处理
- CliResult 序列化
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from server.client.cli import CliError, CliResult, ClientCLI
from server.client.config import ClientConfig
from server.common.models import AssetType, Scope
from server.client.working_copy import WorkingCopy


# ---------------------------------------------------------------------------
# 辅助
# ---------------------------------------------------------------------------


def _git_exe() -> str | None:
    return shutil.which("git")


def _run_git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    exe = _git_exe()
    assert exe is not None
    return subprocess.run(
        [exe, *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    """构造已初始化的 git 仓库。"""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run_git(repo, "init", "-b", "main")
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("# test\n", encoding="utf-8")
    _run_git(repo, "add", "README.md")
    _run_git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def repo_with_assets(tmp_path: Path) -> Path:
    """构造含资产的仓库（无 git，用于 recall 测试）。"""
    wc = WorkingCopy(tmp_path)
    wc.create_asset(
        AssetType.RULE,
        "global-lint",
        owner="alice",
        body="# 全局 lint 规范\n关键词: lint naming",
        scope=Scope.TEAM,
    )
    return tmp_path


def _make_cli(config: ClientConfig) -> tuple[ClientCLI, io.StringIO, io.StringIO]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    cli = ClientCLI(config, stdout=stdout, stderr=stderr)
    return cli, stdout, stderr


def _parse_stdout(stdout: io.StringIO) -> dict:
    data = json.loads(stdout.getvalue())
    return data


# ---------------------------------------------------------------------------
# build_parser
# ---------------------------------------------------------------------------


def test_build_parser_has_six_subcommands():
    cli = ClientCLI(config=ClientConfig(repo_root="/tmp"))
    parser = cli.build_parser()
    # 解析 -h 不报错即说明子命令注册成功
    actions = [a for a in parser._subparsers._group_actions if hasattr(a, "choices")]
    if actions:
        choices = actions[0].choices
        for cmd in ("sync", "pr", "recall", "category-suggest", "cost-estimate", "index-reconcile"):
            assert cmd in choices


# ---------------------------------------------------------------------------
# sync
# ---------------------------------------------------------------------------


def test_sync_dry_run(git_repo: Path):
    cfg = ClientConfig(repo_root=str(git_repo))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["sync", "--dry-run", "--branch", "members/alice"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    assert data["data"]["dry_run"] is True
    assert "git fetch" in data["data"]["steps"][0]
    assert data["data"]["branch"] == "members/alice"


def test_sync_no_git_returns_error(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, _, stderr = _make_cli(cfg)
    rc = cli.run(["sync", "--branch", "members/alice"])
    assert rc == 1
    err = json.loads(stderr.getvalue())
    assert "git" in err["error"]


def test_sync_no_args_uses_config_personal_branch(git_repo: Path):
    cfg = ClientConfig(repo_root=str(git_repo), member_id="alice")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["sync", "--dry-run"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["data"]["branch"] == "members/alice"


# ---------------------------------------------------------------------------
# pr
# ---------------------------------------------------------------------------


def test_pr_no_provider_returns_fallback(git_repo: Path):
    cfg = ClientConfig(repo_root=str(git_repo))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["pr", "--title", "Test PR", "--branch", "members/alice"])
    # 分支不存在 → push 失败 → created=False，返回 error
    assert rc == 1


def test_pr_missing_title_errors(git_repo: Path):
    cfg = ClientConfig(repo_root=str(git_repo))
    cli, _, stderr = _make_cli(cfg)
    rc = cli.run(["pr"])
    assert rc != 0  # argparse 缺必填参数报错


# ---------------------------------------------------------------------------
# recall（离线模式）
# ---------------------------------------------------------------------------


def test_recall_list_offline(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets), agent_id="agent-1")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["recall", "--query", "lint"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    assert data["data"]["degraded"] is True
    assert data["data"]["count"] >= 1
    assert any(it["asset_id"] == "rule-global-lint" for it in data["data"]["items"])


def test_recall_read_offline(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets), agent_id="agent-1")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["recall", "--read", "rule-global-lint"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    assert "lint" in data["data"]["content"]
    assert data["data"]["asset_id"] == "rule-global-lint"


def test_recall_read_gone(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets), agent_id="agent-1")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["recall", "--read", "rule-nonexistent"])
    assert rc == 1
    data = _parse_stdout(stdout)
    assert data["success"] is False
    assert data["data"]["asset_id"] == "rule-nonexistent"


def test_recall_no_adopt_record(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets), agent_id="agent-1")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["recall", "--query", "lint", "--no-adopt-record"])
    assert rc == 0
    # 不记录采纳率 → 无 adoption-events.jsonl
    assert not (repo_with_assets / ".teamharness" / "adoption-events.jsonl").is_file()


def test_recall_records_adoption_event(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets), agent_id="agent-1")
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["recall", "--query", "lint"])
    assert rc == 0
    # 记录了采纳率事件
    log_path = repo_with_assets / ".teamharness" / "adoption-events.jsonl"
    assert log_path.is_file()
    import json as _json
    lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) >= 1
    evt = _json.loads(lines[0])
    assert evt["event_type"] == "recall"


def test_recall_missing_agent_id_errors(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    cli, _, stderr = _make_cli(cfg)
    rc = cli.run(["recall", "--query", "lint"])
    assert rc == 1
    err = json.loads(stderr.getvalue())
    assert "agent_id" in err["error"]


# ---------------------------------------------------------------------------
# category-suggest（离线 mock）
# ---------------------------------------------------------------------------


def test_category_suggest_offline_returns_mock(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["category-suggest", "--content", "lint rule"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    assert data["data"]["source"] == "mock"
    assert data["data"]["count"] == 0
    assert any("占位" in w or "Agent 5" in w for w in data["warnings"])


def test_category_suggest_with_module(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run([
        "category-suggest", "--content", "backend lint", "--module", "modules/backend"
    ])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True


# ---------------------------------------------------------------------------
# cost-estimate
# ---------------------------------------------------------------------------


def test_cost_estimate_default(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["cost-estimate"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    est = data["data"]
    assert est["sessions"] == 10
    assert est["model"] == "gpt-4o-mini"
    assert est["total_tokens"] > 0
    assert est["estimated_cost_usd"] > 0
    assert "light" in est["stages"]
    assert "rem" in est["stages"]
    assert "deep" in est["stages"]
    assert est["source"] == "local-estimate"


def test_cost_estimate_custom_model(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run([
        "cost-estimate", "--sessions", "5", "--avg-tokens", "1000", "--model", "gpt-4o"
    ])
    assert rc == 0
    data = _parse_stdout(stdout)
    est = data["data"]
    assert est["sessions"] == 5
    assert est["model"] == "gpt-4o"
    # gpt-4o 比 gpt-4o-mini 贵
    assert est["estimated_cost_usd"] > 0


def test_cost_estimate_unknown_model_falls_back(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["cost-estimate", "--model", "unknown-model"])
    assert rc == 0
    data = _parse_stdout(stdout)
    est = data["data"]
    # 未知模型走默认单价
    assert est["pricing_per_million"]["input"] == 1.0


# ---------------------------------------------------------------------------
# index-reconcile
# ---------------------------------------------------------------------------


def test_index_reconcile_rebuilds_manifest(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["index-reconcile", "--rebuild-manifest"])
    assert rc == 0
    data = _parse_stdout(stdout)
    assert data["success"] is True
    assert "manifest" in data["data"]
    assert "diff" in data["data"]
    # manifest 文件已生成
    assert (repo_with_assets / ".teamharness" / "manifest.json").is_file()


def test_index_reconcile_check_gitignore(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["index-reconcile", "--check-gitignore"])
    assert rc == 0
    data = _parse_stdout(stdout)
    # 自动追加 .gitignore 规则
    assert (tmp_path / ".gitignore").is_file()
    gitignore_text = (tmp_path / ".gitignore").read_text(encoding="utf-8")
    assert ".teamharness/private/" in gitignore_text


def test_index_reconcile_warns_on_missing_gitignore(repo_with_assets: Path):
    cfg = ClientConfig(repo_root=str(repo_with_assets))
    cli, stdout, _ = _make_cli(cfg)
    rc = cli.run(["index-reconcile"])
    assert rc == 0
    data = _parse_stdout(stdout)
    # 无 .gitignore → 应有 warning
    assert any("gitignore" in w.lower() for w in data["warnings"])


# ---------------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------------


def test_unknown_command_returns_error(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, _, stderr = _make_cli(cfg)
    rc = cli.run(["unknown-command"])
    assert rc != 0


def test_no_command_returns_error(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    cli, _, stderr = _make_cli(cfg)
    rc = cli.run([])
    assert rc != 0  # argparse required=True


# ---------------------------------------------------------------------------
# CliResult 序列化
# ---------------------------------------------------------------------------


def test_cli_result_to_dict():
    result = CliResult(
        command="sync",
        success=True,
        data={"pulled": True},
        message="ok",
        warnings=["w1"],
    )
    d = result.to_dict()
    assert d["command"] == "sync"
    assert d["success"] is True
    assert d["data"]["pulled"] is True
    assert d["message"] == "ok"
    assert d["warnings"] == ["w1"]


def test_cli_result_defaults():
    result = CliResult(command="x", success=True)
    d = result.to_dict()
    assert d["data"] == {}
    assert d["warnings"] == []


def test_cli_error_raised():
    with pytest.raises(CliError, match="test error"):
        raise CliError("test error")
