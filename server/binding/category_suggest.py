"""CategorySuggestService — category 自动推断、校验、post-hoc 校验。

覆盖 SubTask：
- 5.3 category 自动推断（LLM 推荐 3 候选，一键采纳）
- 5.4 category 校验（两级 <type>-<module>，<module> 须 INDEX.md 登记）
- 5.5 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）

依赖：
- Agent 1 categories.py（已完成）：validate_category_format / check_module_registered /
  add_category / load_categories / serialize_categories_yaml
- Agent 1 index_manager.py（已完成）：discover_levels（INDEX.md 解析）
- Agent 5 llm.py（本域）：call_llm_for_category_suggestions
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select, update

from server.infra_db.db import Database
from server.binding.llm import (
    CategoryCandidate,
    LLMChatProtocol,
    SuggestResult,
    call_llm_for_category_suggestions,
)
from server.binding.models import PendingCategory
from server.infra_git.categories import (
    CategoriesRegistry,
    CategoryViolation,
    add_category,
    check_module_registered,
    load_categories,
    parse_category,
    serialize_categories_yaml,
    validate_category_format,
    validate_pr_categories,
)
from server.infra_git.index_manager import IndexDoc, discover_levels

logger = logging.getLogger(__name__)


@dataclass
class CategoryValidationResult:
    """category 校验结果。"""

    category: str
    format_valid: bool
    registered_in_yaml: bool
    module_indexed: bool
    violations: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.format_valid and self.registered_in_yaml and self.module_indexed


@dataclass
class PostHocReport:
    """post-hoc 校验报告。"""

    commit_sha: str
    checked_assets: int = 0
    pending_created: int = 0
    alerts_sent: int = 0
    violations: list[CategoryViolation] = field(default_factory=list)
    pending_ids: list[str] = field(default_factory=list)


class CategorySuggestService:
    """category 推断 / 校验 / post-hoc 服务。

    用法：
        svc = CategorySuggestService(database, repo_root=Path("./repo"))
        result = svc.suggest(content="...", module_path="modules/backend")
        v = svc.validate("rule-backend", docs=discover_levels(repo_root))
        report = svc.posthoc_check(changed_assets, commit_sha="abc123")
    """

    def __init__(
        self,
        database: Database,
        *,
        repo_root: Path | None = None,
        llm: LLMChatProtocol | None = None,
        alert_sink: Callable[[str, dict[str, Any]], None] | None = None,
    ) -> None:
        self._db = database
        self._repo_root = repo_root
        self._llm = llm
        self._alert_sink = alert_sink

    # ------------------------------------------------------------------
    # SubTask 5.3: category 自动推断（LLM 推荐 3 候选，一键采纳）
    # ------------------------------------------------------------------

    def suggest(self, *, content: str, module_path: str = "") -> SuggestResult:
        """LLM 推荐 3 个 category 候选。

        LLM 未注入 → 启发式 fallback（标记 used_fallback=True）。
        """
        return call_llm_for_category_suggestions(
            self._llm, content=content, module_path=module_path
        )

    def adopt_candidate(
        self,
        candidate: CategoryCandidate,
        *,
        description: str = "",
        modules: list[str] | None = None,
        persist_yaml: bool = True,
    ) -> CategoriesRegistry:
        """一键采纳候选：写入 categories.yaml 受控词汇表。

        - persist_yaml=True → 同步写入 .teamharness/categories.yaml
        - persist_yaml=False → 仅更新内存 registry（测试用）
        返回更新后的 registry。
        """
        if self._repo_root is None:
            raise RuntimeError("未配置 repo_root，无法持久化 categories.yaml")
        registry = load_categories(self._repo_root)
        # 拆 category 取 type 与 module
        parsed = parse_category(candidate.category)
        if parsed is None:
            raise ValueError(f"非法 category: {candidate.category}")
        _, module = parsed
        # modules 列表：默认含本 category 的 module
        mod_list = modules if modules is not None else [module]
        add_category(
            registry,
            name=candidate.category,
            description=description or candidate.rationale,
            modules=mod_list,
        )
        if persist_yaml:
            yaml_path = self._repo_root / ".teamharness" / "categories.yaml"
            yaml_path.parent.mkdir(parents=True, exist_ok=True)
            yaml_path.write_text(
                serialize_categories_yaml(registry), encoding="utf-8"
            )
        return registry

    # ------------------------------------------------------------------
    # SubTask 5.4: category 校验
    # ------------------------------------------------------------------

    def validate(
        self,
        category: str,
        *,
        docs: list[IndexDoc] | None = None,
        registry: CategoriesRegistry | None = None,
    ) -> CategoryValidationResult:
        """校验单个 category：格式 + categories.yaml 登记 + INDEX.md module 登记。

        对齐 Agent 1 categories.validate_pr_categories 逻辑，但返回结构化结果。
        """
        result = CategoryValidationResult(
            category=category,
            format_valid=False,
            registered_in_yaml=False,
            module_indexed=False,
        )
        # 1. 格式校验
        if not validate_category_format(category):
            result.violations.append(
                "category 不符合 `<type>-<module>` 命名规范或 type 非法"
            )
            return result
        result.format_valid = True
        # 2. categories.yaml 登记
        if registry is None and self._repo_root is not None:
            registry = load_categories(self._repo_root)
        if registry is None:
            # 无 repo_root 又无 registry → 视为未登记
            result.violations.append("未提供 categories.yaml registry，无法校验登记")
            return result
        if not registry.is_registered(category):
            result.violations.append("category 未在 categories.yaml 登记")
            return result
        result.registered_in_yaml = True
        # 3. INDEX.md module 登记
        if docs is None and self._repo_root is not None:
            docs = discover_levels(self._repo_root)
        if docs is None:
            result.violations.append("未提供 INDEX.md docs，无法校验 module 登记")
            return result
        if not check_module_registered(category, docs):
            parsed = parse_category(category)
            module = parsed[1] if parsed else "?"
            result.violations.append(
                f"category 的 <module>={module} 未在任一 INDEX.md 登记"
            )
            return result
        result.module_indexed = True
        return result

    def validate_pr(
        self,
        changed_assets: list[tuple[str, str]],
        *,
        docs: list[IndexDoc] | None = None,
        registry: CategoriesRegistry | None = None,
    ) -> list[CategoryViolation]:
        """PR Review 阶段批量校验（复用 Agent 1 validate_pr_categories）。

        changed_assets: [(asset_path, category), ...]
        返回非空 → 阻断合入。
        """
        if registry is None and self._repo_root is not None:
            registry = load_categories(self._repo_root)
        if docs is None and self._repo_root is not None:
            docs = discover_levels(self._repo_root)
        return validate_pr_categories(
            changed_assets,
            registry or CategoriesRegistry(),
            docs or [],
        )

    # ------------------------------------------------------------------
    # SubTask 5.5: 快速模式 post-hoc 校验（未登记自动创建 pending + 告警）
    # ------------------------------------------------------------------

    def posthoc_check(
        self,
        changed_assets: list[tuple[str, str]],
        *,
        commit_sha: str,
        docs: list[IndexDoc] | None = None,
        registry: CategoriesRegistry | None = None,
    ) -> PostHocReport:
        """push main 后 post-hoc 校验。

        流程：
        1. 对每个 (asset_path, category) 做校验
        2. <module> 未登记 → 创建 PendingCategory 行（status=pending, alert_sent=true）
        3. 触发告警（注入的 alert_sink）
        4. 已存在同 (category, module, asset_path, commit_sha) 的 pending → 跳过（幂等）

        与 PR Review 校验区别：
        - PR Review 阻断合入；post-hoc 是 push main 后的兜底（快速模式）
        - post-hoc 创建 pending 等待人工补登记 INDEX.md，不阻断
        """
        report = PostHocReport(commit_sha=commit_sha)
        report.checked_assets = len(changed_assets)
        # 加载 registry + docs
        if registry is None and self._repo_root is not None:
            registry = load_categories(self._repo_root)
        if docs is None and self._repo_root is not None:
            docs = discover_levels(self._repo_root)
        # 逐条校验
        for asset_path, category in changed_assets:
            v = self.validate(
                category, docs=docs, registry=registry or CategoriesRegistry()
            )
            if v.ok:
                continue
            # 拆 category 取 module
            parsed = parse_category(category)
            module = parsed[1] if parsed else ""
            # 已存在 pending（同 asset_path + commit_sha + category）→ 跳过
            with self._db.session() as sess:
                existing = sess.scalars(
                    select(PendingCategory).where(
                        PendingCategory.asset_path == asset_path,
                        PendingCategory.commit_sha == commit_sha,
                        PendingCategory.category == category,
                        PendingCategory.status == "pending",
                    )
                ).first()
                if existing is not None:
                    continue
                pending_id = f"pending-{uuid.uuid4().hex[:12]}"
                sess.add(
                    PendingCategory(
                        id=pending_id,
                        category=category,
                        module=module,
                        asset_path=asset_path,
                        commit_sha=commit_sha,
                        reason="; ".join(v.violations),
                        status="pending",
                        alert_sent=True,  # 同事务标记告警已发
                    )
                )
                report.pending_created += 1
                report.pending_ids.append(pending_id)
                # 收集违规清单
                report.violations.append(
                    CategoryViolation(
                        asset_path=asset_path,
                        category=category,
                        reason="; ".join(v.violations),
                    )
                )
                # 触发告警
                if self._alert_sink is not None:
                    try:
                        self._alert_sink(
                            "pending_category_created",
                            {
                                "pending_id": pending_id,
                                "category": category,
                                "module": module,
                                "asset_path": asset_path,
                                "commit_sha": commit_sha,
                                "reason": "; ".join(v.violations),
                            },
                        )
                        report.alerts_sent += 1
                    except Exception as exc:
                        logger.warning("告警 sink 调用失败: %s", exc)
        return report

    def list_pending(self, *, status: str = "pending") -> list[PendingCategory]:
        """列出待办 category。"""
        with self._db.session() as sess:
            return list(
                sess.scalars(
                    select(PendingCategory)
                    .where(PendingCategory.status == status)
                    .order_by(PendingCategory.created_at.desc())
                )
            )

    def resolve_pending(self, pending_id: str) -> bool:
        """人工补登记 INDEX.md 后标记 pending 已解决。"""
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            result = sess.execute(
                update(PendingCategory)
                .where(
                    PendingCategory.id == pending_id,
                    PendingCategory.status == "pending",
                )
                .values(status="resolved", resolved_at=now)
            )
            return result.rowcount > 0


__all__ = [
    "CategorySuggestService",
    "CategoryValidationResult",
    "PostHocReport",
]
