"""binding 域 FastAPI 路由 — Agent 5 公共 API 契约。

对应占位 API 契约（依赖方：Agent 6、Agent 10）：
- POST /v1/binding/create (agent_id, asset_id, type, priority)
- POST /v1/binding/auto (category, task_type) → 自动匹配并绑定
- GET  /v1/binding/list (agent_id) → [bindings]
- POST /v1/category/suggest (content, module_path) → [3 candidates]
- POST /v1/auth/apikey (member_id) → {api_key, agent_id}

附加端点（域内管理用）：
- POST /v1/binding/adopt （一键采纳 category 候选）
- POST /v1/binding/routing （注册调度索引）
- POST /v1/category/validate （校验 category）
- POST /v1/category/posthoc （push main 后 post-hoc 校验）
- POST /v1/tool/review （tool PR Review）
- POST /v1/auth/apikey/rotate （轮换 API Key）
- GET  /v1/auth/apikey/lookup （反查 agent_id）

依赖注入：
- configure_binding_api(services_dict) 由 FastAPI 启动事件调用
- 各 Service 通过模块级全局变量持有（与 webhook.py 模式一致）
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from server.binding.auth_service import AgentApiKeyService
from server.binding.binding_service import BindingService
from server.binding.category_suggest import CategorySuggestService
from server.binding.tool_review import PRFileInfo, ToolReviewService

# 子路由
binding_router = APIRouter(prefix="/v1/binding", tags=["binding"])
category_router = APIRouter(prefix="/v1/category", tags=["binding"])
auth_router = APIRouter(prefix="/v1/auth", tags=["binding"])
tool_router = APIRouter(prefix="/v1/tool", tags=["binding"])

# 模块级服务（启动时由 configure_binding_api 注入）
_BINDING: BindingService | None = None
_CATEGORY: CategorySuggestService | None = None
_AUTH: AgentApiKeyService | None = None
_TOOL: ToolReviewService | None = None


def configure_binding_api(
    *,
    binding: BindingService,
    category: CategorySuggestService,
    auth: AgentApiKeyService,
    tool: ToolReviewService,
) -> None:
    """注入 binding 域全部 Service（由 FastAPI 启动事件调用）。"""
    global _BINDING, _CATEGORY, _AUTH, _TOOL
    _BINDING = binding
    _CATEGORY = category
    _AUTH = auth
    _TOOL = tool


def _require_binding() -> BindingService:
    if _BINDING is None:
        raise HTTPException(status_code=503, detail="BindingService 未配置")
    return _BINDING


def _require_category() -> CategorySuggestService:
    if _CATEGORY is None:
        raise HTTPException(status_code=503, detail="CategorySuggestService 未配置")
    return _CATEGORY


def _require_auth() -> AgentApiKeyService:
    if _AUTH is None:
        raise HTTPException(status_code=503, detail="AgentApiKeyService 未配置")
    return _AUTH


def _require_tool() -> ToolReviewService:
    if _TOOL is None:
        raise HTTPException(status_code=503, detail="ToolReviewService 未配置")
    return _TOOL


# ---------------------------------------------------------------------------
# Pydantic 请求/响应模型
# ---------------------------------------------------------------------------


class CreateBindingRequest(BaseModel):
    agent_id: str
    asset_id: str
    type: str = Field(default="on-demand", description="fixed / on-demand")
    priority: str = Field(default="normal", description="high / normal / low")
    agent_role: str = ""
    binding_version: str = "0.0.1"


class CreateBindingResponse(BaseModel):
    binding_id: str


class AutoBindRequest(BaseModel):
    agent_id: str
    task_type: str
    category: str
    agent_role: str = ""


class AutoBindResponse(BaseModel):
    agent_id: str
    task_type: str
    category: str
    matched_count: int
    bound_count: int
    skipped_inactive: int
    skipped_existing: int
    binding_ids: list[str]


class BindingItem(BaseModel):
    id: str
    agent_id: str
    agent_role: str
    asset_id: str
    binding_type: str
    priority: str
    enabled: bool
    binding_version: str


class CategorySuggestRequest(BaseModel):
    content: str
    module_path: str = ""


class CategoryCandidateItem(BaseModel):
    category: str
    confidence: float
    rationale: str


class CategorySuggestResponse(BaseModel):
    candidates: list[CategoryCandidateItem]
    used_fallback: bool
    error: str = ""


class AdoptCategoryRequest(BaseModel):
    category: str
    rationale: str = ""
    description: str = ""


class ValidateCategoryRequest(BaseModel):
    category: str


class ValidateCategoryResponse(BaseModel):
    category: str
    format_valid: bool
    registered_in_yaml: bool
    module_indexed: bool
    ok: bool
    violations: list[str]


class PostHocRequest(BaseModel):
    changed_assets: list[tuple[str, str]]
    commit_sha: str


class PostHocResponse(BaseModel):
    commit_sha: str
    checked_assets: int
    pending_created: int
    alerts_sent: int
    pending_ids: list[str]


class IssueApiKeyRequest(BaseModel):
    member_id: str = Field(..., min_length=1, max_length=128)
    agent_id: str = Field(..., min_length=1, max_length=128)


class IssueApiKeyResponse(BaseModel):
    api_key: str
    agent_id: str
    key_id: str
    key_prefix: str


class RotateApiKeyRequest(BaseModel):
    key_id: str


class LookupApiKeyRequest(BaseModel):
    api_key: str


class LookupApiKeyResponse(BaseModel):
    agent_id: str | None


class ToolReviewRequest(BaseModel):
    pr_id: str
    asset_path: str
    commit_sha: str = ""
    content: str
    reviewers: list[str] = []
    approvers: list[str] = []


class ToolReviewResponse(BaseModel):
    asset_path: str
    signature_present: bool
    signature_valid: bool
    codeowners_approved: bool
    trusted_reviewers_count: int
    decision: str
    reason: str
    record_id: str = ""


# ---------------------------------------------------------------------------
# /v1/binding/* 端点
# ---------------------------------------------------------------------------


@binding_router.post("/create", response_model=CreateBindingResponse)
def create_binding(req: CreateBindingRequest) -> CreateBindingResponse:
    """创建装配（手动绑定）。"""
    svc = _require_binding()
    try:
        binding_id = svc.create_binding(
            agent_id=req.agent_id,
            asset_id=req.asset_id,
            binding_type=req.type,
            priority=req.priority,
            agent_role=req.agent_role,
            binding_version=req.binding_version,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return CreateBindingResponse(binding_id=binding_id)


@binding_router.post("/auto", response_model=AutoBindResponse)
def auto_bind(req: AutoBindRequest) -> AutoBindResponse:
    """自动匹配并绑定（按 task_type + category）。"""
    svc = _require_binding()
    result = svc.auto_bind(
        agent_id=req.agent_id,
        task_type=req.task_type,
        category=req.category,
        agent_role=req.agent_role,
    )
    return AutoBindResponse(
        agent_id=result.agent_id,
        task_type=result.task_type,
        category=result.category,
        matched_count=result.matched_count,
        bound_count=result.bound_count,
        skipped_inactive=result.skipped_inactive,
        skipped_existing=result.skipped_existing,
        binding_ids=result.binding_ids,
    )


@binding_router.get("/list", response_model=list[BindingItem])
def list_bindings(
    agent_id: str = Query(...),
    include_disabled: bool = Query(default=False),
    include_superseded: bool = Query(default=False),
) -> list[BindingItem]:
    """列出某 Agent 的装配。"""
    svc = _require_binding()
    items = svc.list_bindings(
        agent_id,
        include_disabled=include_disabled,
        include_superseded=include_superseded,
    )
    return [
        BindingItem(
            id=b.id,
            agent_id=b.agent_id,
            agent_role=b.agent_role,
            asset_id=b.asset_id,
            binding_type=b.binding_type,
            priority=b.priority,
            enabled=b.enabled,
            binding_version=b.binding_version,
        )
        for b in items
    ]


@binding_router.post("/routing")
def register_routing(req: dict[str, Any]) -> dict[str, Any]:
    """注册调度索引（task_type + category → asset_id）。"""
    svc = _require_binding()
    routing_id = svc.register_routing(
        task_type=req["task_type"],
        category=req["category"],
        asset_id=req["asset_id"],
        binding_type=req.get("binding_type", "on-demand"),
        priority=req.get("priority", "normal"),
        auto_bind=req.get("auto_bind", True),
    )
    return {"routing_id": routing_id}


# ---------------------------------------------------------------------------
# /v1/category/* 端点
# ---------------------------------------------------------------------------


@category_router.post("/suggest", response_model=CategorySuggestResponse)
def suggest_category(req: CategorySuggestRequest) -> CategorySuggestResponse:
    """LLM 推荐 3 个 category 候选。"""
    svc = _require_category()
    result = svc.suggest(content=req.content, module_path=req.module_path)
    return CategorySuggestResponse(
        candidates=[
            CategoryCandidateItem(
                category=c.category,
                confidence=c.confidence,
                rationale=c.rationale,
            )
            for c in result.candidates
        ],
        used_fallback=result.used_fallback,
        error=result.error,
    )


@category_router.post("/adopt")
def adopt_category(req: AdoptCategoryRequest) -> dict[str, Any]:
    """一键采纳候选：写入 categories.yaml。"""
    svc = _require_category()
    from server.binding.llm import CategoryCandidate

    candidate = CategoryCandidate(
        category=req.category, confidence=1.0, rationale=req.rationale
    )
    try:
        registry = svc.adopt_candidate(candidate, description=req.description)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {
        "adopted": req.category,
        "registry_size": len(registry.categories),
    }


@category_router.post("/validate", response_model=ValidateCategoryResponse)
def validate_category(req: ValidateCategoryRequest) -> ValidateCategoryResponse:
    """校验 category：两级 <type>-<module>，<module> 须 INDEX.md 登记。"""
    svc = _require_category()
    v = svc.validate(req.category)
    return ValidateCategoryResponse(
        category=v.category,
        format_valid=v.format_valid,
        registered_in_yaml=v.registered_in_yaml,
        module_indexed=v.module_indexed,
        ok=v.ok,
        violations=v.violations,
    )


@category_router.post("/posthoc", response_model=PostHocResponse)
def posthoc_check(req: PostHocRequest) -> PostHocResponse:
    """push main 后 post-hoc 校验。"""
    svc = _require_category()
    report = svc.posthoc_check(req.changed_assets, commit_sha=req.commit_sha)
    return PostHocResponse(
        commit_sha=report.commit_sha,
        checked_assets=report.checked_assets,
        pending_created=report.pending_created,
        alerts_sent=report.alerts_sent,
        pending_ids=report.pending_ids,
    )


# ---------------------------------------------------------------------------
# /v1/auth/* 端点
# ---------------------------------------------------------------------------


@auth_router.post("/apikey", response_model=IssueApiKeyResponse)
def issue_apikey(req: IssueApiKeyRequest) -> IssueApiKeyResponse:
    """颁发 API Key（返回原始 key 仅本次一次）。"""
    svc = _require_auth()
    issued = svc.issue(member_id=req.member_id, agent_id=req.agent_id)
    return IssueApiKeyResponse(
        api_key=issued.api_key,
        agent_id=issued.agent_id,
        key_id=issued.key_id,
        key_prefix=issued.key_prefix,
    )


@auth_router.post("/apikey/rotate", response_model=IssueApiKeyResponse)
def rotate_apikey(req: RotateApiKeyRequest) -> IssueApiKeyResponse:
    """轮换 API Key：旧 key 失效，颁发新 key。"""
    svc = _require_auth()
    try:
        issued = svc.rotate(req.key_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return IssueApiKeyResponse(
        api_key=issued.api_key,
        agent_id=issued.agent_id,
        key_id=issued.key_id,
        key_prefix=issued.key_prefix,
    )


@auth_router.post("/apikey/lookup", response_model=LookupApiKeyResponse)
def lookup_apikey(req: LookupApiKeyRequest) -> LookupApiKeyResponse:
    """通过 API Key 反查 agent_id（鉴权入口）。"""
    svc = _require_auth()
    agent_id = svc.lookup_agent_id(req.api_key)
    return LookupApiKeyResponse(agent_id=agent_id)


# ---------------------------------------------------------------------------
# /v1/tool/* 端点
# ---------------------------------------------------------------------------


@tool_router.post("/review", response_model=ToolReviewResponse)
def review_tool_pr(req: ToolReviewRequest) -> ToolReviewResponse:
    """tool PR Review：CODEOWNERS + 签名验证。"""
    svc = _require_tool()
    info = PRFileInfo(
        asset_path=req.asset_path,
        commit_sha=req.commit_sha,
        content=req.content,
        reviewers=req.reviewers,
        approvers=req.approvers,
    )
    result = svc.review_file(info, pr_id=req.pr_id)
    return ToolReviewResponse(
        asset_path=result.asset_path,
        signature_present=result.signature_present,
        signature_valid=result.signature_valid,
        codeowners_approved=result.codeowners_approved,
        trusted_reviewers_count=result.trusted_reviewers_count,
        decision=result.decision,
        reason=result.reason,
        record_id=result.record_id,
    )


__all__ = [
    "auth_router",
    "binding_router",
    "category_router",
    "configure_binding_api",
    "tool_router",
]
