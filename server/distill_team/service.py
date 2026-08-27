"""TeamDistill — 二级提炼主服务（对外契约 API）。

提供：
- trigger_incremental() → job_id：Light 增量聚类 + REM + Deep + LLM 提炼
- trigger_full() → job_id：全量聚类（每周日 cron）
- get_job_status(job_id) → {status, snapshot_sha, progress}
- get_cold_start_progress() → {assets_needed, current_count}

执行流程（incremental / full 共用）：
1. JobSnapshotIsolation.start_job → 记录快照 SHA + 资产清单
2. ClusteringService.light_cluster / full_cluster → 产出 Cluster
3. ConventionBypass.collect_convention_clusters → convention 簇（旁路）
4. REMRecognizer.recognize → REMCluster
5. DeepScorer.score + PromotionGate.check → SixDimScore + GateResult
6. ColdStartBypass.apply_cold_start_marking（若冷启动期）
7. DistillPromptRunner.run → DistilledPrompt（LLM 6 步推理 + SKIP 写 DREAMS.md）
8. AdoptionRateChecker.check + apply_degradation → 采纳率降级
9. JobSnapshotIsolation.complete_job → 计算 delta，若 need_delta_job 触发增量
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select

from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database
from server.infra_db.embedding import EmbeddingService
from server.infra_db.models import AssetIndex as AssetIndexRow, DistillationJob
from server.infra_db.vectorstore import VectorStore
from server.distill_team.adoption import AdoptionRateChecker
from server.distill_team.clustering import ClusteringService, ClusterParams
from server.distill_team.cold_start import ColdStartBypass
from server.distill_team.convention import ConventionBypass
from server.distill_team.deep import DeepScorer, PromotionGate
from server.distill_team.llm_schema import DistillPromptRunner
from server.distill_team.models import (
    ColdStartProgress,
    DistilledPrompt,
    JobSnapshot,
    JobStatus,
    JobTriggerSource,
)
from server.distill_team.rem import REMRecognizer
from server.distill_team.snapshot import HeadResolver, JobSnapshotIsolation

logger = logging.getLogger(__name__)


@dataclass
class JobStatusResponse:
    """get_job_status 响应体。"""

    job_id: str
    status: str
    snapshot_sha: str
    trigger_source: str
    cluster_fingerprint: str
    started_at: str | None = None
    finished_at: str | None = None
    output_prompt_id: str | None = None
    score: float | None = None
    # 进度（简化：基于 status 推断百分比）
    progress: float = 0.0
    # 产出 Prompt（若已完成）
    prompt: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TeamDistill:
    """二级提炼主服务（对外契约 API）。

    用法：
        td = TeamDistill(
            database=db,
            asset_index=asset_index,
            embedding_service=emb,
            vector_store=vs,
            head_resolver=lambda: "abc123",
            repo_root=Path("./repo"),
        )
        job_id = td.trigger_incremental()
        status = td.get_job_status(job_id)
    """

    # 增量 delta 递归深度上限（防止无限循环）
    MAX_DELTA_DEPTH = 3

    def __init__(
        self,
        *,
        database: Database,
        asset_index: AssetIndex,
        embedding_service: EmbeddingService,
        vector_store: VectorStore,
        head_resolver: HeadResolver,
        repo_root: Path | None = None,
        llm: Any | None = None,
        cluster_params: ClusterParams | None = None,
        promotion_orchestrator: Any | None = None,
        coding_software: str | None = None,
    ) -> None:
        self._db = database
        self._asset_index = asset_index
        self._embedding = embedding_service
        self._vector_store = vector_store
        self._head_resolver = head_resolver
        self._repo_root = repo_root
        self._llm = llm
        self._cluster_params = cluster_params or ClusterParams()

        # 内部组件
        self._snapshot = JobSnapshotIsolation(database, asset_index, head_resolver)
        self._clustering = ClusteringService(
            database, asset_index, embedding_service, vector_store, self._cluster_params
        )
        self._rem = REMRecognizer()
        self._scorer = DeepScorer(database, asset_index)
        self._gate = PromotionGate(database, asset_index)
        self._cold_start = ColdStartBypass(self._gate)
        self._convention = ConventionBypass(database, asset_index)
        self._adoption = AdoptionRateChecker(database)
        self._prompt_runner = DistillPromptRunner(
            repo_root=repo_root, llm=llm
        )

        # 升维管理编排器（可选，若未提供则按需懒加载）
        self._promotion_orchestrator = promotion_orchestrator
        self._coding_software = coding_software

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def trigger_incremental(
        self,
        *,
        changed_asset_ids: list[str] | None = None,
        depth: int = 0,
    ) -> str:
        """触发 Light 增量聚类提炼。

        - changed_asset_ids=None → 自动从 last_synced_commit vs HEAD 推断
        - depth: 增量 delta 递归深度（内部用，防无限循环）
        返回 job_id。
        """
        if depth > self.MAX_DELTA_DEPTH:
            logger.warning(
                "增量 delta 递归深度 %d 超限 %d，跳过",
                depth, self.MAX_DELTA_DEPTH,
            )
            return ""

        # 1. 启动快照
        snapshot = self._snapshot.start_job(
            trigger_source=JobTriggerSource.INCREMENTAL,
        )

        # 2. 解析变更资产清单
        if changed_asset_ids is None:
            changed_asset_ids = self._auto_detect_changed_assets(snapshot)

        # 3. Light 增量聚类
        clusters = self._clustering.light_cluster(changed_asset_ids)
        # 3.1 追加 convention 旁路簇
        clusters.extend(self._convention.collect_convention_clusters())

        # 4. REM 识别
        rem_clusters = self._rem.recognize(clusters)

        # 5. 逐簇 Deep 评分 + LLM 提炼
        prompts = self._distill_clusters(rem_clusters, snapshot)

        # 6. 完成快照 + 计算 delta
        delta = self._snapshot.complete_job(
            snapshot,
            status=JobStatus.COMPLETED,
            output_prompt_id=prompts[0].prompt_id if prompts else None,
            score=prompts[0].score.total if prompts else None,
        )

        # 7. 若 need_delta_job → 触发增量（递归一次）
        if delta.need_delta_job and delta.changed_asset_ids:
            logger.info(
                "检测到增量 delta，触发递归 job depth=%d changed=%d",
                depth + 1, len(delta.changed_asset_ids),
            )
            self.trigger_incremental(
                changed_asset_ids=delta.changed_asset_ids,
                depth=depth + 1,
            )

        return snapshot.job_id

    def trigger_full(self) -> str:
        """触发全量聚类提炼（每周日 cron）。

        流程与 incremental 类似，但聚类用 full_cluster（全量重建）。
        """
        snapshot = self._snapshot.start_job(trigger_source=JobTriggerSource.FULL)

        # 全量聚类
        clusters = self._clustering.full_cluster()
        clusters.extend(self._convention.collect_convention_clusters())

        rem_clusters = self._rem.recognize(clusters)
        prompts = self._distill_clusters(rem_clusters, snapshot)

        self._snapshot.complete_job(
            snapshot,
            status=JobStatus.COMPLETED,
            output_prompt_id=prompts[0].prompt_id if prompts else None,
            score=prompts[0].score.total if prompts else None,
        )
        return snapshot.job_id

    def get_job_status(self, job_id: str) -> JobStatusResponse | None:
        """查询 job 状态。"""
        with self._db.session() as sess:
            job = sess.get(DistillationJob, job_id)
            if job is None:
                return None
            # 进度推断（基于 status）
            progress_map = {
                JobStatus.PENDING: 0.0,
                JobStatus.RUNNING: 0.5,
                JobStatus.COMPLETED: 1.0,
                JobStatus.FAILED: 1.0,
                JobStatus.SKIPPED: 1.0,
            }
            return JobStatusResponse(
                job_id=job.id,
                status=job.status,
                snapshot_sha=job.snapshot_commit,
                trigger_source=job.trigger_source,
                cluster_fingerprint=job.cluster_fingerprint,
                started_at=job.started_at.isoformat() if job.started_at else None,
                finished_at=job.finished_at.isoformat() if job.finished_at else None,
                output_prompt_id=job.output_prompt_id,
                score=job.score,
                progress=progress_map.get(job.status, 0.0),
            )

    def get_cold_start_progress(self) -> ColdStartProgress:
        """返回冷启动进度。"""
        assets_needed, current, is_cold = self._gate.get_cold_start_progress()
        return ColdStartProgress(
            assets_needed=assets_needed,
            current_count=current,
            is_cold_start=is_cold,
            remaining=max(0, assets_needed - current),
        )

    # ------------------------------------------------------------------
    # 内部：逐簇提炼
    # ------------------------------------------------------------------

    def _distill_clusters(
        self,
        rem_clusters: list,
        snapshot: JobSnapshot,
    ) -> list[DistilledPrompt]:
        """逐簇执行 Deep 评分 + LLM 提炼，返回 DistilledPrompt 列表。"""
        prompts: list[DistilledPrompt] = []
        for rem_cluster in rem_clusters:
            try:
                # 1. Deep 评分
                score = self._scorer.score(
                    rem_cluster, snapshot_commit=snapshot.snapshot_commit
                )
                # 2. 晋升门禁（convention 簇降级门禁）
                if rem_cluster.cluster.is_convention:
                    gate = self._build_convention_gate(rem_cluster, score)
                else:
                    gate = self._gate.check(rem_cluster, score)

                # 3. 冷启动标记
                if self._cold_start.is_cold_start():
                    # 冷启动期：门禁已降级（PromotionGate 内部处理）
                    pass

                # 4. LLM 6 步推理 + SKIP 写 DREAMS.md
                assets_content = self._fetch_assets_content(rem_cluster.cluster.asset_ids)
                prompt = self._prompt_runner.run(
                    rem_cluster, score, gate, assets_content=assets_content
                )

                # 5. 冷启动标记
                if self._cold_start.is_cold_start():
                    prompt = self._cold_start.apply_cold_start_marking(prompt)

                # 6. 采纳率降级
                adoption = self._adoption.check(rem_cluster.cluster.asset_ids)
                prompt = self._adoption.apply_degradation(prompt, adoption)

                # 7. 门禁不通过 → 强制 SKIP
                if not gate.passed:
                    prompt.in_skip_review = True
                    prompt.skip_reason = (
                        f"gate_failed: {'; '.join(gate.reasons)}"
                    )

                # 8. 升维管理（查重→回测→升维→归档→图谱→跨项目→连锁更新）
                # 门禁通过且非 SKIP 的 prompt 才进入升维流程
                if gate.passed and not prompt.in_skip_review:
                    self._apply_promotion(prompt, rem_cluster)

                prompts.append(prompt)
            except Exception as exc:
                logger.exception(
                    "簇提炼失败 cluster=%s: %s",
                    rem_cluster.cluster.cluster_id, exc,
                )
        return prompts

    def _build_convention_gate(self, rem_cluster, score):
        """convention 簇门禁（降级为来源多样性 ≥ 1）。"""
        from server.distill_team.models import GateResult
        from server.distill_team.convention import (
            CONVENTION_REQUIRED_RECALL_COUNT,
            CONVENTION_REQUIRED_SOURCE_DIVERSITY,
        )

        actual_sd = rem_cluster.cross_member_count
        # convention 簇的 recall_count 用 0 兜底（旁路门禁不要求召回）
        return GateResult(
            passed=actual_sd >= CONVENTION_REQUIRED_SOURCE_DIVERSITY,
            score=score,
            required_source_diversity=CONVENTION_REQUIRED_SOURCE_DIVERSITY,
            required_recall_count=CONVENTION_REQUIRED_RECALL_COUNT,
            actual_source_diversity=actual_sd,
            actual_recall_count=0,
            cold_start=self._gate.is_cold_start(),
            reasons=(
                []
                if actual_sd >= CONVENTION_REQUIRED_SOURCE_DIVERSITY
                else [f"convention 来源多样性 {actual_sd} < 1"]
            ),
        )

    # ------------------------------------------------------------------
    # 内部：辅助
    # ------------------------------------------------------------------

    def _auto_detect_changed_assets(self, snapshot: JobSnapshot) -> list[str]:
        """自动检测变更资产：从 asset_index 中找 git_commit != snapshot_commit 的资产。

        若 snapshot_commit 为空（HEAD 解析失败）→ 返回全部 active 资产（保守）
        """
        if not snapshot.snapshot_commit:
            rows = self._asset_index.query(
                AssetFilter(statuses=["active"]), limit=100000
            )
            return [r.id for r in rows]

        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow.id)
                .where(AssetIndexRow.status == "active")
                .where(AssetIndexRow.git_commit != snapshot.snapshot_commit)
            )
            return list(sess.scalars(stmt))

    def _fetch_assets_content(self, asset_ids: list[str]) -> list[dict]:
        """拉取簇内资产内容（用于 LLM prompt 注入）。"""
        if not asset_ids:
            return []
        with self._db.session() as sess:
            stmt = (
                select(AssetIndexRow)
                .where(AssetIndexRow.id.in_(asset_ids))
                .where(AssetIndexRow.status == "active")
            )
            rows = list(sess.scalars(stmt))
            return [
                {
                    "id": r.id,
                    "owner": r.owner,
                    "module_path": r.module_path,
                    "content": r.content_snapshot or "",
                }
                for r in rows
            ]

    # ------------------------------------------------------------------
    # 内部：升维管理
    # ------------------------------------------------------------------

    def _get_promotion_orchestrator(self):
        """懒加载升维管理编排器。

        若构造函数未注入 promotion_orchestrator，则按 coding_software 自动创建。
        """
        if self._promotion_orchestrator is not None:
            return self._promotion_orchestrator

        from server.distill_team.promotion.manager import PromotionOrchestrator

        project_root = self._repo_root or Path.cwd()
        self._promotion_orchestrator = PromotionOrchestrator(
            software=self._coding_software,
            project_root=project_root,
        )
        return self._promotion_orchestrator

    def _apply_promotion(self, prompt: DistilledPrompt, rem_cluster) -> None:
        """对提炼产出的 DistilledPrompt 执行升维管理。

        将 DistilledPrompt 转换为 RuleEntry，调用 PromotionOrchestrator.promote()。
        升维结果记录到日志，不修改 prompt 本身（升维产出在规则文件中）。
        """
        try:
            from server.distill_team.promotion.adapters.base import RuleEntry

            orchestrator = self._get_promotion_orchestrator()

            # 构造原始案例（用于回测）
            source_cases = [
                a.get("content", "") for a in self._fetch_assets_content(
                    rem_cluster.cluster.asset_ids
                )
            ]

            # DistilledPrompt → RuleEntry
            rule = RuleEntry(
                rule_id=prompt.prompt_id,
                title=prompt.title,
                content=prompt.content,
                file_path=Path(prompt.prompt_id + ".md"),
                category=prompt.category,
            )

            outcome = orchestrator.promote(
                rule,
                source_cases=source_cases,
                source_session="",
            )

            logger.info(
                "升维完成 prompt=%s status=%s layer=%s archive=%s graph=%s",
                outcome.rule_id,
                outcome.final_status,
                outcome.state.current_layer,
                outcome.archive_entry_id,
                outcome.graph_node_id,
            )
        except Exception as exc:
            logger.exception(
                "升维管理失败 prompt=%s: %s", prompt.prompt_id, exc,
            )


__all__ = ["JobStatusResponse", "TeamDistill"]
