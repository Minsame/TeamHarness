"""Git Provider 抽象层。

定义统一接口 fetch / show / diff / ls_tree，并提供三实现：
- GitLabProvider：基于 GitLab API v4（httpx）
- GiteaProvider：基于 Gitea API（httpx）
- Libgit2Provider：基于 pygit2（本地仓库，支持 shallow clone + 仓库大小告警）

通过 GIT_PROVIDER 环境变量 + 配置切换，服务端启动时加载对应实现。
本模块为占位 API 契约提供方，供 infra_db / recall / binding 等领域调用。
"""

from __future__ import annotations

import base64
import os
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

from server.common.models import DiffEntry, DiffStatus, TreeEntry, TreeEntryType

# 仓库大小告警阈值（500MB），对应技术方案 SubTask 1.9
DEFAULT_REPO_SIZE_ALARM_BYTES = 500 * 1024 * 1024


@dataclass
class RepoSizeAlarm:
    """仓库大小告警结果。"""

    size_bytes: int
    threshold_bytes: int
    alarmed: bool

    @property
    def size_mb(self) -> float:
        return round(self.size_bytes / (1024 * 1024), 2)


class GitProvider(ABC):
    """Git Provider 抽象接口（占位 API 契约）。

    所有实现需支持四项核心操作。读操作以 commit SHA 为输入，
    保证幂等与可重现。fetch 用于同步远端到本地视图。
    """

    @abstractmethod
    def fetch(self, repo: str) -> None:
        """拉取远端最新状态。repo 为仓库标识（项目路径 / 本地路径）。"""

    @abstractmethod
    def show(self, sha: str, path: str) -> str:
        """读取指定 commit 下某路径的文件内容（文本）。"""

    @abstractmethod
    def diff(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        """对比两个 commit，返回文件级变更清单。"""

    @abstractmethod
    def ls_tree(self, sha: str, path: str) -> list[TreeEntry]:
        """列出指定 commit 某路径下的树条目。"""

    # 非抽象能力：仓库大小告警（默认实现抛 NotImplemented，由支持本地的实现覆写）
    def repo_size_bytes(self) -> int:
        raise NotImplementedError("当前 Provider 不支持本地仓库大小查询")

    def check_size_alarm(
        self, threshold_bytes: int = DEFAULT_REPO_SIZE_ALARM_BYTES
    ) -> RepoSizeAlarm:
        """检查仓库大小是否超阈值，超 500MB 触发告警。"""
        size = self.repo_size_bytes()
        return RepoSizeAlarm(
            size_bytes=size,
            threshold_bytes=threshold_bytes,
            alarmed=size >= threshold_bytes,
        )


# ---------------------------------------------------------------------------
# GitLab 实现
# ---------------------------------------------------------------------------


class GitLabProvider(GitProvider):
    """基于 GitLab API v4 的实现。"""

    api_suffix = "/api/v4"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        repo: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = self.base_url + self.api_suffix
        self.token = token
        self._repo = repo  # namespace/project
        self._transport: httpx.BaseTransport | None = transport
        self._client: httpx.BaseClient | None = None
        self._timeout = timeout

    @property
    def client(self) -> httpx.BaseClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self.api_base,
                "headers": {"PRIVATE-TOKEN": self.token},
                "timeout": self._timeout,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def set_repo(self, repo: str) -> None:
        """设置当前操作的项目标识（namespace/project）。"""
        self._repo = repo

    @staticmethod
    def _project_id(repo: str) -> str:
        # GitLab 项目标识需整体 URL 编码：namespace/project → namespace%2Fproject
        return repo.replace("/", "%2F")

    def fetch(self, repo: str) -> None:
        # API 模式下远端即权威源，fetch 校验项目可达并登记当前仓库
        self.set_repo(repo)
        resp = self.client.get(f"/projects/{self._project_id(repo)}")
        resp.raise_for_status()

    def show(self, sha: str, path: str) -> str:
        encoded_path = path.replace("/", "%2F")
        url = f"/projects/{self._project_id(self._repo)}/repository/files/{encoded_path}"
        resp = self.client.get(url, params={"ref": sha})
        resp.raise_for_status()
        data = resp.json()
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8")
        return data.get("content", "")

    def diff(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        url = f"/projects/{self._project_id(self._repo)}/repository/compare"
        resp = self.client.get(url, params={"from": sha_a, "to": sha_b})
        resp.raise_for_status()
        diffs = resp.json().get("diffs", [])
        return [self._parse_diff(d) for d in diffs]

    def ls_tree(self, sha: str, path: str) -> list[TreeEntry]:
        url = f"/projects/{self._project_id(self._repo)}/repository/tree"
        params: dict[str, Any] = {"ref": sha, "path": path, "per_page": 100}
        entries: list[TreeEntry] = []
        page = 1
        while True:
            params["page"] = page
            resp = self.client.get(url, params=params)
            resp.raise_for_status()
            items = resp.json()
            if not items:
                break
            for it in items:
                entries.append(
                    TreeEntry(
                        path=it["path"],
                        type=TreeEntryType.TREE if it["type"] == "tree" else TreeEntryType.BLOB,
                        sha=it["id"],
                        mode=it.get("mode", ""),
                    )
                )
            if len(items) < 100:
                break
            page += 1
        return entries

    @staticmethod
    def _parse_diff(d: dict[str, Any]) -> DiffEntry:
        new_path = d.get("new_path") or d.get("old_path") or ""
        old_path = d.get("old_path")
        status: DiffStatus
        if d.get("new_file"):
            status = DiffStatus.ADDED
        elif d.get("deleted_file"):
            status = DiffStatus.DELETED
        elif d.get("renamed_file"):
            status = DiffStatus.RENAMED
        else:
            status = DiffStatus.MODIFIED
        return DiffEntry(
            path=new_path,
            status=status,
            old_path=old_path,
            new_path=new_path,
            patch=d.get("diff"),
        )


# ---------------------------------------------------------------------------
# Gitea 实现
# ---------------------------------------------------------------------------


class GiteaProvider(GitProvider):
    """基于 Gitea API 的实现（类 GitHub 风格 API）。"""

    api_suffix = "/api/v1"

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        repo: str = "",
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_base = self.base_url + self.api_suffix
        self.token = token
        self._repo = repo  # owner/repo
        self._transport: httpx.BaseTransport | None = transport
        self._client: httpx.BaseClient | None = None
        self._timeout = timeout

    @property
    def client(self) -> httpx.BaseClient:
        if self._client is None:
            kwargs: dict[str, Any] = {
                "base_url": self.api_base,
                "headers": {"Authorization": f"token {self.token}"},
                "timeout": self._timeout,
            }
            if self._transport is not None:
                kwargs["transport"] = self._transport
            self._client = httpx.Client(**kwargs)
        return self._client

    def set_repo(self, repo: str) -> None:
        """设置当前操作仓库标识（owner/repo）。"""
        self._repo = repo

    def fetch(self, repo: str) -> None:
        self.set_repo(repo)
        resp = self.client.get(f"/repos/{repo}")
        resp.raise_for_status()

    def show(self, sha: str, path: str) -> str:
        # Gitea contents API 返回 base64 content
        resp = self.client.get(f"/repos/{self._repo}/contents/{path}", params={"ref": sha})
        resp.raise_for_status()
        data = resp.json()
        if isinstance(data, list):
            # 路径指向目录时 API 返回数组，此处约定 show 仅用于文件
            raise IsADirectoryError(f"path 指向目录而非文件: {path}")
        if data.get("encoding") == "base64":
            return base64.b64decode(data["content"]).decode("utf-8")
        return data.get("content", "")

    def diff(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        resp = self.client.get(f"/repos/{self._repo}/compare/{sha_a}...{sha_b}")
        resp.raise_for_status()
        files = resp.json().get("files", [])
        return [self._parse_file(f) for f in files]

    def ls_tree(self, sha: str, path: str) -> list[TreeEntry]:
        url = f"/repos/{self._repo}/contents/{path}".rstrip("/")
        resp = self.client.get(url, params={"ref": sha})
        resp.raise_for_status()
        items = resp.json()
        if not isinstance(items, list):
            items = [items]
        entries: list[TreeEntry] = []
        for it in items:
            entries.append(
                TreeEntry(
                    path=it["path"],
                    type=TreeEntryType.TREE if it["type"] == "dir" else TreeEntryType.BLOB,
                    sha=it.get("sha", ""),
                    mode="",  # contents API 不返回 mode
                )
            )
        return entries

    @staticmethod
    def _parse_file(f: dict[str, Any]) -> DiffEntry:
        status_map = {
            "added": DiffStatus.ADDED,
            "removed": DiffStatus.DELETED,
            "modified": DiffStatus.MODIFIED,
            "renamed": DiffStatus.RENAMED,
        }
        status = status_map.get(f.get("status", "modified"), DiffStatus.MODIFIED)
        return DiffEntry(
            path=f.get("filename", ""),
            status=status,
            old_path=f.get("old_filename"),
            new_path=f.get("filename"),
            additions=f.get("additions", 0),
            deletions=f.get("deletions", 0),
            patch=f.get("patch"),
        )


# ---------------------------------------------------------------------------
# libgit2 实现（pygit2）
# ---------------------------------------------------------------------------


class Libgit2Provider(GitProvider):
    """基于 pygit2 的本地仓库实现。

    支持完整 git 操作：fetch / show / diff / ls_tree，
    额外支持 shallow clone 与仓库大小告警。
    pygit2 为可选依赖，缺失时构造抛出明确错误。
    """

    def __init__(self, repo_path: str | Path) -> None:
        try:
            import pygit2  # noqa: F401  延迟导入用于能力探测
        except ImportError as exc:  # pragma: no cover - 环境依赖
            raise ImportError(
                "Libgit2Provider 需要 pygit2，请安装：pip install pygit2"
            ) from exc
        self.repo_path = Path(repo_path)
        self._repo: Any = None
        if self.repo_path.exists():
            self._repo = pygit2.Repository(str(self.repo_path))

    @property
    def repo(self) -> Any:
        if self._repo is None:
            import pygit2

            self._repo = pygit2.Repository(str(self.repo_path))
        return self._repo

    # ---- 额外能力：shallow clone ----

    @classmethod
    def clone_shallow(
        cls, url: str, dest: str | Path, *, depth: int = 1, bare: bool = False
    ) -> "Libgit2Provider":
        """浅克隆仓库（--depth=1），返回新 provider 实例。

        用于服务端冷启动拉取中央仓库镜像，减少历史传输。
        """
        import pygit2

        dest = Path(dest)
        if dest.exists() and any(dest.iterdir()):
            raise FileExistsError(f"目标目录非空：{dest}")
        dest.mkdir(parents=True, exist_ok=True)
        pygit2.clone_repository(
            url,
            str(dest),
            bare=bare,
            depth=depth,
        )
        return cls(dest)

    def fetch(self, repo: str) -> None:
        """从远端 origin 拉取最新引用。repo 参数为远端 URL（仅当需重设 origin 时使用）。"""
        import pygit2

        if repo and repo != str(self.repo_path):
            try:
                self.repo.remotes["origin"].url = repo
            except KeyError:
                self.repo.remotes.create("origin", repo)
        remote = self.repo.remotes["origin"]
        remote.fetch(callbacks=pygit2.RemoteCallbacks())  # 凭据由 git config 提供

    def _peel_commit(self, sha: str) -> Any:
        """将任意引用解析为 commit 对象。"""
        import pygit2

        obj = self.repo[sha]
        # 若是 tag/branch 等引用，peel 到 commit
        return obj.peel(pygit2.GIT_OBJ_COMMIT)

    def show(self, sha: str, path: str) -> str:
        commit = self._peel_commit(sha)
        tree = commit.tree
        obj = tree
        for part in path.split("/"):
            if not part:
                continue
            obj = self.repo[obj[part].id]
        blob = self.repo[obj.id]
        return blob.data.decode("utf-8")

    def diff(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        import pygit2

        commit_a = self._peel_commit(sha_a)
        commit_b = self._peel_commit(sha_b)
        diffs = self.repo.diff(commit_a.tree, commit_b.tree)
        entries: list[DiffEntry] = []
        for d in diffs:
            status = DiffStatus.MODIFIED
            if d.status == pygit2.GIT_DELTA_ADDED:
                status = DiffStatus.ADDED
            elif d.status == pygit2.GIT_DELTA_DELETED:
                status = DiffStatus.DELETED
            elif d.status == pygit2.GIT_DELTA_RENAMED:
                status = DiffStatus.RENAMED
            entries.append(
                DiffEntry(
                    path=d.new_file_path or d.old_file_path,
                    status=status,
                    old_path=d.old_file_path,
                    new_path=d.new_file_path,
                    patch=d.patch,
                )
            )
        return entries

    def ls_tree(self, sha: str, path: str) -> list[TreeEntry]:
        import pygit2

        commit = self._peel_commit(sha)
        tree = commit.tree
        obj = tree
        for part in path.split("/"):
            if not part:
                continue
            obj = self.repo[obj[part].id]
        entries: list[TreeEntry] = []
        for entry in obj:
            entry_type = (
                TreeEntryType.TREE
                if entry.type == pygit2.GIT_OBJ_TREE
                else TreeEntryType.BLOB
            )
            entries.append(
                TreeEntry(
                    path=entry.name,
                    type=entry_type,
                    sha=str(entry.hex),
                    size=entry.size if entry_type == TreeEntryType.BLOB else 0,
                )
            )
        return entries

    # ---- 额外能力：仓库大小告警 ----

    def repo_size_bytes(self) -> int:
        """统计仓库目录占用磁盘字节数（含 .git）。"""
        total = 0
        for f in self.repo_path.rglob("*"):
            if f.is_file():
                try:
                    total += f.stat().st_size
                except OSError:
                    continue
        return total

    def gc(self) -> None:
        """仓库 GC（对 pack 优化，对应缺陷修复 2.2 仓库 GC）。

        pygit2 不直接提供 gc，这里通过 git 命令执行；
        缺少 git 可执行时静默跳过。
        """
        import subprocess

        try:
            subprocess.run(
                ["git", "gc", "--auto", "--prune=now"],
                cwd=str(self.repo_path),
                check=False,
                capture_output=True,
            )
        except FileNotFoundError:
            # 无 git 可执行，跳过
            pass


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_git_provider(
    *, kind: str | None = None, **kwargs: Any
) -> GitProvider:
    """按 GIT_PROVIDER 环境变量或显式 kind 创建 Provider。

    kind 取值：gitlab / gitea / libgit2。
    """
    kind = (kind or os.environ.get("GIT_PROVIDER") or "libgit2").lower()
    if kind == "gitlab":
        return GitLabProvider(
            base_url=kwargs.get("base_url") or os.environ["GITLAB_BASE_URL"],
            token=kwargs.get("token") or os.environ["GITLAB_TOKEN"],
            repo=kwargs.get("repo", ""),
            timeout=kwargs.get("timeout", 30.0),
            transport=kwargs.get("transport"),
        )
    if kind == "gitea":
        return GiteaProvider(
            base_url=kwargs.get("base_url") or os.environ["GITEA_BASE_URL"],
            token=kwargs.get("token") or os.environ["GITEA_TOKEN"],
            repo=kwargs.get("repo", ""),
            timeout=kwargs.get("timeout", 30.0),
            transport=kwargs.get("transport"),
        )
    if kind == "libgit2":
        repo_path = kwargs.get("repo_path") or os.environ.get("GIT_REPO_PATH", ".")
        return Libgit2Provider(repo_path)
    raise ValueError(f"未知 Git Provider 类型：{kind}")
