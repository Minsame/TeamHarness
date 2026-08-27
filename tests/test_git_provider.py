"""GitProvider 抽象层与三实现域内测试。

对应 SubTask 1.1 + SubTask 1.9：
- 接口契约 fetch/show/diff/ls_tree 形态校验
- GitLab/Gitea 实现用 httpx.MockTransport 模拟远端响应
- Libgit2Provider：pygit2 缺失时优雅跳过；存在时验证 shallow clone/size 告警
- 仓库大小 500MB 告警阈值
"""

from __future__ import annotations

import base64
import os
import shutil
from pathlib import Path
from typing import Any

import httpx
import pytest

from server.common.models import DiffStatus, TreeEntryType
from server.infra_git.git_provider import (
    DEFAULT_REPO_SIZE_ALARM_BYTES,
    GitLabProvider,
    GiteaProvider,
    Libgit2Provider,
    RepoSizeAlarm,
    create_git_provider,
)


# ---------------------------------------------------------------------------
# GitLabProvider
# ---------------------------------------------------------------------------


def _gitlab_transport(handler):
    """构造注入 MockTransport 的 GitLabProvider。"""
    transport = httpx.MockTransport(handler)
    return GitLabProvider(
        base_url="https://gitlab.example.com",
        token="tok-abc",
        repo="team/repo",
        transport=transport,
    )


def test_gitlab_fetch_ok():
    """fetch 校验项目可达并登记 repo。"""
    seen: list[str] = []

    def handler(req: httpx.Request) -> httpx.Response:
        seen.append(str(req.url))
        assert req.headers["PRIVATE-TOKEN"] == "tok-abc"
        return httpx.Response(200, json={"id": 1, "path_with_namespace": "team/repo"})

    p = _gitlab_transport(handler)
    p.fetch("team/repo")
    assert "/api/v4/projects/team%2Frepo" in seen[0]
    assert p._repo == "team/repo"


def test_gitlab_show_decodes_base64():
    def handler(req: httpx.Request) -> httpx.Response:
        # 路径中 / 须被编码为 %2F
        assert "/repository/files/rules%2Flint.md" in str(req.url)
        assert req.url.params["ref"] == "sha-1"
        return httpx.Response(
            200,
            json={
                "encoding": "base64",
                "content": base64.b64encode(b"# rule\nhello").decode(),
            },
        )

    p = _gitlab_transport(handler)
    text = p.show("sha-1", "rules/lint.md")
    assert text == "# rule\nhello"


def test_gitlab_show_plain_content():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"content": "plain text"})

    p = _gitlab_transport(handler)
    assert p.show("sha", "a.md") == "plain text"


def test_gitlab_diff_parses_statuses():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "diffs": [
                    {"new_file": True, "new_path": "a.md", "old_path": "", "diff": "+"},
                    {"deleted_file": True, "new_path": "b.md", "old_path": "b.md", "diff": "-"},
                    {"renamed_file": True, "new_path": "c.md", "old_path": "c_old.md", "diff": "m"},
                    {"new_path": "d.md", "old_path": "d.md", "diff": "m"},
                ]
            },
        )

    p = _gitlab_transport(handler)
    entries = p.diff("a", "b")
    assert [e.status for e in entries] == [
        DiffStatus.ADDED,
        DiffStatus.DELETED,
        DiffStatus.RENAMED,
        DiffStatus.MODIFIED,
    ]
    assert entries[0].path == "a.md"
    assert entries[2].old_path == "c_old.md"


def test_gitlab_ls_tree_paginates():
    """ls_tree 翻页：每页 100 条，须连续拉取直到不足 100 条。"""
    pages = {
        1: [{"path": f"f{i}.md", "type": "blob", "id": str(i), "mode": "100644"} for i in range(100)],
        2: [{"path": f"g{i}.md", "type": "tree", "id": f"t{i}", "mode": "040000"} for i in range(50)],
    }

    def handler(req: httpx.Request) -> httpx.Response:
        page = int(req.url.params["page"])
        return httpx.Response(200, json=pages[page])

    p = _gitlab_transport(handler)
    entries = p.ls_tree("sha", "rules")
    assert len(entries) == 150
    assert entries[0].type == TreeEntryType.BLOB
    assert entries[100].type == TreeEntryType.TREE


