"""DB 故障降级模式：内存 LRU 缓存 + 模块范围 BM25。

对应技术方案 7.5 + 缺陷 3.1 降级可用性：
- 向量库 / PG 不可达时进入降级路径
- 强制 module_path（未传返回 503）
- 带 module_path 须 2 秒内返回 degraded:true
- LRU 缓存 git show 结果，避免每次回 git
- 模块 BM25：基于 git ls_tree 遍历模块下资产 + BM25 排序

LRU 缓存设计：
- key = (commit_sha, git_path)
- 容量可配（默认 256 条，单资产平均 4KB → 1MB 内存上限）
- 命中时 move_to_end；超出容量 popitem(last=False)
- 缓存未命中时调用 git_provider.show 写入

模块 BM25 构建：
- 通过 git_provider.ls_tree(commit, module_path) 递归枚举资产文件
- 对每个文件调用 git_provider.show 读取内容（命中 LRU 则直接用缓存）
- 构建 BM25Index，对 query 打分
- 整个模块 BM25 索引可缓存（key=commit+module_path），TTL 内复用
"""

from __future__ import annotations

import logging
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable

from server.recall.bm25 import BM25Index

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LRU 缓存
# ---------------------------------------------------------------------------


class LRUCache:
    """线程安全的 LRU 缓存，键值通用。

    用 OrderedDict 实现 move_to_end 语义。线程安全用 RLock 保护，
    避免降级路径并发读取时损坏内部结构。
    """

    def __init__(self, capacity: int = 256) -> None:
        if capacity <= 0:
            raise ValueError("LRU 容量必须 > 0")
        self._capacity = capacity
        self._store: OrderedDict[Any, Any] = OrderedDict()
        self._lock = threading.RLock()

    def __len__(self) -> int:
        with self._lock:
            return len(self._store)

    def get(self, key: Any) -> Any | None:
        """读取缓存，命中时 move_to_end。未命中返回 None。"""
        with self._lock:
            if key not in self._store:
                return None
            self._store.move_to_end(key)
            return self._store[key]

    def put(self, key: Any, value: Any) -> None:
        """写入缓存，超出容量淘汰最久未访问项。"""
        with self._lock:
            if key in self._store:
                self._store.move_to_end(key)
                self._store[key] = value
                return
            self._store[key] = value
            if len(self._store) > self._capacity:
                self._store.popitem(last=False)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


# ---------------------------------------------------------------------------
# 模块 BM25 缓存条目
# ---------------------------------------------------------------------------


@dataclass
class ModuleBM25Entry:
    """单个模块在某 commit 下的 BM25 索引缓存条目。"""

    commit_sha: str
    module_path: str
    index: BM25Index
    # 资产 id → (git_path, title_hint)，用于回显
    asset_meta: dict[str, tuple[str, str]] = field(default_factory=dict)
    built_at: float = field(default_factory=time.time)


