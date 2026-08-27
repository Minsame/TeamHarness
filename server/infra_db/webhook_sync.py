"""webhook 同步处理：读 INDEX.md 增量扫描 + embedding 计算。

对应 SubTask 2.3 + 缺陷 1.3 webhook 补偿：
- 接收 WebhookEvent / commit_sha → 触发增量同步
- 通过 GitProvider.diff 找变更文件（不全仓库扫描）
- 解析变更的 INDEX.md → 写 module_stats（CountsChecker）
- 解析变更的资产文件 (.md) → 写 asset_index + 投递 outbox
- 删除的文件 → 标记 asset_index.status=deleted + 投递 delete outbox
- commit SHA 幂等：last_synced_commit == commit_sha 则跳过

依赖：
- GitProvider（Agent 1 提供）：fetch / show / diff
- INDEXManager（Agent 1 提供）：parse_frontmatter / parse_index_md
- AssetIndex（本 Agent）：upsert / delete
- CountsChecker（本 Agent）：counts 校验
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

import yaml
from sqlalchemy import select

from server.common.models import Asset as AssetVO, AssetType, Scope
from server.infra_git.index_manager import (
    INDEX_FILENAME,
    parse_frontmatter,
    parse_index_md,
)
from server.infra_git.git_provider import GitProvider
from server.infra_db.asset_index import AssetIndex
from server.infra_db.counts_check import CountsChecker
from server.infra_db.db import Database
from server.infra_db.models import IndexSyncState

logger = logging.getLogger(__name__)


# 资产目录与类型映射（与 index_manager.ASSET_DIRS 对齐）
ASSET_DIR_TO_TYPE: dict[str, AssetType] = {
    "rules": AssetType.RULE,
    "memory": AssetType.MEMORY,
    "skills": AssetType.SKILL,
    "tools": AssetType.TOOL,
    "prompts": AssetType.PROMPT,
}


@dataclass
class SyncResult:
    """单次同步结果。"""

    commit_sha: str
    previous_commit: str = ""
    assets_upserted: int = 0
    assets_deleted: int = 0
    indexes_updated: int = 0
    counts_mismatches: int = 0
    skipped: bool = False
    skip_reason: str = ""
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.skipped


class WebhookSyncHandler:
    """webhook 同步处理器（增量扫描）。

    用法：
        handler = WebhookSyncHandler(db, git_provider, asset_index, counts_checker, repo_root=".")
        result = handler.sync_commit(commit_sha)
    """

    EMPTY_COMMIT = "0" * 40  # git 初始 before/删除分支占位

    def __init__(
        self,
        database: Database,
        git_provider: GitProvider,
        asset_index: AssetIndex,
        counts_checker: CountsChecker,
        *,
        repo_root: str = "",
    ) -> None:
        self._db = database
        self._git = git_provider
        self._asset_index = asset_index
        self._counts_checker = counts_checker
        self._repo_root = repo_root

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def sync_commit(self, commit_sha: str) -> SyncResult:
        """同步指定 commit 的变更到 DB 索引层。

        幂等：commit_sha == last_synced_commit 直接跳过。
        """
        result = SyncResult(commit_sha=commit_sha)
        # 同步前 fetch 远端，确保 commit_sha 可见
        try:
            self._git.fetch(self._repo_root)
        except Exception as exc:
            logger.warning("git fetch 失败（继续尝试用本地引用）：%s", exc)

        with self._db.session() as sess:
            state = sess.get(IndexSyncState, "singleton")
            if state is None:
                state = IndexSyncState(
                    id="singleton",
                    last_synced_commit="",
                    status="ok",
                    lag_periods=0,
                )
                sess.add(state)
            previous = state.last_synced_commit
            result.previous_commit = previous

            # 幂等：同一 commit 不重复处理
            if previous == commit_sha and previous:
                result.skipped = True
                result.skip_reason = f"commit {commit_sha} 已同步过"
                return result

            # 执行增量同步（或全量重建）
            try:
                if not previous:
                    self._full_rebuild(sess, commit_sha, result)
                else:
                    self._incremental_sync(sess, previous, commit_sha, result)
                # 同步成功 → 更新水位
                state.last_synced_commit = commit_sha
                state.last_synced_at = datetime.now(timezone.utc)
                state.status = "ok"
                state.lag_periods = 0
                state.last_error = None
            except Exception as exc:
                state.status = "error"
                state.last_error = f"{type(exc).__name__}: {exc}"
                result.errors.append(f"{type(exc).__name__}: {exc}")
                logger.exception("同步失败 commit=%s", commit_sha)
                raise
        return result

    # ------------------------------------------------------------------
    # 全量重建（冷启动 / DB 重建）
    # ------------------------------------------------------------------

    def _full_rebuild(
        self, sess, commit_sha: str, result: SyncResult
    ) -> None:
        """全量重建：从根 INDEX.md 递归扫描全部资产。

        对应技术方案 7.5 "DB 故障降级恢复后从 last_synced_commit 增量补同步"。
        顺序：先扫描资产文件（写 asset_index），后处理 INDEX.md（counts 校验），
        保证 counts 校验时 actual 已反映本次扫描结果。
        """
        logger.info("全量重建索引 commit=%s", commit_sha)
        # 1. 扫描根资产目录（rules/ memory/ skills/ tools/ prompts/）
        for dir_name, asset_type in ASSET_DIR_TO_TYPE.items():
            entries = self._safe_ls_tree(commit_sha, dir_name)
            for entry in entries:
                # 期望 entry.path 形如 rules/xxx.md
                if entry.path.endswith(".md"):
                    content = self._safe_show(commit_sha, entry.path)
                    if content is not None:
                        self._upsert_asset_from_content(
                            sess,
                            entry.path,
                            content,
                            commit_sha,
                            asset_type,
                            result,
                        )

        # 2. 扫描 modules/<module>/ 子目录（递归，含资产 + 子模块 INDEX.md）
        modules_entries = self._safe_ls_tree(commit_sha, "modules")
        for mod_entry in modules_entries:
            # 递归扫描模块
            self._scan_module_recursive(sess, commit_sha, mod_entry.path, result)

        # 3. 最后处理根 INDEX.md（此时根资产已写入，counts actual 才准确）
        root_index_content = self._safe_show(commit_sha, INDEX_FILENAME)
        if root_index_content:
            self._process_index_md(sess, INDEX_FILENAME, root_index_content, commit_sha, result)

    def _scan_module_recursive(
        self, sess, commit_sha: str, module_path: str, result: SyncResult, depth: int = 0
    ) -> None:
        """递归扫描 modules/<module>/ 子目录。

        顺序：先资产 → 子模块 → 最后模块 INDEX.md（保证 counts actual 准确）。
        """
        if depth > 8:  # 防御性深度限制
            return
        # 1. 资产目录
        for dir_name, asset_type in ASSET_DIR_TO_TYPE.items():
            entries = self._safe_ls_tree(commit_sha, f"{module_path}/{dir_name}")
            for entry in entries:
                if entry.path.endswith(".md"):
                    content = self._safe_show(commit_sha, entry.path)
                    if content is not None:
                        self._upsert_asset_from_content(
                            sess,
                            entry.path,
                            content,
                            commit_sha,
                            asset_type,
                            result,
                        )
        # 2. 子模块（modules/<m>/submodules/<sub>/）
        sub_entries = self._safe_ls_tree(commit_sha, f"{module_path}/submodules")
        for sub in sub_entries:
            self._scan_module_recursive(sess, commit_sha, sub.path, result, depth + 1)
        # 3. 最后处理模块 INDEX.md
        idx_path = f"{module_path}/{INDEX_FILENAME}"
        idx_content = self._safe_show(commit_sha, idx_path)
        if idx_content:
            self._process_index_md(sess, idx_path, idx_content, commit_sha, result)

    # ------------------------------------------------------------------
    # 增量同步
    # ------------------------------------------------------------------

    def _incremental_sync(
        self, sess, previous: str, commit_sha: str, result: SyncResult
    ) -> None:
        """增量同步：基于 git diff 处理变更文件。

        不全仓库扫描，只处理 previous → commit_sha 之间的变更。
        """
        if commit_sha == self.EMPTY_COMMIT:
            result.skip_reason = "after commit 为空（删除分支等），跳过"
            result.skipped = True
            return
        logger.info("增量同步 %s → %s", previous[:8], commit_sha[:8])
        diff_entries = self._git.diff(previous, commit_sha)
        for entry in diff_entries:
            path = entry.path
            try:
                if path.endswith(INDEX_FILENAME):
                    self._handle_index_diff(sess, entry, commit_sha, result)
                elif self._is_asset_path(path):
                    self._handle_asset_diff(sess, entry, commit_sha, result)
                # 其他文件（如 .teamharness/*.yaml）不触发 DB 同步
            except Exception as exc:
                logger.exception("处理变更文件失败 path=%s", path)
                result.errors.append(f"path={path} error={exc}")

    @staticmethod
    def _is_asset_path(path: str) -> bool:
        """判断路径是否为资产文件（rules/memory/skills/tools/prompts 下的 .md）。"""
        parts = path.split("/")
        if len(parts) < 2:
            return False
        if parts[0] not in ASSET_DIR_TO_TYPE and not (
            parts[0] == "modules" and len(parts) > 2 and parts[2] in ASSET_DIR_TO_TYPE
        ):
            return False
        return path.endswith(".md")

    def _handle_index_diff(
        self, sess, diff_entry, commit_sha: str, result: SyncResult
    ) -> None:
        """处理 INDEX.md 变更：解析 counts → CountsChecker。"""
        if diff_entry.status.value == "deleted":
            # INDEX.md 被删除 → 模块被移除，跳过（资产删除由对应文件 diff 处理）
            return
        content = self._safe_show(commit_sha, diff_entry.path)
        if content is None:
            return
        self._process_index_md(sess, diff_entry.path, content, commit_sha, result)

    def _handle_asset_diff(
        self, sess, diff_entry, commit_sha: str, result: SyncResult
    ) -> None:
        """处理资产文件变更。"""
        path = diff_entry.path
        # 推断资产类型
        asset_type = self._infer_asset_type(path)
        if asset_type is None:
            return

        if diff_entry.status.value == "deleted":
            # 资产删除 → 标记 asset_index.status=deleted + 投递 delete outbox
            asset_id = self._derive_asset_id_from_path(path)
            self._asset_index.delete(asset_id, git_commit=commit_sha, soft_delete=True)
            result.assets_deleted += 1
            return

        content = self._safe_show(commit_sha, path)
        if content is None:
            return
        self._upsert_asset_from_content(sess, path, content, commit_sha, asset_type, result)

    @staticmethod
    def _infer_asset_type(path: str) -> AssetType | None:
        parts = path.split("/")
        if parts[0] in ASSET_DIR_TO_TYPE:
            return ASSET_DIR_TO_TYPE[parts[0]]
        if len(parts) > 2 and parts[0] == "modules" and parts[2] in ASSET_DIR_TO_TYPE:
            return ASSET_DIR_TO_TYPE[parts[2]]
        return None

    @staticmethod
    def _derive_asset_id_from_path(path: str) -> str:
        """从路径推断 asset_id（frontmatter id 优先；缺失时用去扩展名的文件名）。"""
        # 简化：用文件名去 .md 后缀作为 id（实际 frontmatter id 由内容解析时覆盖）
        name = path.rsplit("/", 1)[-1]
        return name[:-3] if name.endswith(".md") else name

    # ------------------------------------------------------------------
    # 文件解析
    # ------------------------------------------------------------------

    def _process_index_md(
        self, sess, path: str, content: str, commit_sha: str, result: SyncResult
    ) -> None:
        """解析 INDEX.md，提取 counts 写入 module_stats。"""
        try:
            doc = parse_index_md(content)
        except Exception as exc:
            logger.warning("INDEX.md 解析失败 path=%s err=%s", path, exc)
            return
        module_path = self._derive_module_path_from_index_path(path)
        declared = {
            module_path: {
                "assets": int(doc.counts.get("assets", 0)),
                "submodules": int(doc.counts.get("submodules", 0)),
            }
        }
        counts_result = self._counts_checker.check_and_persist(
            declared, commit_sha=commit_sha
        )
        result.counts_mismatches += len(counts_result.mismatches)
        result.indexes_updated += 1

    @staticmethod
    def _derive_module_path_from_index_path(path: str) -> str:
        """从 INDEX.md 路径推断 module_path。

        - "INDEX.md" → ""（项目根）
        - "modules/backend/INDEX.md" → "modules/backend"
        - "modules/backend/submodules/auth/INDEX.md" → "modules/backend/submodules/auth"
        """
        if path == INDEX_FILENAME:
            return ""
        return path[: -len("/" + INDEX_FILENAME)]

    def _upsert_asset_from_content(
        self,
        sess,
        path: str,
        content: str,
        commit_sha: str,
        asset_type: AssetType,
        result: SyncResult,
    ) -> None:
        """解析资产文件 frontmatter → 写 asset_index。"""
        fm, body = parse_frontmatter(content)
        asset_id = str(fm.get("id") or self._derive_asset_id_from_path(path))
        scope_raw = str(fm.get("scope", "team")).lower()
        try:
            scope = Scope(scope_raw)
        except ValueError:
            scope = Scope.TEAM
        module_path = self._derive_module_path_from_asset_path(path)
        category = fm.get("category")
        tags = fm.get("tags") or []
        if not isinstance(tags, list):
            tags = [str(tags)]
        related = fm.get("related_to") or []
        if not isinstance(related, list):
            related = [str(related)]

        asset_vo = AssetVO(
            id=asset_id,
            type=asset_type,
            owner=str(fm.get("owner", "")),
            scope=scope,
            content=body,
            content_hash=fm.get("content_hash"),
            tags=[str(t) for t in tags],
            version=str(fm.get("version", "0.0.1")),
            module_path=module_path,
            category=str(category) if category else None,
            related_to=[str(r) for r in related],
            content_file_ref=path,
            schema_version=int(fm.get("schema_version", 1)),
        )
        self._asset_index.upsert(
            asset_vo,
            git_commit=commit_sha,
            content_snapshot=body,
        )
        result.assets_upserted += 1

    @staticmethod
    def _derive_module_path_from_asset_path(path: str) -> str:
        """从资产文件路径推断 module_path。

        - "rules/x.md" → ""（项目根）
        - "modules/backend/rules/x.md" → "modules/backend"
        - "modules/backend/submodules/auth/rules/x.md" → "modules/backend/submodules/auth"
        """
        parts = path.split("/")
        if parts[0] != "modules":
            return ""
        # 找到 ASSET_DIR_TO_TYPE 中第一个匹配的 part，去掉它及之后的部分
        for i, p in enumerate(parts):
            if p in ASSET_DIR_TO_TYPE:
                return "/".join(parts[:i]) if i > 0 else ""
        return ""

    # ------------------------------------------------------------------
    # GitProvider 包装（容错）
    # ------------------------------------------------------------------

    def _safe_show(self, commit_sha: str, path: str) -> str | None:
        try:
            return self._git.show(commit_sha, path)
        except Exception as exc:
            logger.debug("git show 失败 commit=%s path=%s err=%s", commit_sha, path, exc)
            return None

    def _safe_ls_tree(self, commit_sha: str, path: str):
        try:
            return self._git.ls_tree(commit_sha, path)
        except Exception as exc:
            logger.debug("git ls_tree 失败 commit=%s path=%s err=%s", commit_sha, path, exc)
            return []


__all__ = [
    "SyncResult",
    "WebhookSyncHandler",
]
