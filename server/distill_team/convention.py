"""is_convention=true 单成员旁路（SubTask 8.7）。

设计：
- 资产 frontmatter 含 is_convention: true 标记（团队约定资产）
- 单成员即可触发提炼（绕过跨成员门禁 ≥ 3 的要求）
- convention 资产直接聚成簇 → 走 Deep 评分（门禁降级为来源多样性 ≥ 1）
- 仍要经过反例检验（避免误提升冲突约定）

用途：团队显式声明的约定（如代码风格、命名规范）无需等跨成员重复出现即可提炼。
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from sqlalchemy import select

from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow
from server.distill_team.models import Cluster

logger = logging.getLogger(__name__)


def parse_frontmatter(content: str) -> dict[str, Any]:
    """解析 frontmatter（YAML 头部），失败返回空 dict。

    复用 recall/service.py 同款实现，避免循环依赖。
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


def is_convention_asset(row: AssetIndexRow) -> bool:
    """判断资产是否标记 is_convention=true。"""
    fm = parse_frontmatter(row.content_snapshot or "")
    val = fm.get("is_convention")
    if val is None:
        return False
    if isinstance(val, bool):
        return val
    return str(val).lower() in ("true", "1", "yes")


class ConventionBypass:
    """is_convention 单成员旁路。

    用法：
        bypass = ConventionBypass(database, asset_index)
        clusters = bypass.collect_convention_clusters()
        # 这些 cluster 已 is_convention=True，Deep 阶段门禁降级
    """

    # convention 旁路门禁：来源多样性 ≥ 1（单成员即可）
    CONVENTION_REQUIRED_SOURCE_DIVERSITY = 1
    # convention 旁路门禁：被召回 ≥ 1 次
    CONVENTION_REQUIRED_RECALL_COUNT = 1

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
    ) -> None:
        self._db = database
        self._asset_index = asset_index

    def collect_convention_assets(self) -> list[AssetIndexRow]:
        """收集所有 is_convention=true 的 active 资产。"""
        rows = self._asset_index.query(
            AssetFilter(statuses=["active"]), limit=100000
        )
        return [r for r in rows if is_convention_asset(r)]

    def collect_convention_clusters(self) -> list[Cluster]:
        """将 convention 资产按 category 聚成簇。

        同 category 的 convention 资产聚为同一簇；无 category 的各自成簇。
        """
        rows = self.collect_convention_assets()
        if not rows:
            return []

        # 按 category 分组；无 category 的各自成簇（用唯一 key 隔离）
        groups: dict[str, list[AssetIndexRow]] = {}
        for r in rows:
            key = r.category if r.category else f"__individual_{r.id}"
            groups.setdefault(key, []).append(r)

        clusters: list[Cluster] = []
        for category, group_rows in groups.items():
            asset_ids = sorted(r.id for r in group_rows)
            owners = list({r.owner for r in group_rows})
            module_paths = list({r.module_path for r in group_rows})
            from server.distill_team.clustering import compute_cluster_fingerprint

            cluster = Cluster(
                cluster_id=f"conv-{uuid.uuid4().hex[:12]}",
                fingerprint=compute_cluster_fingerprint(asset_ids),
                asset_ids=asset_ids,
                owners=owners,
                module_paths=module_paths,
                category=category,
                centroid_asset_id=asset_ids[0] if asset_ids else None,
                cohesion=1.0,  # convention 资产同分类，cohesion 默认 1.0
                is_convention=True,
            )
            clusters.append(cluster)
        return clusters


__all__ = [
    "CONVENTION_REQUIRED_RECALL_COUNT",
    "CONVENTION_REQUIRED_SOURCE_DIVERSITY",
    "ConventionBypass",
    "is_convention_asset",
    "parse_frontmatter",
]