class DegradedRecaller:
    """降级模式召回器：LRU + 模块 BM25。

    用法：
        recaller = DegradedRecaller(git_provider=git, lru_capacity=256,
                                     module_cache_ttl=60.0)
        items = recaller.recall_in_module(
            commit_sha=sha, module_path="modules/backend",
            query="lint 规则", top_k=10,
        )
    """

    def __init__(
        self,
        *,
        git_provider: Any,
        lru_capacity: int = 256,
        module_cache_ttl: float = 60.0,
        module_cache_size: int = 32,
    ) -> None:
        self._git = git_provider
        self._content_lru = LRUCache(capacity=lru_capacity)
        # 模块 BM25 索引缓存：key=(commit_sha, module_path) → ModuleBM25Entry
        self._module_index_cache: OrderedDict[tuple[str, str], ModuleBM25Entry] = (
            OrderedDict()
        )
        self._module_cache_ttl = module_cache_ttl
        self._module_cache_size = module_cache_size
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # 公开方法
    # ------------------------------------------------------------------

    def recall_in_module(
        self,
        *,
        commit_sha: str,
        module_path: str,
        query: str,
        top_k: int = 10,
    ) -> list[dict[str, Any]]:
        """降级模式召回：模块范围 BM25 检索。

        返回 [{asset_id, git_path, content, score}, ...] 按 score 降序。
        """
        if not module_path:
            # 强制 module_path：未传由调用方返回 503，本方法不处理
            raise ValueError("降级模式必须传 module_path")
        if not commit_sha:
            raise ValueError("降级模式需要 commit_sha 作为读取基准")

        entry = self._get_or_build_module_index(commit_sha, module_path)
        if entry is None or len(entry.index) == 0:
            return []

        scores = entry.index.score(query)
        results: list[dict[str, Any]] = []
        for asset_id, score in scores[:top_k]:
            meta = entry.asset_meta.get(asset_id, ("", ""))
            git_path, title_hint = meta
            # 内容从 LRU 取（构建索引时已缓存）
            content = self._content_lru.get((commit_sha, git_path)) or ""
            results.append(
                {
                    "asset_id": asset_id,
                    "git_path": git_path,
                    "title": title_hint,
                    "content": content,
                    "relevance_score": float(score),
                }
            )
        return results

    def read_asset_content(
        self,
        *,
        commit_sha: str,
        git_path: str,
    ) -> str:
        """降级模式读取单资产内容（带 LRU 缓存）。"""
        if not commit_sha or not git_path:
            raise ValueError("read_asset_content 需要 commit_sha 与 git_path")
        key = (commit_sha, git_path)
        cached = self._content_lru.get(key)
        if cached is not None:
            return cached
        # 缓存未命中 → 调用 git show
        content = self._git.show(commit_sha, git_path)
        self._content_lru.put(key, content)
        return content

    def invalidate(self) -> None:
        """清空全部缓存（恢复后调用，强制下次重建）。"""
        self._content_lru.clear()
        with self._lock:
            self._module_index_cache.clear()

    # ------------------------------------------------------------------
    # 内部：模块 BM25 索引构建
    # ------------------------------------------------------------------

    def _get_or_build_module_index(
        self, commit_sha: str, module_path: str
    ) -> ModuleBM25Entry | None:
        """获取或构建模块 BM25 索引。TTL 内复用。"""
        key = (commit_sha, module_path)
        now = time.time()
        with self._lock:
            entry = self._module_index_cache.get(key)
            if entry is not None and (now - entry.built_at) < self._module_cache_ttl:
                # 命中缓存，move_to_end
                self._module_index_cache.move_to_end(key)
                return entry

        # 缓存未命中或过期 → 重建
        entry = self._build_module_index(commit_sha, module_path)
        if entry is None:
            return None
        with self._lock:
            self._module_index_cache[key] = entry
            self._module_index_cache.move_to_end(key)
            while len(self._module_index_cache) > self._module_cache_size:
                self._module_index_cache.popitem(last=False)
        return entry

    def _build_module_index(
        self, commit_sha: str, module_path: str
    ) -> ModuleBM25Entry | None:
        """构建模块 BM25 索引：ls_tree 枚举 → 读取内容 → 构建 BM25。"""
        index = BM25Index()
        asset_meta: dict[str, tuple[str, str]] = {}
        try:
            # 递归枚举模块下文件
            files = self._enumerate_files(commit_sha, module_path)
        except Exception:
            logger.exception(
                "降级模式 ls_tree 失败 commit=%s module=%s", commit_sha, module_path
            )
            return None

        for path, _sha in files:
            # 仅处理 markdown / yaml / json 等文本资产
            if not _is_text_asset(path):
                continue
            try:
                content = self.read_asset_content(commit_sha=commit_sha, git_path=path)
            except Exception:
                logger.exception("降级模式 git show 失败 path=%s", path)
                continue
            # 资产 id 用 path 推导（降级模式下没有 DB 主键，path 唯一）
            asset_id = _path_to_asset_id(path)
            title = _extract_title(content, path)
            index.add(asset_id, content)
            asset_meta[asset_id] = (path, title)

        return ModuleBM25Entry(
            commit_sha=commit_sha,
            module_path=module_path,
            index=index,
            asset_meta=asset_meta,
        )

    def _enumerate_files(
        self, commit_sha: str, root_path: str, max_depth: int = 5
    ) -> list[tuple[str, str]]:
        """递归枚举模块下全部文件，返回 [(path, sha), ...]。"""
        result: list[tuple[str, str]] = []
        # 用栈避免深度递归
        stack: list[tuple[str, int]] = [(root_path, 0)]
        while stack:
            current, depth = stack.pop()
            try:
                entries = self._git.ls_tree(commit_sha, current)
            except Exception:
                logger.debug(
                    "降级 ls_tree 子路径失败 commit=%s path=%s", commit_sha, current
                )
                continue
            for entry in entries:
                # TreeEntry.path 是相对当前 ls_tree root 的子路径名
                # 这里组装完整路径
                if current and not entry.path.startswith(current):
                    full_path = f"{current}/{entry.path}" if current else entry.path
                else:
                    full_path = entry.path
                # entry.type 为 TreeEntryType 枚举
                from server.common.models import TreeEntryType

                if entry.type == TreeEntryType.TREE:
                    if depth + 1 <= max_depth:
                        stack.append((full_path, depth + 1))
                else:
                    result.append((full_path, entry.sha))
        return result


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


_TEXT_ASSET_EXTENSIONS = (".md", ".yaml", ".yml", ".json", ".txt", ".py", ".js", ".ts")


def _is_text_asset(path: str) -> bool:
    """判断路径是否为文本资产（降级模式只索引文本文件）。"""
    lowered = path.lower()
    return any(lowered.endswith(ext) for ext in _TEXT_ASSET_EXTENSIONS)


def _path_to_asset_id(path: str) -> str:
    """降级模式下用 path 作为 asset_id（无 DB 主键）。"""
    # 去除扩展名 + 替换路径分隔符为短横线，生成稳定 id
    import hashlib

    return "degraded-" + hashlib.sha1(path.encode("utf-8")).hexdigest()[:12]


def _extract_title(content: str, path: str) -> str:
    """从内容首行 H1 或文件名提取标题。"""
    if content:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
            if line and not line.startswith("---"):
                return line[:60]
    # 兜底用文件名
    return path.rsplit("/", 1)[-1]


__all__ = [
    "DegradedRecaller",
    "LRUCache",
    "ModuleBM25Entry",
]
