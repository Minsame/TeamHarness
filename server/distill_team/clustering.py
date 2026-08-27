"""Light 增量聚类 + 全量聚类（SubTask 8.1 + 8.2）。

Light 增量聚类（8.1）：
- 输入：仅新增/修改资产（来自 job delta 或显式传入 asset_ids）
- 流程：每个新增资产 → 向量检索 top-K 邻居 → 形成簇 → 用 cluster_fingerprint 去重
- 去重：若同 fingerprint 的簇已提炼过（distillation_job 表中存在 completed）→ 跳过

全量聚类（8.2）：
- 每周日凌晨 cron 触发（service.trigger_full）
- 流程：全部 active 资产 → 重新聚类（DBSCAN-like，基于余弦相似度阈值）
- 用于发现新增跨成员模式（Light 增量可能漏检）

聚类算法选择：
- 简单可控：基于向量余弦相似度的贪心聚类（top-K 邻接合并）
- 不依赖 sklearn，纯 Python 实现（测试友好）
"""

from __future__ import annotations

import hashlib
import logging
import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import DistillationJob, AssetIndex as AssetIndexRow
from server.infra_db.vectorstore import VectorStore
from server.distill_team.models import Cluster

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 聚类参数
# ---------------------------------------------------------------------------


@dataclass
class ClusterParams:
    """聚类参数。"""

    # Light 增量聚类：邻居 top-K
    light_top_k: int = 5
    # 余弦相似度阈值（≥ 才视为同簇）
    similarity_threshold: float = 0.75
    # 簇内最少资产数（少于则视为孤点，不形成簇）
    min_cluster_size: int = 2
    # 全量聚类：扫描批次大小（避免一次拉全量向量库）
    full_batch_size: int = 500


# ---------------------------------------------------------------------------
# ClusterFingerprint：去重指纹
# ---------------------------------------------------------------------------


def compute_cluster_fingerprint(asset_ids: list[str]) -> str:
    """计算簇指纹：资产 id 排序后 SHA256 取前 16 hex。

    排序保证同集合不同顺序的簇指纹相同。
    """
    sorted_ids = sorted(set(asset_ids))
    raw = "|".join(sorted_ids)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def is_cluster_already_distilled(
    database: Database, fingerprint: str
) -> bool:
    """检查同 fingerprint 的簇是否已提炼过（distillation_job 表存在 completed 行）。

    增量去重核心：若已提炼则跳过，避免重复产出。
    """
    with database.session() as sess:
        stmt = (
            select(DistillationJob.id)
            .where(DistillationJob.cluster_fingerprint == fingerprint)
            .where(DistillationJob.status == "completed")
            .limit(1)
        )
        return sess.scalars(stmt).first() is not None


# ---------------------------------------------------------------------------
# ClusteringService
# ---------------------------------------------------------------------------