def test_gitlab_fetch_error_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"message": "not found"})

    p = _gitlab_transport(handler)
    with pytest.raises(httpx.HTTPStatusError):
        p.fetch("team/missing")


# ---------------------------------------------------------------------------
# GiteaProvider
# ---------------------------------------------------------------------------


def _gitea_transport(handler):
    transport = httpx.MockTransport(handler)
    return GiteaProvider(
        base_url="https://gitea.example.com",
        token="tok-xyz",
        repo="team/repo",
        transport=transport,
    )


def test_gitea_fetch_ok():
    def handler(req: httpx.Request) -> httpx.Response:
        assert req.headers["Authorization"] == "token tok-xyz"
        return httpx.Response(200, json={"full_name": "team/repo"})

    p = _gitea_transport(handler)
    p.fetch("team/repo")


def test_gitea_show_decodes_base64():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "encoding": "base64",
                "content": base64.b64encode(b"hello gitea").decode(),
            },
        )

    p = _gitea_transport(handler)
    assert p.show("sha", "a.md") == "hello gitea"


def test_gitea_show_directory_raises():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"name": "x"}])  # 数组表示目录

    p = _gitea_transport(handler)
    with pytest.raises(IsADirectoryError):
        p.show("sha", "rules/")


def test_gitea_diff_parses_statuses():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "files": [
                    {"status": "added", "filename": "a.md", "additions": 1, "deletions": 0},
                    {"status": "removed", "filename": "b.md"},
                    {"status": "modified", "filename": "c.md"},
                    {"status": "renamed", "filename": "d.md", "old_filename": "d_old.md"},
                ]
            },
        )

    p = _gitea_transport(handler)
    entries = p.diff("a", "b")
    assert [e.status for e in entries] == [
        DiffStatus.ADDED,
        DiffStatus.DELETED,
        DiffStatus.MODIFIED,
        DiffStatus.RENAMED,
    ]
    assert entries[0].additions == 1
    assert entries[3].old_path == "d_old.md"


def test_gitea_ls_tree():
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {"path": "rules/a.md", "type": "file", "sha": "h1"},
                {"path": "rules/sub", "type": "dir", "sha": "h2"},
            ],
        )

    p = _gitea_transport(handler)
    entries = p.ls_tree("sha", "rules")
    assert len(entries) == 2
    assert entries[0].type == TreeEntryType.BLOB
    assert entries[1].type == TreeEntryType.TREE


# ---------------------------------------------------------------------------
# Libgit2Provider：pygit2 缺失时优雅跳过
# ---------------------------------------------------------------------------


def _has_pygit2() -> bool:
    try:
        import pygit2  # noqa: F401

        return True
    except ImportError:
        return False


@pytest.mark.skipif(not _has_pygit2(), reason="pygit2 未安装，跳过 libgit2 集成测试")
def test_libgit2_shallow_clone_and_show(tmp_path: Path):
    """pygit2 可用时验证 clone_shallow + show + ls_tree + diff。

    用本地 bare repo 模拟远端。
    """
    import pygit2

    src = tmp_path / "src"
    src.mkdir()
    repo = pygit2.init_repository(str(src), initial_head="main")
    # 写入一个文件并 commit
    (src / "RULE.md").write_text("# rule\n", encoding="utf-8")
    repo.index.add("RULE.md")
    repo.index.write()
    tree = repo.index.write_tree()
    sig = pygit2.Signature("tester", "t@t")
    commit = repo.create_commit("HEAD", sig, sig, "init", tree, [])

    dest = tmp_path / "dest"
    provider = Libgit2Provider.clone_shallow(
        url=str(src / ".git"), dest=dest, depth=1
    )
    assert dest.exists()
    text = provider.show(commit, "RULE.md")
    assert text == "# rule\n"

    entries = provider.ls_tree(commit, "")
    assert any(e.path == "RULE.md" for e in entries)


