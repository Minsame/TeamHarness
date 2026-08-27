"""RecallService — 召回服务核心（Agent 4 对外契约 API）。

对应技术方案 3.2b + 7.5 + 缺陷 3.1/3.2/5.4。对外三契约方法：
- recall_list(agent_id, query?, module_path?, consistency?, ...) → RecallListResponse
- recall_read(agent_id, asset_id, consistency?) → RecallReadResponse | GoneResponse
- get_sync_status() → SyncStatusResponse

核心设计：
1. **索引下钻**：module_path 通过 AssetFilter.module_path_prefix 前缀匹配（递归子模块），
   缩小候选集，避免全量向量检索。
2. **权限过滤**（缺陷 3.2 装配失效双重过滤）：JOIN agent_binding + asset_index，
   WHERE agent_binding.agent_id=? AND agent_binding.enabled=true
     AND asset_index.status='active'
   保证 webhook 删除资产时 agent_binding.enabled=false 同事务级联后召回不返回。
3. **精排**（向量 + BM25 + RRF）：
   - 无 query → 返回装配清单（fixed 绑定的资产）
   - 有 query → 向量检索（VectorStore.search）+ BM25（content_snapshot）+ RRF 融合
4. **consistency**：
   - eventual（默认）：as_of_commit = index_sync_state.last_synced_commit
   - strict：先 git fetch 实时拉取，as_of_commit = head_resolver() 返回的 HEAD
5. **DB 故障降级**（缺陷 3.1）：
   - 检测 PG / 向量库不可达 → 进入降级路径
   - 降级路径强制 module_path，未传返回 503
   - 用 DegradedRecaller（LRU + 模块 BM25），2 秒内返回 degraded=true
6. **离线降级**：recall/read strict 模式下若 git fetch 失败 → 从本地 working copy 读取
7. **410 Gone + 替代建议**：recall/read 命中 status=deleted 资产时返回 410，
   附带同类目 / 同模块下的 active 替代资产 top N
8. **recall_log 写入**：每次召回命中资产写一条 recall_log（含 trace_id），
   作为二级提炼晋升门禁"被召回次数"数据源
9. **OpenTelemetry trace_id**：每请求生成 trace_id（contextvars），写 recall_log，
   透传响应头
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from server.common.models import Scope, TreeEntryType
from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import (
    AgentBinding,
    AssetIndex as AssetIndexRow,
    RecallLog,
)
from server.infra_db.sync import SyncService
from server.infra_db.vectorstore import VectorStore
from server.infra_git.git_provider import GitProvider
from server.infra_git.restricted import RestrictedReader
from server.recall.bm25 import BM25Index
from server.recall.degraded import DegradedRecaller
from server.recall.tracing import ensure_trace_id, get_trace_id

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 响应数据类（对外契约）
# ---------------------------------------------------------------------------


@dataclass
class RecallItem:
    """召回单条资产摘要。"""

    asset_id: str
    type: str
    title: str
    tags: list[str]
    relevance_score: float
    git_path: str
    module_path: str
    scope: str = "team"
    category: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class RecallListResponse:
    """recall_list 响应体。"""

    items: list[RecallItem]
    as_of_commit: str
    sync_lag_seconds: float
    degraded: bool
    trace_id: str
    # 降级原因（仅 degraded=true 时有值）
    degraded_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "items": [item.to_dict() for item in self.items],
            "as_of_commit": self.as_of_commit,
            "sync_lag_seconds": self.sync_lag_seconds,
            "degraded": self.degraded,
            "trace_id": self.trace_id,
            "degraded_reason": self.degraded_reason,
        }


@dataclass
class RecallReadResponse:
    """recall_read 响应体。"""

    content: str
    frontmatter: dict[str, Any]
    asset_id: str
    git_path: str
    as_of_commit: str
    degraded: bool
    trace_id: str
    # 离线降级标记（从本地 working copy 读取）
    from_local_copy: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AlternativeSuggestion:
    """410 Gone 替代建议项。"""

    asset_id: str
    title: str
    git_path: str
    module_path: str
    category: str | None = None


@dataclass
class GoneResponse:
    """recall_read 命中已删除资产的 410 响应。"""

    asset_id: str
    message: str
    alternatives: list[AlternativeSuggestion] = field(default_factory=list)
    trace_id: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "asset_id": self.asset_id,
            "message": self.message,
            "alternatives": [asdict(a) for a in self.alternatives],
            "trace_id": self.trace_id,
        }


@dataclass
class SyncStatusResponse:
    """GET /v1/sync/status 响应体。"""

    last_synced_commit: str
    lag_seconds: float
    sync_source: str  # webhook / reconciliation / offline
    last_synced_at: str | None = None
    status: str = "ok"  # ok / syncing / error / lagging
    last_error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ---------------------------------------------------------------------------
# 异常类型
# ---------------------------------------------------------------------------


class DegradedModulePathRequiredError(RuntimeError):
    """降级模式必须传 module_path，未传时由 API 层返回 503。"""


class AssetNotFoundError(LookupError):
    """资产不存在（404）。"""


class AssetGoneError(LookupError):
    """资产已删除（410 Gone），携带替代建议。"""

    def __init__(self, asset_id: str, alternatives: list[AlternativeSuggestion]) -> None:
        super().__init__(f"asset {asset_id} gone")
        self.asset_id = asset_id
        self.alternatives = alternatives


class RestrictedAccessDeniedError(PermissionError):
    """restricted 资产访问被拒。"""


# ---------------------------------------------------------------------------
# RecallService
# ---------------------------------------------------------------------------


class RecallService:
    """召回服务（对外契约 API）。

    用法：
        svc = RecallService(
            database=db,
            asset_index=asset_index,
            embedding_service=emb,
            sync_service=sync_svc,
            vector_store=vs,
            git_provider=git,
            repo_root=".",
        )
        resp = svc.recall_list(agent_id="builder-1", query="lint", module_path="modules/backend")
    """

    # 降级模式超时阈值（缺陷 3.1：带 module_path 2 秒内返回）
    DEGRADED_TIMEOUT_SECONDS = 2.0
    # 410 Gone 替代建议数量
    ALTERNATIVES_LIMIT = 3
    # 默认 top_k
    DEFAULT_TOP_K = 10

    def __init__(
        self,
        *,
        database: Database,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        sync_service: SyncService,
        vector_store: VectorStore,
        git_provider: GitProvider,
        repo_root: str = "",
        restricted_reader: RestrictedReader | None = None,
        head_resolver: Callable[[], str] | None = None,
        repo_url: str = "",
        degraded_recaller: DegradedRecaller | None = None,
        offline_root: str | None = None,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._embedding = embedding_service
        self._sync = sync_service
        self._vector_store = vector_store
        self._git = git_provider
        self._repo_root = repo_root
        self._restricted_reader = restricted_reader
        self._head_resolver = head_resolver
        self._repo_url = repo_url
        self._offline_root = offline_root or repo_root
        # 降级模式召回器（延迟初始化：仅在第一次进入降级路径时构建）
        self._degraded = degraded_recaller or DegradedRecaller(git_provider=git_provider)
        # 模块 BM25 索引（用于正常路径的 BM25 通道，按 module_path 缓存）
        self._bm25_cache: dict[str, BM25Index] = {}

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def recall_list(
        self,
        *,
        agent_id: str,
        query: str | None = None,
        module_path: str | None = None,
        consistency: str = "eventual",
        task_type: str | None = None,
        asset_type: str | None = None,
        tags: list[str] | None = None,
        top_k: int | None = None,
        trace_id: str | None = None,
    ) -> RecallListResponse:
        """召回列表：索引下钻 + 权限过滤 + 向量+BM25+RRF 精排。

        - consistency=eventual：基于 last_synced_commit；strict：git fetch + HEAD
        - DB / 向量库不可达 → 进入降级路径（强制 module_path，2 秒内返回 degraded=true）
        - 每条召回结果写 recall_log（含 trace_id）
        """
        # 1. trace_id 透传
        if trace_id:
            from server.recall.tracing import set_trace_id

            set_trace_id(trace_id)
        tid = ensure_trace_id()
        top_k = top_k or self.DEFAULT_TOP_K

        # 2. 解析 consistency + as_of_commit + sync_lag
        as_of_commit, sync_lag, degraded_consistency = self._resolve_consistency(consistency)
        # strict 模式可能进入降级（git fetch 失败）
        degraded = degraded_consistency
        degraded_reason: str | None = None
        if degraded_consistency:
            degraded_reason = "strict 模式 git fetch 失败，回退 eventual"

        # 3. 正常路径：尝试 DB + 向量库
        try:
            items = self._normal_recall(
                agent_id=agent_id,
                query=query,
                module_path=module_path,
                task_type=task_type,
                asset_type=asset_type,
                tags=tags,
                top_k=top_k,
                as_of_commit=as_of_commit,
            )
            # 4. 写 recall_log（每条一次）
            self._write_recall_logs(
                agent_id=agent_id,
                items=items,
                query=query or "",
                trace_id=tid,
            )
            return RecallListResponse(
                items=items,
                as_of_commit=as_of_commit,
                sync_lag_seconds=sync_lag,
                # 合并 strict 模式 fetch 失败的 degraded 标志：
                # 数据虽读到，但 strict 实时性承诺未达成，仍标记 degraded
                degraded=degraded_consistency,
                trace_id=tid,
                degraded_reason=degraded_reason,
            )
        except (DegradedModulePathRequiredError,):
            # 未传 module_path → 直接 503，不进入降级
            raise
        except Exception as exc:
            # DB / 向量库故障 → 降级
            logger.warning(
                "recall_list 正常路径失败，进入降级模式 agent=%s err=%s",
                agent_id,
                exc,
            )
            degraded = True
            degraded_reason = f"正常路径异常：{type(exc).__name__}: {exc}"

        # 5. 降级路径
        if not module_path:
            # 强制 module_path（缺陷 3.1）：未传 → 503
            raise DegradedModulePathRequiredError(
                "向量库不可达且未传 module_path，无法降级召回"
            )

        degraded_items = self._degraded_recall(
            agent_id=agent_id,
            query=query or "",
            module_path=module_path,
            as_of_commit=as_of_commit,
            top_k=top_k,
        )
        # 写 recall_log（降级模式也写，便于治理统计）
        self._write_recall_logs(
            agent_id=agent_id,
            items=degraded_items,
            query=query or "",
            trace_id=tid,
        )
        return RecallListResponse(
            items=degraded_items,
            as_of_commit=as_of_commit,
            sync_lag_seconds=sync_lag,
            degraded=True,
            trace_id=tid,
            degraded_reason=degraded_reason,
        )

    def recall_read(
        self,
        *,
        agent_id: str,
        asset_id: str,
        consistency: str = "eventual",
        trace_id: str | None = None,
    ) -> RecallReadResponse:
        """读取资产完整内容（从 git）。

        - 已删除资产 → 抛 AssetGoneError（含替代建议），由 API 层转 410
        - restricted 资产 → RestrictedReader 鉴权网关
        - strict 模式 → git fetch + HEAD commit；eventual → last_synced_commit
        - strict 模式 git fetch 失败 → 离线降级（本地 working copy）
        """
        if trace_id:
            from server.recall.tracing import set_trace_id

            set_trace_id(trace_id)
        tid = ensure_trace_id()

        # 1. 查资产完整行（含 scope / category，AssetStatus 数据类字段不全）
        asset_row = self._fetch_asset_row(asset_id)
        if asset_row is None:
            raise AssetNotFoundError(f"资产不存在：{asset_id}")

        # 提取关键字段（session 即将关闭，先取出）
        row_status = asset_row.status
        row_git_path = asset_row.git_path
        row_git_commit = asset_row.git_commit
        row_module_path = asset_row.module_path or ""
        row_scope = asset_row.scope or "team"
        row_category = asset_row.category

        # 2. 已删除 → 410 Gone + 替代建议
        if row_status == "deleted":
            alts = self._find_alternatives(
                asset_id=asset_id,
                module_path=row_module_path,
                category=row_category,
                limit=self.ALTERNATIVES_LIMIT,
            )
            raise AssetGoneError(asset_id=asset_id, alternatives=alts)

        # 3. restricted 鉴权网关
        from_local_copy = False
        if row_scope == Scope.RESTRICTED.value:
            self._check_restricted_access(agent_id=agent_id, asset_id=asset_id)

        # 4. 解析 consistency + commit
        commit_to_read = row_git_commit
        degraded = False
        if consistency == "strict":
            try:
                if self._repo_url:
                    self._git.fetch(self._repo_url)
                if self._head_resolver is not None:
                    commit_to_read = self._head_resolver()
                else:
                    # 无 resolver → 回退到 last_synced_commit
                    commit_to_read = self._resolve_consistency("eventual")[0]
            except Exception as exc:
                logger.warning(
                    "strict 模式 git fetch 失败，离线降级到本地 working copy asset=%s err=%s",
                    asset_id,
                    exc,
                )
                content = self._read_from_local_copy(row_git_path)
                frontmatter = _parse_frontmatter(content)
                self._write_recall_log_single(
                    agent_id=agent_id,
                    asset_id=asset_id,
                    module_path=row_module_path,
                    query="",
                    score=None,
                    trace_id=tid,
                )
                return RecallReadResponse(
                    content=content,
                    frontmatter=frontmatter,
                    asset_id=asset_id,
                    git_path=row_git_path,
                    as_of_commit="",
                    degraded=True,
                    trace_id=tid,
                    from_local_copy=True,
                )
        else:
            # eventual：用 last_synced_commit
            commit_to_read, _, _ = self._resolve_consistency("eventual")
            if not commit_to_read:
                # last_synced_commit 为空 → 回退到资产记录的 git_commit
                commit_to_read = row_git_commit

        # 5. 从 git 读取内容
        try:
            content = self._git.show(commit_to_read, row_git_path)
        except Exception as exc:
            # git 读取失败 → 离线降级
            logger.warning(
                "recall_read git show 失败，离线降级 asset=%s commit=%s err=%s",
                asset_id,
                commit_to_read,
                exc,
            )
            degraded = True
            from_local_copy = True
            content = self._read_from_local_copy(row_git_path)

        frontmatter = _parse_frontmatter(content)

        # 6. 写 recall_log（read 事件）
        self._write_recall_log_single(
            agent_id=agent_id,
            asset_id=asset_id,
            module_path=row_module_path,
            query="",
            score=None,
            trace_id=tid,
        )

        return RecallReadResponse(
            content=content,
            frontmatter=frontmatter,
            asset_id=asset_id,
            git_path=row_git_path,
            as_of_commit=commit_to_read,
            degraded=degraded,
            trace_id=tid,
            from_local_copy=from_local_copy,
        )

    def _fetch_asset_row(self, asset_id: str) -> AssetIndexRow | None:
        """查询资产完整行（含 scope/category/status 等全部字段）。"""
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset_id)
            if row is None:
                return None
            # expire_on_commit=False，session 关闭后字段仍可访问
            return row

    def get_sync_status(self) -> SyncStatusResponse:
        """查询同步滞后状态。"""
        try:
            status = self._sync.get_sync_status()
        except Exception as exc:
            logger.warning("get_sync_status 失败，返回降级响应 err=%s", exc)
            return SyncStatusResponse(
                last_synced_commit="",
                lag_seconds=-1.0,
                sync_source="unknown",
                status="error",
                last_error=str(exc),
            )

        lag_seconds = self._compute_lag_seconds(status.last_synced_at)
        # 推断 sync_source：webhook 触发的同步会更新 last_synced_at；
        # reconciliation 补同步的 lag_periods > 0；这里用启发式
        sync_source = "webhook"
        if status.lag_periods > 0:
            sync_source = "reconciliation"

        return SyncStatusResponse(
            last_synced_commit=status.last_synced_commit,
            lag_seconds=lag_seconds,
            sync_source=sync_source,
            last_synced_at=status.last_synced_at.isoformat() if status.last_synced_at else None,
            status=status.status,
            last_error=status.last_error,
        )

    # ------------------------------------------------------------------
    # 内部：正常路径召回
    # ------------------------------------------------------------------

    def _normal_recall(
        self,
        *,
        agent_id: str,
        query: str | None,
        module_path: str | None,
        task_type: str | None,
        asset_type: str | None,
        tags: list[str] | None,
        top_k: int,
        as_of_commit: str,
    ) -> list[RecallItem]:
        """正常路径召回：索引下钻 + 权限过滤 + 精排。"""

        # 1. 装配失效双重过滤：JOIN agent_binding + asset_index WHERE status='active'
        bound_assets = self._query_bound_assets(
            agent_id=agent_id,
            module_path=module_path,
            asset_type=asset_type,
            tags=tags,
        )
        if not bound_assets:
            return []

        # 2. 索引下钻：module_path 前缀过滤（双重过滤已 JOIN，此处再筛 active）
        # bound_assets 已是 active，直接用
        candidate_rows: dict[str, AssetIndexRow] = {r.id: r for r in bound_assets}
        candidate_ids = set(candidate_rows.keys())

        # 3. 精排
        if not query:
            # 无 query → 返回装配清单（fixed 绑定优先 + on-demand）
            items: list[RecallItem] = []
            for row in bound_assets:
                # 装配清单按 binding_type fixed 优先排序
                items.append(self._row_to_item(row, relevance_score=1.0))
            # fixed 优先：通过额外查询排序
            return self._sort_by_binding_priority(agent_id, items)[:top_k]

        # 4. 有 query → 向量检索 + BM25 + RRF
        # 4.1 向量检索
        vector_hits = self._vector_search(
            query=query,
            candidate_ids=candidate_ids,
            top_k=top_k * 3,  # 多召回用于 RRF 融合
        )
        # 4.2 BM25 检索（候选集内）
        bm25_hits = self._bm25_search(
            query=query,
            candidate_rows=list(candidate_rows.values()),
            top_k=top_k * 3,
        )
        # 4.3 RRF 融合
        fused = self._rrf_fuse(
            vector_hits=vector_hits,
            bm25_hits=bm25_hits,
            top_k=top_k,
        )
        # 4.4 构造 RecallItem
        items = []
        score_map = dict(fused)
        for row in bound_assets:
            if row.id in score_map:
                items.append(self._row_to_item(row, relevance_score=score_map[row.id]))
        # 按融合分降序
        items.sort(key=lambda x: x.relevance_score, reverse=True)
        return items[:top_k]

    def _query_bound_assets(
        self,
        *,
        agent_id: str,
        module_path: str | None,
        asset_type: str | None,
        tags: list[str] | None,
    ) -> list[AssetIndexRow]:
        """装配失效双重过滤：JOIN agent_binding + asset_index WHERE status='active'。

        对应缺陷 3.2：webhook 删除资产时 agent_binding.enabled=false 同事务级联，
        本查询再过滤 asset_index.status='active'，形成双重保险：
        - 即使 agent_binding.enabled 未及时更新，asset_index.status='deleted' 也排除
        - 即使 asset_index.status 误为 active，agent_binding.enabled=false 也排除
        """
        from sqlalchemy import or_

        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .join(AgentBinding, AgentBinding.asset_id == AssetIndexRow.id)
                .where(AgentBinding.agent_id == agent_id)
                .where(AgentBinding.enabled.is_(True))
                .where(AssetIndexRow.status == "active")
            )
            # 索引下钻：module_path 前缀匹配（递归召回子模块）
            if module_path:
                stmt = stmt.where(
                    or_(
                        AssetIndexRow.module_path == module_path,
                        AssetIndexRow.module_path.like(f"{module_path}/%"),
                    )
                )
            if asset_type:
                stmt = stmt.where(AssetIndexRow.type == asset_type)
            if tags:
                # tags JSON 数组，任一命中（OR 语义）
                clauses = [AssetIndexRow.tags.like(f'%"{t}"%') for t in tags]
                stmt = stmt.where(or_(*clauses))
            return list(sess.scalars(stmt))

    def _vector_search(
        self,
        *,
        query: str,
        candidate_ids: set[str],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """向量检索：返回 [(asset_id, score), ...]。"""
        if not candidate_ids:
            return []
        # 计算 query 向量
        emb = self._embedding.embed(query)
        active_version = self._embedding.get_active_version()
        # 向量检索（top_k 扩大，再用候选集过滤）
        hits = self._vector_store.search(
            emb.vector,
            model_version=active_version,
            top_k=top_k,
        )
        result: list[tuple[str, float]] = []
        for hit in hits:
            if hit.asset_id in candidate_ids:
                result.append((hit.asset_id, float(hit.score)))
        return result

    def _bm25_search(
        self,
        *,
        query: str,
        candidate_rows: list[AssetIndexRow],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """BM25 检索：在候选集内构建索引并打分。"""
        if not candidate_rows:
            return []
        index = BM25Index()
        for row in candidate_rows:
            # content_snapshot 是资产内容快照（Agent 2 已写入）
            content = row.content_snapshot or ""
            # 拼接 title / tags 提升召回
            tags_str = row.tags or ""
            index.add(row.id, f"{content} {tags_str}")
        scores = index.score(query)
        return scores[:top_k]

    def _rrf_fuse(
        self,
        *,
        vector_hits: list[tuple[str, float]],
        bm25_hits: list[tuple[str, float]],
        top_k: int,
    ) -> list[tuple[str, float]]:
        """RRF 融合向量 + BM25 结果。

        复用 EmbeddingService.fuse_rrf（已实现 RRF 公式）。
        """
        return EmbeddingService.fuse_rrf(
            {"vector": vector_hits, "bm25": bm25_hits},
            k=60,
            top_k=top_k,
        )

    def _sort_by_binding_priority(
        self, agent_id: str, items: list[RecallItem]
    ) -> list[RecallItem]:
        """按 binding_type 排序：fixed 优先于 on-demand。"""
        # 查询每个资产的 binding_type
        with self._db.session() as sess:
            stmt = (
                select(AgentBinding.asset_id, AgentBinding.binding_type)
                .where(AgentBinding.agent_id == agent_id)
                .where(AgentBinding.enabled.is_(True))
            )
            binding_map: dict[str, str] = {}
            for row in sess.execute(stmt):
                binding_map[row[0]] = row[1]
        # fixed 优先
        return sorted(
            items,
            key=lambda x: (0 if binding_map.get(x.asset_id) == "fixed" else 1, x.asset_id),
        )

    # ------------------------------------------------------------------
    # 内部：降级路径召回
    # ------------------------------------------------------------------

    def _degraded_recall(
        self,
        *,
        agent_id: str,
        query: str,
        module_path: str,
        as_of_commit: str,
        top_k: int,
    ) -> list[RecallItem]:
        """降级模式召回：用 DegradedRecaller（LRU + 模块 BM25）。

        注意：降级模式下不查 agent_binding（DB 可能不可达），
        直接返回模块范围内 BM25 top_k 结果。degraded=true 标记告知客户端。
        """
        # 缺陷 3.1：2 秒内返回
        import time

        start = time.monotonic()
        # commit 用 as_of_commit（strict 模式可能为 HEAD；eventual 为 last_synced_commit）
        commit = as_of_commit
        if not commit:
            # 无 commit → 用 working copy HEAD（Libgit2Provider 支持）
            try:
                if self._head_resolver is not None:
                    commit = self._head_resolver()
            except Exception:
                commit = ""
        if not commit:
            # 仍无 commit → 无法降级
            return []

        results = self._degraded.recall_in_module(
            commit_sha=commit,
            module_path=module_path,
            query=query,
            top_k=top_k,
        )
        # 转为 RecallItem
        items: list[RecallItem] = []
        for r in results:
            items.append(
                RecallItem(
                    asset_id=r["asset_id"],
                    type="rule",  # 降级模式无 type 信息，用 rule 兜底
                    title=r.get("title", ""),
                    tags=[],
                    relevance_score=r.get("relevance_score", 0.0),
                    git_path=r["git_path"],
                    module_path=module_path,
                    scope="team",
                    category=None,
                )
            )
        elapsed = time.monotonic() - start
        if elapsed > self.DEGRADED_TIMEOUT_SECONDS:
            logger.warning(
                "降级召回超时阈值 %ss 实际耗时 %ss module=%s",
                self.DEGRADED_TIMEOUT_SECONDS,
                elapsed,
                module_path,
            )
        return items

    # ------------------------------------------------------------------
    # 内部：consistency 解析
    # ------------------------------------------------------------------

    def _resolve_consistency(self, consistency: str) -> tuple[str, float, bool]:
        """解析 consistency，返回 (as_of_commit, sync_lag_seconds, degraded)。

        - eventual：as_of_commit = last_synced_commit
        - strict：尝试 git fetch；失败则 degraded=true 并回退到 last_synced_commit
        """
        # 获取同步状态
        try:
            sync_status = self._sync.get_sync_status()
        except Exception:
            # 同步状态查询失败 → 视为降级
            return ("", -1.0, True)

        lag_seconds = self._compute_lag_seconds(sync_status.last_synced_at)
        last_commit = sync_status.last_synced_commit

        if consistency != "strict":
            return (last_commit, lag_seconds, False)

        # strict 模式：尝试 git fetch
        try:
            if self._repo_url:
                self._git.fetch(self._repo_url)
            if self._head_resolver is not None:
                head = self._head_resolver()
                return (head, 0.0, False)
            # 无 head_resolver → 无法解析 HEAD，降级
            return (last_commit, lag_seconds, True)
        except Exception as exc:
            logger.warning("strict 模式 git fetch 失败，降级 err=%s", exc)
            return (last_commit, lag_seconds, True)

    def _compute_lag_seconds(self, last_synced_at: datetime | None) -> float:
        """计算同步滞后秒数。last_synced_at 为 None 返回 -1。"""
        if last_synced_at is None:
            return -1.0
        now = datetime.now(timezone.utc)
        # 确保 last_synced_at 带时区
        if last_synced_at.tzinfo is None:
            last_synced_at = last_synced_at.replace(tzinfo=timezone.utc)
        delta = (now - last_synced_at).total_seconds()
        return max(0.0, delta)

    # ------------------------------------------------------------------
    # 内部：410 Gone 替代建议
    # ------------------------------------------------------------------

    def _find_alternatives(
        self,
        *,
        asset_id: str,
        module_path: str,
        category: str | None,
        limit: int = 3,
    ) -> list[AlternativeSuggestion]:
        """查找替代资产：同类目 / 同模块下的 active 资产 top N。"""
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.id != asset_id)
            )
            # 优先同类目
            if category:
                stmt = stmt.where(AssetIndexRow.category == category)
            elif module_path:
                stmt = stmt.where(AssetIndexRow.module_path == module_path)
            stmt = stmt.limit(limit)
            rows = list(sess.scalars(stmt))
            return [
                AlternativeSuggestion(
                    asset_id=r.id,
                    title=_extract_title_from_row(r),
                    git_path=r.git_path,
                    module_path=r.module_path,
                    category=r.category,
                )
                for r in rows
            ]

    # ------------------------------------------------------------------
    # 内部：restricted 鉴权网关
    # ------------------------------------------------------------------

    def _check_restricted_access(
        self,
        *,
        agent_id: str,
        asset_id: str,
    ) -> None:
        """restricted 资产鉴权：必须存在 enabled 的 agent_binding 且 RestrictedReader 可用。

        双重校验：
        1. agent_binding 中存在该 (agent_id, asset_id, enabled=true) 记录
        2. RestrictedReader.is_available()（git-crypt 已解锁 / 独立仓库可达）
        """
        # 1. agent_binding 校验
        with self._db.session() as sess:
            stmt = (
                select(AgentBinding)
                .where(AgentBinding.agent_id == agent_id)
                .where(AgentBinding.asset_id == asset_id)
                .where(AgentBinding.enabled.is_(True))
            )
            binding = sess.scalars(stmt).first()
            if binding is None:
                raise RestrictedAccessDeniedError(
                    f"agent {agent_id} 无权访问 restricted 资产 {asset_id}"
                )

        # 2. RestrictedReader 可用性校验
        if self._restricted_reader is None or not self._restricted_reader.is_available():
            raise RestrictedAccessDeniedError(
                "RestrictedReader 不可用，restricted 资产无法读取"
            )

    # ------------------------------------------------------------------
    # 内部：离线降级（本地 working copy）
    # ------------------------------------------------------------------

    def _read_from_local_copy(self, git_path: str) -> str:
        """从本地 git working copy 读取资产内容。"""
        import os
        from pathlib import Path

        if not self._offline_root:
            raise FileNotFoundError("offline_root 未配置，无法离线读取")
        # 拼接本地路径
        local_path = Path(self._offline_root) / git_path
        if not local_path.is_file():
            raise FileNotFoundError(f"本地 working copy 无此资产：{local_path}")
        return local_path.read_text(encoding="utf-8")

    # ------------------------------------------------------------------
    # 内部：recall_log 写入
    # ------------------------------------------------------------------

    def _write_recall_logs(
        self,
        *,
        agent_id: str,
        items: list[RecallItem],
        query: str,
        trace_id: str,
    ) -> None:
        """批量写 recall_log（每条召回资产一条）。"""
        if not items:
            return
        now = datetime.now(timezone.utc)
        try:
            with self._db.session() as sess:
                for item in items:
                    sess.add(
                        RecallLog(
                            asset_id=item.asset_id,
                            agent_id=agent_id,
                            recalled_at=now,
                            module_path=item.module_path,
                            query=query,
                            relevance_score=item.relevance_score,
                            trace_id=trace_id,
                        )
                    )
        except Exception:
            # recall_log 写入失败不影响召回返回（治理数据降级）
            logger.exception("recall_log 批量写入失败")

    def _write_recall_log_single(
        self,
        *,
        agent_id: str,
        asset_id: str,
        module_path: str,
        query: str,
        score: float | None,
        trace_id: str,
    ) -> None:
        """单条 recall_log 写入（recall/read 用）。"""
        try:
            with self._db.session() as sess:
                sess.add(
                    RecallLog(
                        asset_id=asset_id,
                        agent_id=agent_id,
                        recalled_at=datetime.now(timezone.utc),
                        module_path=module_path,
                        query=query,
                        relevance_score=score,
                        trace_id=trace_id,
                    )
                )
        except Exception:
            logger.exception("recall_log 单条写入失败")

    # ------------------------------------------------------------------
    # 内部：辅助
    # ------------------------------------------------------------------

    def _row_to_item(self, row: AssetIndexRow, *, relevance_score: float) -> RecallItem:
        """AssetIndexRow → RecallItem。"""
        tags = _safe_json_loads_list(row.tags)
        title = _extract_title_from_row(row)
        return RecallItem(
            asset_id=row.id,
            type=row.type,
            title=title,
            tags=tags,
            relevance_score=float(relevance_score),
            git_path=row.git_path,
            module_path=row.module_path,
            scope=row.scope,
            category=row.category,
        )


# ---------------------------------------------------------------------------
# 辅助函数
# ---------------------------------------------------------------------------


def _safe_json_loads_list(s: str | None) -> list[str]:
    """安全解析 JSON 数组字符串，失败返回空列表。"""
    if not s:
        return []
    try:
        val = json.loads(s)
        if isinstance(val, list):
            return [str(x) for x in val]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def _extract_title_from_row(row: AssetIndexRow) -> str:
    """从内容快照首行 H1 或 git_path 推断标题。"""
    content = row.content_snapshot or ""
    return _extract_title_from_content(content, row.git_path)


def _extract_title_from_content(content: str, fallback_path: str) -> str:
    """从内容首行 H1 或文件名提取标题。"""
    if content:
        for line in content.splitlines():
            line = line.strip()
            if line.startswith("# "):
                return line[2:].strip()
            if line and not line.startswith("---"):
                return line[:60]
    return fallback_path.rsplit("/", 1)[-1]


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """解析 frontmatter（YAML 头部），失败返回空 dict。

    frontmatter 格式：以 `---` 开头与结尾的 YAML 块。
    """
    if not content or not content.startswith("---"):
        return {}
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    end_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break
    if end_idx <= 0:
        return {}
    yaml_text = "\n".join(lines[1:end_idx])
    try:
        import yaml

        data = yaml.safe_load(yaml_text)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


__all__ = [
    "AlternativeSuggestion",
    "AssetGoneError",
    "AssetNotFoundError",
    "DegradedModulePathRequiredError",
    "GoneResponse",
    "RecallItem",
    "RecallListResponse",
    "RecallReadResponse",
    "RecallService",
    "RestrictedAccessDeniedError",
    "SyncStatusResponse",
]