class ClusteringService:
    """聚类服务（Light 增量 + 全量）。

    用法：
        cs = ClusteringService(database, asset_index, embedding_service, vector_store)
        # Light 增量
        clusters = cs.light_cluster(changed_asset_ids=["a1", "a2"])
        # 全量
        all_clusters = cs.full_cluster()
    """

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        params: ClusterParams | None = None,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._embedding = embedding_service
        self._vector_store = vector_store
        self._params = params or ClusterParams()

    # ------------------------------------------------------------------
    # Light 增量聚类
    # ------------------------------------------------------------------

    def light_cluster(
        self,
        changed_asset_ids: list[str],
        *,
        skip_already_distilled: bool = True,
    ) -> list[Cluster]:
        """Light 增量聚类：只处理新增/修改资产。

        流程：
        1. 每个 changed_asset → 计算 embedding → 向量检索 top-K 邻居
        2. 邻居中相似度 ≥ threshold 的合并为同簇
        3. 簇内资产数 ≥ min_cluster_size 才形成簇
        4. cluster_fingerprint 去重：若同指纹簇已提炼过，跳过

        返回 Cluster 列表（孤点不返回）。
        """
        if not changed_asset_ids:
            return []

        active_rows = self._fetch_active_rows(changed_asset_ids)
        if not active_rows:
            return []

        # 用 dict[centroid_id, set(asset_id)] 表示合并中的簇
        merged: dict[str, set[str]] = {}
        # 资产元数据缓存
        meta: dict[str, AssetIndexRow] = {r.id: r for r in active_rows}

        for row in active_rows:
            neighbors = self._find_neighbors(row.id, top_k=self._params.light_top_k)
            # 邻居含自己（若向量库已索引）；过滤 < threshold
            close_neighbors = [
                (nid, score)
                for nid, score in neighbors
                if score >= self._params.similarity_threshold
            ]
            if not close_neighbors:
                # 孤点 → 自成一簇（最后过滤 min_cluster_size）
                merged.setdefault(row.id, {row.id})
                continue
            # 合并：取第一个邻居作为 cluster key（贪心）
            cluster_key = close_neighbors[0][0]
            merged.setdefault(cluster_key, set()).add(row.id)
            for nid, _ in close_neighbors:
                merged[cluster_key].add(nid)

        # 后处理：合并重叠簇（若 asset 同时在多簇，合并到第一簇）
        merged = self._merge_overlapping(merged)

        # 过滤 + 构造 Cluster 对象
        clusters: list[Cluster] = []
        for cluster_assets in merged.values():
            if len(cluster_assets) < self._params.min_cluster_size:
                continue
            # 拉取簇内资产完整 metadata（邻居可能是非 changed 资产）
            cluster_meta = self._fetch_active_rows(list(cluster_assets))
            if not cluster_meta:
                continue
            meta_map = {r.id: r for r in cluster_meta}
            asset_ids_sorted = sorted(cluster_assets)
            fingerprint = compute_cluster_fingerprint(asset_ids_sorted)

            # 去重：同 fingerprint 已提炼过则跳过
            if skip_already_distilled and is_cluster_already_distilled(
                self._db, fingerprint
            ):
                logger.info(
                    "Light 聚类跳过已提炼簇 fingerprint=%s size=%d",
                    fingerprint,
                    len(cluster_assets),
                )
                continue

            owners = list({meta_map[a].owner for a in asset_ids_sorted if a in meta_map})
            module_paths = list(
                {meta_map[a].module_path for a in asset_ids_sorted if a in meta_map}
            )
            categories = list(
                {meta_map[a].category for a in asset_ids_sorted if a in meta_map}
            )
            cluster = Cluster(
                cluster_id=f"cluster-{uuid.uuid4().hex[:12]}",
                fingerprint=fingerprint,
                asset_ids=asset_ids_sorted,
                owners=owners,
                module_paths=module_paths,
                category=categories[0] if len(categories) == 1 else None,
                centroid_asset_id=asset_ids_sorted[0],
                cohesion=self._compute_cohesion(asset_ids_sorted),
            )
            clusters.append(cluster)
        return clusters

    # ------------------------------------------------------------------
    # 全量聚类
    # ------------------------------------------------------------------

    def full_cluster(self) -> list[Cluster]:
        """全量聚类：全部 active 资产重新聚类。

        每周日 cron 触发。流程与 Light 类似，但输入是全部 active 资产。
        不做 fingerprint 去重（全量重建，发现新跨成员模式）。
        """
        all_rows = self._asset_index.query(
            AssetFilter(statuses=["active"]), limit=100000
        )
        all_ids = [r.id for r in all_rows]
        if not all_ids:
            return []

        # 分批扫描（避免一次拉全量向量库）
        clusters: list[Cluster] = []
        seen_asset_ids: set[str] = set()
        for i in range(0, len(all_ids), self._params.full_batch_size):
            batch = all_ids[i : i + self._params.full_batch_size]
            batch_clusters = self.light_cluster(
                batch, skip_already_distilled=False
            )
            for cluster in batch_clusters:
                # 去重：若簇内资产已在其他簇出现，跳过（贪心：先到的优先）
                if seen_asset_ids & set(cluster.asset_ids):
                    continue
                seen_asset_ids.update(cluster.asset_ids)
                clusters.append(cluster)
        return clusters

    # ------------------------------------------------------------------
    # 内部：邻居检索
    # ------------------------------------------------------------------

    def _find_neighbors(self, asset_id: str, *, top_k: int) -> list[tuple[str, float]]:
        """向量检索 top-K 邻居（含自身，若已索引）。

        向量库无结果或资产未索引时退化为 content 匹配（冷启动容错）：
        优先 content_hash 匹配，其次 content_snapshot 精确匹配。
        """
        try:
            status = self._asset_index.get_status(asset_id)
            if status is None:
                return []
            # 优先向量检索
            if status.embedding_id:
                active_version = self._embedding.get_active_version()
                emb_result = self._embedding.embed(
                    status.git_path, model_version=active_version
                )
                hits = self._vector_store.search(
                    emb_result.vector,
                    model_version=active_version,
                    top_k=top_k,
                )
                if hits:
                    return [(h.asset_id, float(h.score)) for h in hits]
            # 向量检索无结果 → content fallback（冷启动容错）
            return self._find_content_neighbors(asset_id, top_k=top_k)
        except Exception as exc:
            logger.warning(
                "邻居检索失败 asset=%s 退化为孤点: %s", asset_id, exc
            )
            return []

    def _find_content_neighbors(
        self, asset_id: str, *, top_k: int
    ) -> list[tuple[str, float]]:
        """content fallback：同 content_hash / content_snapshot 的资产视为邻居。

        冷启动期（embedding 未就绪）的容错策略，相似度固定 1.0。
        """
        with self._db.session() as sess:
            target = sess.get(AssetIndexRow, asset_id)
            if target is None:
                return []
            # 优先 content_hash 匹配
            if target.content_hash:
                stmt = (
                    select(AssetIndexRow.id)
                    .where(AssetIndexRow.content_hash == target.content_hash)
                    .where(AssetIndexRow.status == "active")
                    .where(AssetIndexRow.id != asset_id)
                    .limit(top_k)
                )
                neighbor_ids = list(sess.scalars(stmt))
                if neighbor_ids:
                    return [(nid, 1.0) for nid in neighbor_ids]
            # content_hash 为空 → content_snapshot 精确匹配
            if target.content_snapshot:
                stmt = (
                    select(AssetIndexRow.id)
                    .where(
                        AssetIndexRow.content_snapshot == target.content_snapshot
                    )
                    .where(AssetIndexRow.status == "active")
                    .where(AssetIndexRow.id != asset_id)
                    .limit(top_k)
                )
                neighbor_ids = list(sess.scalars(stmt))
                return [(nid, 1.0) for nid in neighbor_ids]
            return []

    def _fetch_active_rows(self, asset_ids: list[str]) -> list[AssetIndexRow]:
        """按 id 列表批量拉取 active 资产行。"""
        if not asset_ids:
            return []
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.id.in_(asset_ids))
                .where(AssetIndexRow.status == "active")
            )
            return list(sess.scalars(stmt))

    def _merge_overlapping(
        self, merged: dict[str, set[str]]
    ) -> dict[str, set[str]]:
        """合并重叠簇：若两簇有交集，合并为同一簇。"""
        keys = list(merged.keys())
        for i in range(len(keys)):
            for j in range(i + 1, len(keys)):
                k1, k2 = keys[i], keys[j]
                if k1 not in merged or k2 not in merged:
                    continue
                if merged[k1] & merged[k2]:
                    merged[k1] |= merged[k2]
                    merged.pop(k2, None)
        return merged

    def _compute_cohesion(self, asset_ids: list[str]) -> float:
        """计算簇内平均相似度（cohesion）。

        简化实现：用 content_hash 重叠度近似（避免重复向量检索）。
        真实场景应基于向量余弦相似度均值。
        """
        if len(asset_ids) < 2:
            return 1.0
        rows = self._fetch_active_rows(asset_ids)
        if len(rows) < 2:
            return 0.0
        # 用 tags 集合 Jaccard 近似
        tags_sets: list[set[str]] = []
        for r in rows:
            try:
                import json
                tags = json.loads(r.tags) if r.tags else []
                tags_sets.append(set(str(t) for t in tags))
            except Exception:
                tags_sets.append(set())
        if not tags_sets or not any(tags_sets):
            return 0.5  # 无 tags 信息时中性
        total_jaccard = 0.0
        pairs = 0
        for i in range(len(tags_sets)):
            for j in range(i + 1, len(tags_sets)):
                s1, s2 = tags_sets[i], tags_sets[j]
                if not s1 and not s2:
                    continue
                union = s1 | s2
                if not union:
                    continue
                total_jaccard += len(s1 & s2) / len(union)
                pairs += 1
        return total_jaccard / pairs if pairs > 0 else 0.5


__all__ = [
    "ClusterParams",
    "ClusteringService",
    "compute_cluster_fingerprint",
    "is_cluster_already_distilled",
]
