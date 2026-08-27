"""Job 快照隔离（SubTask 8.12 + 缺陷 5.3 提炼 job 竞态修复）。

核心设计：
- 启动 job 时记录快照 commit SHA（snapshot_commit）+ 当前资产 id 清单
- job 全程基于该 SHA 读取资产（通过 AssetIndex.get_by_id + git_commit 比对）
- 完成时比对 HEAD 与 snapshot_commit：
  - 一致 → 直接完成
  - 不一致 → 计算增量 delta（新 commit 引入的资产 id），触发 delta job
- 写入 distillation_job 表的 snapshot_commit / trigger_source 字段

竞态根因（缺陷 5.3）：
  并发 webhook 提交期间，提炼 job 若不固定快照 SHA，
  会读到不一致状态（job 中途资产被删除/修改）。
  快照隔离保证 job 内部资产视图稳定。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Callable

from sqlalchemy import select

from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow, DistillationJob
from server.distill_team.models import (
    JobDelta,
    JobSnapshot,
    JobStatus,
    JobTriggerSource,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# HeadResolver：解析当前 HEAD commit SHA
# ---------------------------------------------------------------------------

HeadResolver = Callable[[], str]
"""HEAD commit 解析器：返回当前 git HEAD SHA。"""


# ---------------------------------------------------------------------------
# JobSnapshotIsolation
# ---------------------------------------------------------------------------


class JobSnapshotIsolation:
    """job 快照隔离管理器。

    用法：
        iso = JobSnapshotIsolation(database, asset_index, head_resolver)
        snapshot = iso.start_job(trigger_source="incremental")
        # ... job 全程基于 snapshot.snapshot_commit 读取资产 ...
        delta = iso.complete_job(snapshot)
        if delta.need_delta_job:
            # 触发增量 delta job
            ...
    """

    def __init__(
        self,
        database: Database,
        asset_index: AssetIndex,
        head_resolver: HeadResolver,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._head_resolver = head_resolver

    # ------------------------------------------------------------------
    # 启动快照
    # ------------------------------------------------------------------

    def start_job(
        self,
        *,
        trigger_source: str = JobTriggerSource.INCREMENTAL,
        cluster_fingerprint: str = "",
    ) -> JobSnapshot:
        """启动 job：写 distillation_job 行 + 记录快照 SHA + 资产清单。

        返回 JobSnapshot，调用方在 job 全程基于 snapshot_commit 读取。
        """
        head_commit = self._safe_resolve_head()
        job_id = f"distill-{uuid.uuid4().hex[:16]}"
        now = datetime.now(timezone.utc)

        # 拉取当前全部 active 资产清单（基于 last_synced_commit 视图）
        rows = self._asset_index.query(AssetFilter(statuses=["active"]), limit=100000)
        asset_ids = [r.id for r in rows]

        # 写 distillation_job 行
        with self._db.session() as sess:
            sess.add(
                DistillationJob(
                    id=job_id,
                    trigger_source=trigger_source,
                    cluster_fingerprint=cluster_fingerprint,
                    snapshot_commit=head_commit,
                    status=JobStatus.RUNNING,
                    input_asset_ids=",".join(asset_ids),
                    started_at=now,
                    created_at=now,
                )
            )

        snapshot = JobSnapshot(
            job_id=job_id,
            snapshot_commit=head_commit,
            head_commit_at_start=head_commit,
            asset_ids=asset_ids,
            asset_count=len(asset_ids),
            started_at=now,
        )
        logger.info(
            "distill job 启动快照 job_id=%s snapshot_commit=%s assets=%d",
            job_id,
            head_commit[:12] if head_commit else "(空)",
            len(asset_ids),
        )
        return snapshot

    # ------------------------------------------------------------------
    # 完成时增量 delta
    # ------------------------------------------------------------------

    def complete_job(
        self,
        snapshot: JobSnapshot,
        *,
        output_prompt_id: str | None = None,
        score: float | None = None,
        status: str = JobStatus.COMPLETED,
    ) -> JobDelta:
        """完成 job：比对 HEAD 与 snapshot_commit，计算增量 delta。

        - HEAD == snapshot_commit → need_delta_job=False
        - HEAD != snapshot_commit → 计算变更资产清单，need_delta_job=True
        - 更新 distillation_job 行（status / finished_at / output_prompt_id / score）
        """
        head_after = self._safe_resolve_head()
        now = datetime.now(timezone.utc)

        changed_asset_ids: list[str] = []
        need_delta = False
        new_commit = ""

        if head_after and head_after != snapshot.snapshot_commit:
            new_commit = head_after
            need_delta = True
            # 计算变更资产 id：从 asset_index 中找 git_commit == head_after 的资产
            # （即快照之后被 upsert 的资产，其 git_commit 字段为新 commit）
            changed_asset_ids = self._collect_changed_assets(
                snapshot_commit=snapshot.snapshot_commit,
                head_commit=head_after,
            )

        # 更新 distillation_job 行
        with self._db.session() as sess:
            job = sess.get(DistillationJob, snapshot.job_id)
            if job is not None:
                job.status = status
                job.finished_at = now
                if output_prompt_id is not None:
                    job.output_prompt_id = output_prompt_id
                if score is not None:
                    job.score = score

        delta = JobDelta(
            job_id=snapshot.job_id,
            snapshot_commit=snapshot.snapshot_commit,
            head_commit_after=head_after,
            new_commit=new_commit,
            changed_asset_ids=changed_asset_ids,
            need_delta_job=need_delta,
        )
        logger.info(
            "distill job 完成 job_id=%s status=%s delta_need=%s changed=%d",
            snapshot.job_id,
            status,
            need_delta,
            len(changed_asset_ids),
        )
        return delta

    # ------------------------------------------------------------------
    # 内部：解析 HEAD（容错）
    # ------------------------------------------------------------------

    def _safe_resolve_head(self) -> str:
        """安全解析 HEAD，失败返回空字符串。"""
        try:
            return self._head_resolver() or ""
        except Exception as exc:
            logger.warning("解析 HEAD commit 失败，快照用空 SHA: %s", exc)
            return ""

    def _collect_changed_assets(
        self,
        *,
        snapshot_commit: str,
        head_commit: str,
    ) -> list[str]:
        """收集 snapshot_commit → head_commit 之间变更的资产 id。

        判定：asset_index.git_commit 与 snapshot_commit 不同（且 status=active）。
        注意：Deleted 资产 status=deleted 也会被排除。
        """
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow.id)
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.git_commit != snapshot_commit)
            )
            return [row for row in sess.scalars(stmt)]


__all__ = ["HeadResolver", "JobSnapshotIsolation"]
