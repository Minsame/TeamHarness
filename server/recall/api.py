"""RecallService FastAPI router（Agent 4 对外 HTTP 契约）。

对应技术方案 3.2b 三契约端点：
- POST /v1/recall/list
- POST /v1/recall/read
- GET /v1/sync/status

特性：
- trace_id 中间件：从 X-Trace-Id / X-Request-Id / traceparent 头取或生成 32 位 hex
- 异常 → HTTP 状态码映射：
  - DegradedModulePathRequiredError → 503
  - AssetNotFoundError → 404
  - AssetGoneError → 410（含 alternatives）
  - RestrictedAccessDeniedError → 403
- 响应头 X-Trace-Id 透传 trace_id，便于客户端关联日志
"""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field

from server.recall import tracing
from server.recall.service import (
    AssetGoneError,
    AssetNotFoundError,
    DegradedModulePathRequiredError,
    RecallService,
    RestrictedAccessDeniedError,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Pydantic 请求 / 响应模型
# ---------------------------------------------------------------------------


class RecallListRequest(BaseModel):
    """POST /v1/recall/list 请求体。"""

    agent_id: str
    query: str | None = None
    module_path: str | None = None
    consistency: str = Field(default="eventual", pattern="^(eventual|strict)$")
    task_type: str | None = None
    asset_type: str | None = None
    tags: list[str] | None = None
    top_k: int | None = Field(default=None, ge=1, le=100)


class RecallItemSchema(BaseModel):
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


class RecallListResponseSchema(BaseModel):
    """POST /v1/recall/list 响应体。"""

    items: list[RecallItemSchema]
    as_of_commit: str
    sync_lag_seconds: float
    degraded: bool
    trace_id: str
    degraded_reason: str | None = None


class RecallReadRequest(BaseModel):
    """POST /v1/recall/read 请求体。"""

    agent_id: str
    asset_id: str
    consistency: str = Field(default="eventual", pattern="^(eventual|strict)$")


class RecallReadResponseSchema(BaseModel):
    """POST /v1/recall/read 响应体。"""

    content: str
    frontmatter: dict[str, Any]
    asset_id: str
    git_path: str
    as_of_commit: str
    degraded: bool
    trace_id: str
    from_local_copy: bool = False


class AlternativeSchema(BaseModel):
    """410 Gone 替代建议项。"""

    asset_id: str
    title: str
    git_path: str
    module_path: str
    category: str | None = None


class GoneResponseSchema(BaseModel):
    """recall/read 已删除资产 410 响应。"""

    asset_id: str
    message: str
    alternatives: list[AlternativeSchema]
    trace_id: str


class SyncStatusResponseSchema(BaseModel):
    """GET /v1/sync/status 响应体。"""

    last_synced_commit: str
    lag_seconds: float
    sync_source: str
    last_synced_at: str | None = None
    status: str = "ok"
    last_error: str | None = None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


recall_router = APIRouter(prefix="/v1", tags=["recall"])

# 全局 RecallService 实例（由 configure_recall 注入）
_RECALL_SERVICE: RecallService | None = None


def configure_recall(service: RecallService) -> None:
    """注入 RecallService 实例（FastAPI 启动事件调用）。"""
    global _RECALL_SERVICE
    _RECALL_SERVICE = service


def _get_service() -> RecallService:
    """获取已注入的 RecallService，未配置返回 503。"""
    if _RECALL_SERVICE is None:
        raise HTTPException(status_code=503, detail="RecallService 未配置")
    return _RECALL_SERVICE


def build_router(service: RecallService | None = None) -> APIRouter:
    """构造 router，可显式传入 service（用于测试隔离）。

    若传入 service，则本 router 内的端点直接使用该实例（不依赖全局 _RECALL_SERVICE）；
    不传则运行时从全局取（生产路径，由 configure_recall 注入）。
    """
    if service is not None:
        # 用闭包绑定 service，避免污染全局
        return _build_router_with_service(service)
    return recall_router


def _build_router_with_service(svc: RecallService) -> APIRouter:
    """为测试场景构造绑定特定 service 的 router。"""
    router = APIRouter(prefix="/v1", tags=["recall"])

    @router.post("/recall/list", response_model=RecallListResponseSchema)
    def recall_list(
        req: RecallListRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tid = _resolve_trace_id(request)
        try:
            result = svc.recall_list(
                agent_id=req.agent_id,
                query=req.query,
                module_path=req.module_path,
                consistency=req.consistency,
                task_type=req.task_type,
                asset_type=req.asset_type,
                tags=req.tags,
                top_k=req.top_k,
                trace_id=tid,
            )
        except DegradedModulePathRequiredError as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("recall_list 内部错误")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        response.headers["X-Trace-Id"] = result.trace_id
        return result.to_dict()

    @router.post("/recall/read")
    def recall_read(
        req: RecallReadRequest,
        request: Request,
        response: Response,
    ) -> dict[str, Any]:
        tid = _resolve_trace_id(request)
        try:
            result = svc.recall_read(
                agent_id=req.agent_id,
                asset_id=req.asset_id,
                consistency=req.consistency,
                trace_id=tid,
            )
        except AssetNotFoundError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except AssetGoneError as exc:
            response.headers["X-Trace-Id"] = tid
            # 410 Gone + alternatives
            return Response(
                content=GoneResponseSchema(
                    asset_id=exc.asset_id,
                    message=f"资产 {exc.asset_id} 已删除",
                    alternatives=[
                        AlternativeSchema(**alt.__dict__) for alt in exc.alternatives
                    ],
                    trace_id=tid,
                ).model_dump_json(),
                status_code=410,
                media_type="application/json",
                headers={"X-Trace-Id": tid},
            )
        except RestrictedAccessDeniedError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            logger.exception("recall_read 内部错误")
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        response.headers["X-Trace-Id"] = result.trace_id
        return result.to_dict()

    @router.get("/sync/status", response_model=SyncStatusResponseSchema)
    def sync_status(response: Response) -> dict[str, Any]:
        result = svc.get_sync_status()
        response.headers["X-Trace-Id"] = tracing.get_trace_id() or tracing.new_trace_id()
        return result.to_dict()

    return router


# ---------------------------------------------------------------------------
# 全局 router 端点（生产路径，依赖 configure_recall 注入 service）
# ---------------------------------------------------------------------------


@recall_router.post("/recall/list", response_model=RecallListResponseSchema)
def recall_list_endpoint(
    req: RecallListRequest,
    request: Request,
    response: Response,
) -> dict[str, Any]:
    """POST /v1/recall/list：召回列表。"""
    svc = _get_service()
    tid = _resolve_trace_id(request)
    try:
        result = svc.recall_list(
            agent_id=req.agent_id,
            query=req.query,
            module_path=req.module_path,
            consistency=req.consistency,
            task_type=req.task_type,
            asset_type=req.asset_type,
            tags=req.tags,
            top_k=req.top_k,
            trace_id=tid,
        )
    except DegradedModulePathRequiredError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("recall_list 内部错误")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    response.headers["X-Trace-Id"] = result.trace_id
    return result.to_dict()


@recall_router.post("/recall/read")
def recall_read_endpoint(
    req: RecallReadRequest,
    request: Request,
    response: Response,
) -> Response:
    """POST /v1/recall/read：读取资产内容。"""
    svc = _get_service()
    tid = _resolve_trace_id(request)
    try:
        result = svc.recall_read(
            agent_id=req.agent_id,
            asset_id=req.asset_id,
            consistency=req.consistency,
            trace_id=tid,
        )
    except AssetNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except AssetGoneError as exc:
        # 410 Gone + alternatives
        body = GoneResponseSchema(
            asset_id=exc.asset_id,
            message=f"资产 {exc.asset_id} 已删除",
            alternatives=[
                AlternativeSchema(**alt.__dict__) for alt in exc.alternatives
            ],
            trace_id=tid,
        )
        return Response(
            content=body.model_dump_json(),
            status_code=410,
            media_type="application/json",
            headers={"X-Trace-Id": tid},
        )
    except RestrictedAccessDeniedError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("recall_read 内部错误")
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    response.headers["X-Trace-Id"] = result.trace_id
    # 返回 JSON dict，FastAPI 自动序列化
    from fastapi.responses import JSONResponse

    return JSONResponse(content=result.to_dict(), headers={"X-Trace-Id": result.trace_id})


@recall_router.get("/sync/status", response_model=SyncStatusResponseSchema)
def sync_status_endpoint(response: Response) -> dict[str, Any]:
    """GET /v1/sync/status：查询同步滞后状态。"""
    svc = _get_service()
    result = svc.get_sync_status()
    tid = tracing.get_trace_id() or tracing.new_trace_id()
    response.headers["X-Trace-Id"] = tid
    return result.to_dict()


# ---------------------------------------------------------------------------
# trace_id 解析
# ---------------------------------------------------------------------------


def _resolve_trace_id(request: Request) -> str:
    """从请求头解析 trace_id，无则生成新值并写入上下文。"""
    headers = {k.lower(): v for k, v in request.headers.items()}
    tid = tracing.parse_trace_id_from_headers(headers)
    if not tid:
        tid = tracing.new_trace_id()
    tracing.set_trace_id(tid)
    return tid


__all__ = [
    "AlternativeSchema",
    "GoneResponseSchema",
    "RecallItemSchema",
    "RecallListRequest",
    "RecallListResponseSchema",
    "RecallReadRequest",
    "RecallReadResponseSchema",
    "SyncStatusResponseSchema",
    "build_router",
    "configure_recall",
    "recall_router",
]
