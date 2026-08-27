"""webhook pr_merged 事件归一化测试。

固化自主对话内联脚本，确保可重复执行。

覆盖：
- Gitea pull_request closed+merged → pr_merged
- GitLab merge_request action=merge → pr_merged
- PR closed but NOT merged → 不触发 pr_merged
- 普通 push 事件不受影响
- Gitea/GitLab provider 识别
- merge_commit_sha 提取
- ref 提取（Gitea base.ref / GitLab target_branch）
"""

from __future__ import annotations

from server.infra_git.webhook import normalize_event
from server.common.models import WebhookEvent


# ---------------------------------------------------------------------------
# Gitea pull_request 事件
# ---------------------------------------------------------------------------

GITEA_HEADERS = {"x-gitea-event": "pull_request", "x-gitea-signature": "x"}
GITLAB_HEADERS = {"x-gitlab-event": "merge_request", "x-gitlab-token": "x"}


def test_gitea_pr_merged():
    """Gitea PR closed + merged=true → pr_merged，用 merge_commit_sha 作 after。"""
    payload = {
        "action": "closed",
        "pull_request": {
            "merged": True,
            "merge_commit_sha": "abc123merge",
            "base": {"ref": "main"},
        },
        "repository": {"full_name": "teamharness/teamharness-shared"},
    }
    ev = normalize_event(payload, GITEA_HEADERS)
    assert ev.event_type == "pr_merged"
    assert ev.after == "abc123merge"
    assert ev.ref == "main"
    assert ev.repo == "teamharness/teamharness-shared"
    assert ev.provider == "gitea"


def test_gitlab_mr_merged():
    """GitLab MR action=merge → pr_merged，用 merge_commit_sha 作 after。"""
    payload = {
        "object_kind": "merge_request",
        "event_type": "merge_request",
        "action": "merge",
        "object_attributes": {
            "merge_commit_sha": "def456merge",
            "target_branch": "main",
        },
        "project": {"path_with_namespace": "teamharness/teamharness-shared"},
    }
    ev = normalize_event(payload, GITLAB_HEADERS)
    assert ev.event_type == "pr_merged"
    assert ev.after == "def456merge"
    assert ev.ref == "main"
    assert ev.repo == "teamharness/teamharness-shared"
    assert ev.provider == "gitlab"


def test_pr_closed_not_merged():
    """PR closed 但未 merged → 不触发 pr_merged。"""
    payload = {
        "action": "closed",
        "pull_request": {"merged": False},
        "repository": {"full_name": "x/y"},
    }
    ev = normalize_event(payload, GITEA_HEADERS)
    assert ev.event_type != "pr_merged"


def test_push_event_unchanged():
    """普通 push 事件不受 PR 逻辑影响。"""
    payload = {
        "after": "push123",
        "ref": "refs/heads/main",
        "repository": {"full_name": "x/y"},
    }
    push_headers = {"x-gitea-event": "push"}
    ev = normalize_event(payload, push_headers)
    assert ev.event_type == "push"
    assert ev.after == "push123"


def test_pr_merged_missing_merge_sha():
    """PR merged 但 merge_commit_sha 为空 → after 为空字符串（不崩溃）。"""
    payload = {
        "action": "closed",
        "pull_request": {"merged": True, "base": {"ref": "develop"}},
        "repository": {"full_name": "x/y"},
    }
    ev = normalize_event(payload, GITEA_HEADERS)
    assert ev.event_type == "pr_merged"
    assert ev.after == ""
    assert ev.ref == "develop"


def test_pr_merged_repo_fallback():
    """PR merged 时 repository.full_name 缺失 → 回退到 project.path_with_namespace。"""
    payload = {
        "action": "merge",
        "object_attributes": {"merge_commit_sha": "sha789", "target_branch": "main"},
        "project": {"path_with_namespace": "fallback/repo"},
    }
    ev = normalize_event(payload, GITLAB_HEADERS)
    assert ev.event_type == "pr_merged"
    assert ev.repo == "fallback/repo"


def test_pr_open_event_not_merged():
    """PR opened/synchronized 事件不触发 pr_merged。"""
    for action in ("opened", "synchronized", "reopened", "labeled"):
        payload = {
            "action": action,
            "pull_request": {"merged": False, "merge_commit_sha": ""},
            "repository": {"full_name": "x/y"},
        }
        ev = normalize_event(payload, GITEA_HEADERS)
        assert ev.event_type != "pr_merged", f"action={action} 不应触发 pr_merged"


def test_webhook_event_dataclass_fields():
    """验证 WebhookEvent 返回的字段完整性。"""
    payload = {
        "action": "closed",
        "pull_request": {"merged": True, "merge_commit_sha": "sha1", "base": {"ref": "main"}},
        "repository": {"full_name": "x/y"},
    }
    ev = normalize_event(payload, GITEA_HEADERS)
    assert isinstance(ev, WebhookEvent)
    assert ev.before == ""  # PR merged 无 before
    assert ev.raw == payload  # 原始 payload 保留
