"""git 同步封装（sync: pull --rebase + push / pr / 冲突 diff 辅助）。

对应 SubTask 6.2 + 技术方案 3.1.3「客户端封装」：
- teamharness sync：一键 pull --rebase + push 个人分支
- teamharness pr：从个人分支向 main 发起 PR
- 冲突 diff 辅助：检测冲突文件 + 渲染三方 diff 视图
- 不隐藏 git，高级用户可直接用 git 命令操作

底层依赖：
- 本地 git 命令（subprocess）：pull/push/rebase/commit/diff/status
- Agent 1 的 GitProvider：用于 PR 创建（GitLab/Gitea HTTP API）
  当 GitProvider 为 Libgit2Provider 或不可用时，PR 创建走"推送 + 提示用户手动创建"降级

不执行 git 提交与推送 unless 用户显式调用 sync() 方法（rule: 未经允许不可执行 git 提交和推送）。
本模块只提供能力，是否调用由 CLI / 守护进程按用户指令决定。
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.common.models import DiffEntry, DiffStatus
from server.infra_git.git_provider import (
    GiteaProvider,
    GitLabProvider,
    GitProvider,
    Libgit2Provider,
)

# 标记：未配置远程时返回的占位 SHA
NULL_SHA = "0000000000000000000000000000000000000000"


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass
class GitSyncResult:
    """sync 命令结果。"""

    pulled: bool = False
    rebased: bool = False
    pushed: bool = False
    upstream_commit: str = ""
    head_commit: str = ""
    conflicts: list[str] = field(default_factory=list)
    error: str | None = None
    raw_log: str = ""

    @property
    def ok(self) -> bool:
        return self.error is None and not self.conflicts


@dataclass
class PrCreationResult:
    """pr 命令结果。"""

    created: bool
    pr_id: int | None = None
    pr_url: str | None = None
    branch: str = ""
    target: str = ""
    error: str | None = None
    fallback_message: str | None = None  # 降级提示（如手动创建 PR）


@dataclass
class ConflictDiff:
    """冲突文件 diff 视图项。"""

    path: str
    status: DiffStatus
    our_version: str = ""    # 当前分支（个人）的版本内容
    their_version: str = ""  # 上游（main）的版本内容
    base_version: str = ""   # 共同祖先版本
    patch: str | None = None


@dataclass
class CommitResult:
    """commit 结果。"""

    committed: bool
    commit_sha: str = ""
    message: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# GitSync
# ---------------------------------------------------------------------------


class GitSync:
    """git 同步封装。

    使用方式：
        sync = GitSync(repo_root=Path(...), git_provider=gitlab_provider)
        result = sync.sync(personal_branch="members/alice", target_branch="main")
        if result.conflicts:
            diffs = sync.conflict_diffs()

    依赖：
    - git 可执行（subprocess 调用）；缺失则降级仅返回错误
    - git_provider（可选）：用于 PR 创建 HTTP API；为 Libgit2Provider 或 None 时
      PR 命令降级为"推送 + 提示手动创建"
    """

    def __init__(
        self,
        repo_root: Path | str,
        *,
        git_provider: GitProvider | None = None,
        default_remote: str = "origin",
    ) -> None:
        self.repo_root = Path(repo_root)
        self.git_provider = git_provider
        self.default_remote = default_remote

    # ------------------------------------------------------------------
    # 基础工具：subprocess git 调用
    # ------------------------------------------------------------------

    def _git(self, *args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
        """统一 git 命令调用。

        raises FileNotFoundError 若 git 可执行缺失。
        raises subprocess.CalledProcessError 若 check=True 且 git 退出非零。
        """
        git_exe = shutil.which("git")
        if git_exe is None:
            raise FileNotFoundError("git 可执行未安装，无法执行 git 命令")
        return subprocess.run(
            [git_exe, *args],
            cwd=str(self.repo_root),
            check=check,
            capture_output=capture,
            text=True,
        )

    def _git_soft(self, *args: str) -> subprocess.CompletedProcess:
        """git 调用但不 raise（用于冲突检测等需要解析输出的场景）。"""
        return self._git(*args, check=False)

    def has_git(self) -> bool:
        """仓库是否已初始化 git（存在 .git 目录）。"""
        return (self.repo_root / ".git").exists()

    def current_branch(self) -> str:
        """返回当前分支名（失败返回空字符串）。

        detached HEAD 状态下 git 返回 "HEAD"，视为无分支（返回空字符串），
        避免 sync() 将 "HEAD" 误用为分支名。
        """
        if not self.has_git():
            return ""
        try:
            r = self._git("rev-parse", "--abbrev-ref", "HEAD")
            branch = r.stdout.strip()
            # detached HEAD 状态下 git 返回 "HEAD"，视为无分支
            if branch == "HEAD":
                return ""
            return branch
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    def current_commit(self) -> str:
        """返回 HEAD commit SHA。"""
        if not self.has_git():
            return ""
        try:
            r = self._git("rev-parse", "HEAD")
            return r.stdout.strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return ""

    def has_uncommitted_changes(self) -> bool:
        """是否有未提交变更（工作区或暂存区）。"""
        if not self.has_git():
            return False
        try:
            r = self._git("status", "--porcelain")
            return bool(r.stdout.strip())
        except (subprocess.CalledProcessError, FileNotFoundError):
            return False

    def ensure_branch(self, branch: str, *, base: str = "main") -> None:
        """确保本地存在 branch 分支；不存在则从 base 创建。

        raises subprocess.CalledProcessError 失败。
        """
        if not self.has_git():
            raise RuntimeError("仓库未初始化 git")
        # 检查分支是否已存在
        r = self._git_soft("rev-parse", "--verify", f"refs/heads/{branch}")
        if r.returncode == 0:
            return
        # 创建分支
        self._git("checkout", "-b", branch, base)

    def checkout(self, branch: str) -> None:
        """切换到分支。"""
        self._git("checkout", branch)

    # ------------------------------------------------------------------
    # commit（不自动调用，由 CLI 显式触发）
    # ------------------------------------------------------------------

    def stage_all(self) -> None:
        """git add -A（含新增/修改/删除）。"""
        self._git("add", "-A")

    def stage_paths(self, paths: list[str]) -> None:
        """git add 指定路径列表。"""
        if not paths:
            return
        self._git("add", "--", *paths)

    def commit(self, message: str, *, allow_empty: bool = False) -> CommitResult:
        """git commit -m <message>。

        返回 CommitResult；失败时 error 字段填充。
        不会自动 push。
        """
        if not self.has_git():
            return CommitResult(committed=False, error="仓库未初始化 git")
        try:
            args = ["commit", "-m", message]
            if allow_empty:
                args.append("--allow-empty")
            r = self._git_soft(*args)
            if r.returncode != 0:
                # 无变更提交时 git 退出 1 但不算错误
                if "nothing to commit" in r.stdout or "no changes added" in r.stdout:
                    return CommitResult(committed=False, message="nothing to commit")
                return CommitResult(committed=False, error=r.stderr.strip() or r.stdout.strip())
            sha = self.current_commit()
            return CommitResult(committed=True, commit_sha=sha, message=message)
        except FileNotFoundError as exc:
            return CommitResult(committed=False, error=str(exc))

    # ------------------------------------------------------------------
    # sync：pull --rebase + push
    # ------------------------------------------------------------------

    def sync(
        self,
        *,
        personal_branch: str | None = None,
        target_branch: str = "main",
        remote: str | None = None,
    ) -> GitSyncResult:
        """一键 pull --rebase + push 个人分支。

        流程：
        1. 切到 personal_branch（缺省为当前分支）
        2. git fetch <remote>
        3. git rebase <remote>/<target_branch>（变基到上游最新）
        4. 若冲突 → 返回 conflicts 列表，不 push
        5. git push <remote> <personal_branch>（首次推送 -u）

        个人分支不存在时自动从 target_branch 创建。
        """
        result = GitSyncResult()
        if not self.has_git():
            result.error = "仓库未初始化 git"
            return result

        remote = remote or self.default_remote
        branch = personal_branch or self.current_branch()
        if not branch:
            result.error = "未指定 personal_branch 且当前不在任何分支上"
            return result

        log_lines: list[str] = []
        try:
            # 1. 确保分支存在 + 切换
            self.ensure_branch(branch, base=target_branch)
            self.checkout(branch)
            # 2. fetch
            r = self._git_soft("fetch", remote)
            log_lines.append(f"$ git fetch {remote}\n{r.stdout}{r.stderr}")
            if r.returncode != 0:
                result.error = f"fetch 失败: {r.stderr.strip()}"
                result.raw_log = "\n".join(log_lines)
                return result
            result.pulled = True
            # 3. rebase
            r = self._git_soft("rebase", f"{remote}/{target_branch}")
            log_lines.append(f"$ git rebase {remote}/{target_branch}\n{r.stdout}{r.stderr}")
            if r.returncode != 0:
                # 冲突
                result.rebased = False
                result.conflicts = self._list_conflict_files()
                result.error = f"rebase 冲突: {result.conflicts}"
                result.raw_log = "\n".join(log_lines)
                # 不自动 abort，保留冲突状态供用户/CLI 处理
                return result
            result.rebased = True
            # 4. 获取 commit SHAs
            result.upstream_commit = self._resolve_ref(f"{remote}/{target_branch}")
            result.head_commit = self.current_commit()
            # 5. push
            push_args = ["push"]
            # 首次推送设置 upstream
            if not self._branch_tracks_remote(branch, remote):
                push_args.extend(["-u", remote, branch])
            else:
                push_args.extend([remote, branch])
            r = self._git_soft(*push_args)
            log_lines.append(f"$ git push {' '.join(push_args[1:])}\n{r.stdout}{r.stderr}")
            if r.returncode != 0:
                result.error = f"push 失败: {r.stderr.strip()}"
                result.raw_log = "\n".join(log_lines)
                return result
            result.pushed = True
            result.raw_log = "\n".join(log_lines)
            return result
        except FileNotFoundError as exc:
            result.error = str(exc)
            result.raw_log = "\n".join(log_lines)
            return result

    # ------------------------------------------------------------------
    # PR 创建
    # ------------------------------------------------------------------

    def create_pr(
        self,
        *,
        title: str,
        body: str = "",
        branch: str | None = None,
        target: str = "main",
        remote: str | None = None,
    ) -> PrCreationResult:
        """发起 PR。

        - 若 git_provider 为 GitLabProvider/GiteaProvider → 走 HTTP API
        - 若 git_provider 为 Libgit2Provider 或 None → 仅推送 + 返回降级提示

        branch 缺省为当前分支。
        """
        branch = branch or self.current_branch()
        if not branch:
            return PrCreationResult(created=False, error="未指定 branch 且当前无分支")
        remote = remote or self.default_remote

        # 确保 branch 已推送到 remote
        r = self._git_soft("push", "-u", remote, branch)
        if r.returncode != 0 and "Everything up-to-date" not in r.stdout:
            return PrCreationResult(
                created=False,
                branch=branch,
                target=target,
                error=f"push 失败: {r.stderr.strip()}",
            )

        provider = self.git_provider
        if isinstance(provider, GitLabProvider):
            return self._create_gitlab_mr(provider, title=title, body=body, branch=branch, target=target)
        if isinstance(provider, GiteaProvider):
            return self._create_gitea_pr(provider, title=title, body=body, branch=branch, target=target)
        # 降级：无法自动创建 PR
        return PrCreationResult(
            created=False,
            branch=branch,
            target=target,
            fallback_message=(
                f"已推送分支 {branch} 到 {remote}。"
                f"Git Provider ({type(provider).__name__ if provider else 'None'}) 不支持自动创建 PR，"
                "请手动在 GitLab/Gitea Web UI 创建 Merge Request/Pull Request。"
            ),
        )

    def _create_gitlab_mr(
        self,
        provider: GitLabProvider,
        *,
        title: str,
        body: str,
        branch: str,
        target: str,
    ) -> PrCreationResult:
        """GitLab: POST /projects/:id/merge_requests。"""
        try:
            client = provider.client
            # 确保 provider 已 set_repo
            if not provider._repo:
                # 跳过：调用方应在初始化时 set_repo
                return PrCreationResult(
                    created=False,
                    branch=branch,
                    target=target,
                    error="GitLabProvider 未设置 repo（namespace/project）",
                )
            url = f"/projects/{provider._project_id(provider._repo)}/merge_requests"
            payload = {
                "title": title,
                "description": body,
                "source_branch": branch,
                "target_branch": target,
            }
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return PrCreationResult(
                created=True,
                pr_id=data.get("iid"),
                pr_url=data.get("web_url"),
                branch=branch,
                target=target,
            )
        except Exception as exc:  # noqa: BLE001 - HTTP 错误统一捕获
            return PrCreationResult(
                created=False,
                branch=branch,
                target=target,
                error=f"GitLab MR 创建失败: {exc}",
            )

    def _create_gitea_pr(
        self,
        provider: GiteaProvider,
        *,
        title: str,
        body: str,
        branch: str,
        target: str,
    ) -> PrCreationResult:
        """Gitea: POST /repos/:owner/:repo/pulls。"""
        try:
            client = provider.client
            if not provider._repo:
                return PrCreationResult(
                    created=False,
                    branch=branch,
                    target=target,
                    error="GiteaProvider 未设置 repo（owner/repo）",
                )
            url = f"/repos/{provider._repo}/pulls"
            payload = {
                "title": title,
                "body": body,
                "head": branch,
                "base": target,
            }
            resp = client.post(url, json=payload)
            resp.raise_for_status()
            data = resp.json()
            return PrCreationResult(
                created=True,
                pr_id=data.get("number"),
                pr_url=data.get("html_url"),
                branch=branch,
                target=target,
            )
        except Exception as exc:  # noqa: BLE001
            return PrCreationResult(
                created=False,
                branch=branch,
                target=target,
                error=f"Gitea PR 创建失败: {exc}",
            )

    # ------------------------------------------------------------------
    # 冲突 diff 辅助
    # ------------------------------------------------------------------

    def _list_conflict_files(self) -> list[str]:
        """git diff --name-only --diff-filter=U 列出冲突文件。"""
        r = self._git_soft("diff", "--name-only", "--diff-filter=U")
        if r.returncode != 0:
            return []
        return [line.strip() for line in r.stdout.splitlines() if line.strip()]

    def list_conflicts(self) -> list[str]:
        """公开接口：列出当前冲突文件。"""
        return self._list_conflict_files()

    def conflict_diffs(self, paths: list[str] | None = None) -> list[ConflictDiff]:
        """渲染冲突文件的 diff 视图。

        对每个冲突文件：
        - our_version: git show :2:<path>（当前分支版本）
        - their_version: git show :3:<path>（上游版本）
        - base_version: git show :1:<path>（共同祖先）
        - patch: git diff <path>（含冲突标记的合并视图）

        paths 为 None 时取所有冲突文件。
        """
        targets = paths if paths is not None else self._list_conflict_files()
        results: list[ConflictDiff] = []
        for p in targets:
            our = self._read_stage(p, stage=2)
            their = self._read_stage(p, stage=3)
            base = self._read_stage(p, stage=1)
            r = self._git_soft("diff", "--", p)
            patch = r.stdout if r.returncode == 0 else None
            results.append(
                ConflictDiff(
                    path=p,
                    status=DiffStatus.MODIFIED,
                    our_version=our,
                    their_version=their,
                    base_version=base,
                    patch=patch,
                )
            )
        return results

    def _read_stage(self, path: str, stage: int) -> str:
        """读取暂存区某 stage 的文件内容（1=base, 2=ours, 3=theirs）。"""
        r = self._git_soft("show", f":{stage}:{path}")
        if r.returncode != 0:
            return ""
        return r.stdout

    def abort_rebase(self) -> bool:
        """git rebase --abort（用户放弃冲突解决时调用）。"""
        r = self._git_soft("rebase", "--abort")
        return r.returncode == 0

    def continue_rebase(self) -> bool:
        """解决冲突后 git rebase --continue。"""
        # 需要先 stage 已解决的文件
        r = self._git_soft("rebase", "--continue")
        return r.returncode == 0

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _resolve_ref(self, ref: str) -> str:
        """解析 ref 到 commit SHA；失败返回空。"""
        r = self._git_soft("rev-parse", ref)
        if r.returncode != 0:
            return ""
        return r.stdout.strip()

    def _branch_tracks_remote(self, branch: str, remote: str) -> bool:
        """检查 branch 是否已配置追踪 remote/branch。"""
        r = self._git_soft("config", f"branch.{branch}.remote")
        if r.returncode != 0:
            return False
        return r.stdout.strip() == remote

    def diff_between(self, sha_a: str, sha_b: str) -> list[DiffEntry]:
        """委托 GitProvider.diff；为 Libgit2Provider 缺省时走 git CLI。"""
        provider = self.git_provider
        if provider is not None:
            try:
                return provider.diff(sha_a, sha_b)
            except Exception:  # noqa: BLE001
                pass
        # 降级：git CLI
        r = self._git_soft("diff", "--name-status", sha_a, sha_b)
        if r.returncode != 0:
            return []
        entries: list[DiffEntry] = []
        for line in r.stdout.splitlines():
            if not line.strip():
                continue
            parts = line.split("\t", 2)
            if len(parts) < 2:
                continue
            status_code = parts[0]
            new_path = parts[1]
            old_path = parts[2] if len(parts) > 2 else None
            status_map = {
                "A": DiffStatus.ADDED,
                "M": DiffStatus.MODIFIED,
                "D": DiffStatus.DELETED,
                "R": DiffStatus.RENAMED,
            }
            status = status_map.get(status_code[0], DiffStatus.MODIFIED)
            entries.append(
                DiffEntry(
                    path=new_path,
                    status=status,
                    old_path=old_path,
                    new_path=new_path,
                )
            )
        return entries


__all__ = [
    "CommitResult",
    "ConflictDiff",
    "GitSync",
    "GitSyncResult",
    "PrCreationResult",
    "NULL_SHA",
]