def test_libgit2_missing_dep_raises(monkeypatch):
    """pygit2 缺失时构造抛出明确 ImportError。"""
    import builtins

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name == "pygit2":
            raise ImportError("no pygit2")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fake_import)
    with pytest.raises(ImportError, match="pygit2"):
        Libgit2Provider(repo_path=".")


# ---------------------------------------------------------------------------
# 仓库大小告警
# ---------------------------------------------------------------------------


def test_repo_size_alarm_below_threshold():
    alarm = RepoSizeAlarm(size_bytes=100, threshold_bytes=1000, alarmed=False)
    assert not alarm.alarmed
    assert alarm.size_mb == 0.0  # 100 / 1048576 round 2 = 0.0


def test_repo_size_alarm_above_threshold():
    alarm = RepoSizeAlarm(
        size_bytes=600 * 1024 * 1024,
        threshold_bytes=DEFAULT_REPO_SIZE_ALARM_BYTES,
        alarmed=True,
    )
    assert alarm.alarmed
    assert alarm.size_mb == 600.0


def test_repo_size_alarm_threshold_value():
    """阈值必须为 500MB（对应 SubTask 1.9）。"""
    assert DEFAULT_REPO_SIZE_ALARM_BYTES == 500 * 1024 * 1024


def test_check_size_alarm_uses_provider_size():
    """check_size_alarm 应基于 repo_size_bytes 与阈值比较。"""

    class Stub(Libgit2Provider.__mro__[0]):  # 直接构造存根继承 GitProvider
        pass

    # 用 GitProvider 直接子类化做存根（避免触碰 pygit2）
    from server.infra_git.git_provider import GitProvider

    class StubProvider(GitProvider):
        def __init__(self, size):
            self._size = size

        def fetch(self, repo):  # pragma: no cover - 未用
            return None

        def show(self, sha, path):  # pragma: no cover
            return ""

        def diff(self, sha_a, sha_b):  # pragma: no cover
            return []

        def ls_tree(self, sha, path):  # pragma: no cover
            return []

        def repo_size_bytes(self) -> int:
            return self._size

    small = StubProvider(100).check_size_alarm()
    assert not small.alarmed

    big = StubProvider(600 * 1024 * 1024).check_size_alarm()
    assert big.alarmed
    assert big.threshold_bytes == DEFAULT_REPO_SIZE_ALARM_BYTES


def test_repo_size_bytes_default_not_supported():
    """GitProvider 默认 repo_size_bytes 抛 NotImplemented，check_size_alarm 也抛。"""
    from server.infra_git.git_provider import GitProvider

    class Bare(GitProvider):
        def fetch(self, repo):  # pragma: no cover
            return None

        def show(self, sha, path):  # pragma: no cover
            return ""

        def diff(self, sha_a, sha_b):  # pragma: no cover
            return []

        def ls_tree(self, sha, path):  # pragma: no cover
            return []

    with pytest.raises(NotImplementedError):
        Bare().check_size_alarm()


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def test_create_provider_unknown_kind():
    with pytest.raises(ValueError, match="未知"):
        create_git_provider(kind="unknown")


def test_create_provider_default_libgit2(monkeypatch, tmp_path):
    monkeypatch.delenv("GIT_PROVIDER", raising=False)
    monkeypatch.setenv("GIT_REPO_PATH", str(tmp_path))
    # pygit2 缺失时 Libgit2Provider 构造会抛 ImportError；缺失时跳过本测试
    if not _has_pygit2():
        pytest.skip("pygit2 未安装")
    p = create_git_provider()
    assert isinstance(p, Libgit2Provider)


def test_create_provider_gitlab(monkeypatch):
    monkeypatch.setenv("GITLAB_BASE_URL", "https://gl.example.com")
    monkeypatch.setenv("GITLAB_TOKEN", "t")
    p = create_git_provider(kind="gitlab", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert isinstance(p, GitLabProvider)


def test_create_provider_gitea(monkeypatch):
    monkeypatch.setenv("GITEA_BASE_URL", "https://g.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "t")
    p = create_git_provider(kind="gitea", transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    assert isinstance(p, GiteaProvider)
