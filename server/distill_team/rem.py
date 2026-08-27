"""REM 跨成员模式识别（SubTask 8.3）。

REM 阶段：在 Light 聚类产出的 Cluster 基础上，识别跨成员模式：
- 跨 owner（不同成员重复出现同一问题/经验）→ 来源多样性信号
- 跨 module_path（不同模块重复模式）→ 泛化性信号
- 同 category 跨成员 → 强跨成员信号

输出：REMCluster（含 cross_member_count / cross_module_count / is_cross_member）
晋升门禁的"来源多样性 ≥ 3"判定依赖此阶段产出。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

from server.distill_team.models import Cluster

logger = logging.getLogger(__name__)


@dataclass
class REMCluster:
    """REM 阶段标注后的簇。"""

    cluster: Cluster
    cross_member_count: int  # 去重 owner 数
    cross_module_count: int  # 去重 module_path 数
    is_cross_member: bool  # cross_member_count >= 2
    is_cross_module: bool  # cross_module_count >= 2
    # 同 category 跨成员强信号
    is_strong_cross_member: bool = False
    # 反例信号：簇内资产互相矛盾（如规则冲突），需进 SKIP 审查
    has_counter_example: bool = False
    # REM 阶段识别出的共同主题（用于 Prompt 标题生成）
    common_topic: str = ""
    # 命中的跨成员 owner 列表（用于 Deep 阶段来源多样性归因）
    cross_owners: list[str] = field(default_factory=list)


class REMRecognizer:
    """REM 跨成员模式识别器。

    用法：
        rem = REMRecognizer()
        rem_clusters = rem.recognize(clusters)
    """

    # 跨成员阈值：≥ 2 视为跨成员
    CROSS_MEMBER_THRESHOLD = 2
    # 强跨成员：同 category 跨成员
    STRONG_CROSS_MEMBER_MIN_OWNERS = 2

    def recognize(self, clusters: list[Cluster]) -> list[REMCluster]:
        """识别跨成员模式。"""
        result: list[REMCluster] = []
        for cluster in clusters:
            owners = [o for o in cluster.owners if o]
            modules = [m for m in cluster.module_paths if m]
            unique_owners = list(dict.fromkeys(owners))  # 保序去重
            unique_modules = list(dict.fromkeys(modules))

            cross_member_count = len(unique_owners)
            cross_module_count = len(unique_modules)
            is_cross_member = cross_member_count >= self.CROSS_MEMBER_THRESHOLD
            is_cross_module = cross_module_count >= self.CROSS_MEMBER_THRESHOLD

            # 强跨成员：同 category + 跨 ≥ 2 成员
            is_strong = (
                is_cross_member
                and cluster.category is not None
                and cross_member_count >= self.STRONG_CROSS_MEMBER_MIN_OWNERS
            )

            # 共同主题：取 cluster.category 或前两个 owner 共同 tag
            common_topic = cluster.category or ""
            if not common_topic and unique_owners:
                common_topic = f"跨成员模式（{cross_member_count} 成员）"

            rem_cluster = REMCluster(
                cluster=cluster,
                cross_member_count=cross_member_count,
                cross_module_count=cross_module_count,
                is_cross_member=is_cross_member,
                is_cross_module=is_cross_module,
                is_strong_cross_member=is_strong,
                common_topic=common_topic,
                cross_owners=unique_owners,
            )
            result.append(rem_cluster)
        return result

    def filter_cross_member(
        self, rem_clusters: list[REMCluster]
    ) -> list[REMCluster]:
        """过滤出仅跨成员的簇（晋升候选）。"""
        return [rc for rc in rem_clusters if rc.is_cross_member]


__all__ = ["REMCluster", "REMRecognizer"]
