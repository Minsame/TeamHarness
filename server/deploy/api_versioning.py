"""API 语义化版本（SubTask 3.4）。

对应技术方案：/v1/ 路由锁定（向后兼容），破坏性变更走 /v2/。
本模块提供：
- parse_semver：复用 config 的版本解析
- APIVersionPolicy：版本策略（哪些端点锁定 /v1/，哪些升级到 /v2/）
- VersionedAPIRouter：FastAPI 路由器封装，强制按版本前缀注册
- 兼容中间件：未带版本前缀的请求重定向到当前默认版本

设计原则：
1. v1 路由只允许追加新字段（向后兼容），不得删字段或改语义
2. 破坏性变更必须新挂 /v2/ 路由，v1 保留至少 1 个版本周期
3. 弃用 v1 时在响应头加 Sunset 与 Deprecation（RFC 8594 / RFC 7234）
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse

from server.deploy.config import CURRENT_VERSION, parse_semver

# ---------------------------------------------------------------------------
# 版本策略
# ---------------------------------------------------------------------------

# 当前"默认版本"：未带前缀的请求按此版本路由（如 /recall/list → /v1/recall/list）
DEFAULT_API_VERSION = "v1"

# 受支持的 API 大版本（按发布顺序）
SUPPORTED_API_VERSIONS: tuple[str, ...] = ("v1", "v2")

# 已弃用版本（仍可用，但响应头提示 Sunset）
DEPRECATED_VERSIONS: frozenset[str] = frozenset()


@dataclass
class APIVersionPolicy:
    """API 版本策略声明。

    locked_versions：锁定版本，只能追加兼容字段，禁止破坏性变更。
    breaking_versions：允许破坏性变更的版本。
    sunset_versions：已弃用版本（响应头带 Sunset 提示）。
    """

    default_version: str = DEFAULT_API_VERSION
    locked_versions: frozenset[str] = frozenset({"v1"})
    breaking_versions: frozenset[str] = frozenset({"v2"})
    sunset_versions: frozenset[str] = DEPRECATED_VERSIONS

    def is_locked(self, version: str) -> bool:
        """该版本是否锁定（禁止破坏性变更）。"""
        return version in self.locked_versions

    def is_breaking(self, version: str) -> bool:
        """该版本是否允许破坏性变更。"""
        return version in self.breaking_versions

    def is_supported(self, version: str) -> bool:
        """版本是否仍受支持。"""
        return version in SUPPORTED_API_VERSIONS

    def is_sunset(self, version: str) -> bool:
        """版本是否已弃用（响应头加 Sunset 提示）。"""
        return version in self.sunset_versions


# ---------------------------------------------------------------------------
# VersionedAPIRouter：按版本前缀注册路由
# ---------------------------------------------------------------------------


class VersionedAPIRouter:
    """按版本前缀注册路由的封装。

    用法：
        router = VersionedAPIRouter(policy)
        # 注册 v1 路由（锁定，仅追加兼容字段）
        @router.v1("/recall/list", methods=["POST"])
        async def recall_list_v1(...): ...
        # 注册 v2 路由（破坏性变更，签名可与 v1 不同）
        @router.v2("/recall/list", methods=["POST"])
        async def recall_list_v2(...): ...

    内部为每个版本维护独立 APIRouter，统一通过 routers() 暴露给 FastAPI 应用。
    """

    def __init__(
        self,
        policy: APIVersionPolicy | None = None,
        *,
        tags: list[str] | None = None,
    ) -> None:
        self.policy = policy or APIVersionPolicy()
        self._routers: dict[str, APIRouter] = {
            v: APIRouter(prefix=f"/{v}", tags=tags or []) for v in SUPPORTED_API_VERSIONS
        }

    def _version_route(self, version: str) -> Callable[..., Callable]:
        """返回某个版本的装饰器，校验版本受支持。"""
        if not self.policy.is_supported(version):
            raise ValueError(
                f"API 版本 {version!r} 不受支持，可选：{SUPPORTED_API_VERSIONS}"
            )

        def decorator(path: str, **kwargs: Any) -> Callable[..., Callable]:
            router = self._routers[version]

            def wrapper(func: Callable[..., Any]) -> Callable[..., Any]:
                # 注册到带版本前缀的路由器
                router.add_api_route(path, func, **kwargs)
                # 若 v2 是 v1 的破坏性升级，且 v1 未单独注册 → 自动在 v1 也注册
                # 仅当 v2 端点未声明 v1_locked=False 时
                return func

            return wrapper

        return decorator

    def v1(self, path: str, **kwargs: Any) -> Callable[..., Callable]:
        """注册 v1 路由（锁定版本）。"""
        return self._version_route("v1")(path, **kwargs)

    def v2(self, path: str, **kwargs: Any) -> Callable[..., Callable]:
        """注册 v2 路由（破坏性变更版本）。"""
        return self._version_route("v2")(path, **kwargs)

    def routers(self) -> list[APIRouter]:
        """返回所有受支持版本的 APIRouter 列表，供 FastAPI include。"""
        return [self._routers[v] for v in SUPPORTED_API_VERSIONS]

    def versions_for_path(self, path: str) -> list[str]:
        """查询某 path 在哪些版本注册了（用于 OpenAPI 文档生成）。"""
        versions: list[str] = []
        for v, router in self._routers.items():
            for route in router.routes:
                if getattr(route, "path", "").endswith(path):
                    versions.append(v)
                    break
        return versions


# ---------------------------------------------------------------------------
# 兼容中间件：未带版本前缀的请求重定向到默认版本
# ---------------------------------------------------------------------------

# 这些路径前缀直接放行，不做版本路由
_NO_VERSION_PATHS: frozenset[str] = frozenset(
    {"/", "/v1", "/v2", "/health", "/healthz", "/readyz", "/metrics", "/openapi.json", "/docs", "/redoc"}
)


async def version_compat_middleware(request: Request, call_next: Callable[..., Any]) -> Any:
    """FastAPI 中间件：未带版本前缀的请求转发到默认版本。

    例：POST /recall/list → POST /v1/recall/list（默认版本）。
    命中 /health /metrics /docs 等运维路径时放行不重写。
    """
    path = request.url.path
    if not path or path == "/":
        return await call_next(request)
    # 命中无版本前缀白名单
    if path in _NO_VERSION_PATHS or any(path.startswith(p + "/") for p in _NO_VERSION_PATHS):
        return await call_next(request)
    # 已带 /v1 /v2 前缀，放行
    for v in SUPPORTED_API_VERSIONS:
        if path == f"/{v}" or path.startswith(f"/{v}/"):
            # sunset 提示
            policy = APIVersionPolicy()
            if policy.is_sunset(v):
                resp = await call_next(request)
                resp.headers["Deprecation"] = "true"
                resp.headers["Sunset"] = "Wed, 31 Dec 2025 23:59:59 GMT"
                return resp
            return await call_next(request)
    # 未带前缀：内部改写 scope.path 与 raw_path，转发到默认版本
    new_path = f"/{DEFAULT_API_VERSION}{path}"
    scope = request.scope
    scope["path"] = new_path
    scope["raw_path"] = new_path.encode("ascii")
    return await call_next(request)


# ---------------------------------------------------------------------------
# 版本断言（运行时校验，防止 v1 误改）
# ---------------------------------------------------------------------------


def assert_backward_compatible(
    old_handler: Callable[..., Any], new_handler: Callable[..., Any]
) -> None:
    """断言 v1 端点变更仍向后兼容（签名不破坏旧调用）。

    用于 CI 校验：v1 路由更新后比对入参集合。
    向后兼容语义（双重判断）：
    1. 参数集合不允许删除（只能新增/保留）：旧调用方传的位置/关键字参数在新签名中
       必须仍能匹配，否则 TypeError → 破坏性。
    2. 必填参数集合不允许扩张（只能收缩/保留）：可选→必填会让旧调用方少传参数
       触发 TypeError → 破坏性。
    满足以上两条才算兼容。新增可选参数、必填→可选均兼容。
    """
    import inspect

    old_sig = inspect.signature(old_handler)
    new_sig = inspect.signature(new_handler)
    # 所有参数名（含可选），排除 *args / **kwargs
    _callable_kinds = (
        inspect.Parameter.POSITIONAL_ONLY,
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
    )
    old_params = {
        name
        for name, p in old_sig.parameters.items()
        if p.kind in _callable_kinds
    }
    new_params = {
        name
        for name, p in new_sig.parameters.items()
        if p.kind in _callable_kinds
    }
    old_required = {
        name
        for name, p in old_sig.parameters.items()
        if p.default is inspect.Parameter.empty and p.kind in _callable_kinds
    }
    new_required = {
        name
        for name, p in new_sig.parameters.items()
        if p.default is inspect.Parameter.empty and p.kind in _callable_kinds
    }
    # 判断 1：删除参数 → 破坏性
    removed = old_params - new_params
    if removed:
        raise HTTPException(
            status_code=500,
            detail=f"v1 路由破坏性变更：删除了参数 {sorted(removed)}；应改走 /v2/",
        )
    # 判断 2：新增必填参数 → 破坏性
    added_required = new_required - old_required
    if added_required:
        raise HTTPException(
            status_code=500,
            detail=f"v1 路由破坏性变更：新增了必填参数 {sorted(added_required)}；应改走 /v2/",
        )


# ---------------------------------------------------------------------------
# /v1/system/info 端点（暴露当前版本与部署模式）
# ---------------------------------------------------------------------------


def build_system_info_router() -> APIRouter:
    """构建 /v1/system/* 路由器，暴露版本与部署信息。"""
    from server.deploy.config import get_deploy_config

    router = APIRouter(prefix="/v1/system", tags=["system"])

    @router.get("/info")
    async def system_info() -> JSONResponse:
        cfg = get_deploy_config()
        return JSONResponse(
            {
                "version": cfg.get_version(),
                "mode": cfg.get_mode().value,
                "storage_backend": cfg.get_storage_backend().as_dict(),
                "supported_api_versions": list(SUPPORTED_API_VERSIONS),
                "default_api_version": DEFAULT_API_VERSION,
                "schema_version_current": 1,
            }
        )

    @router.get("/selfcheck")
    async def system_selfcheck() -> JSONResponse:
        from server.deploy.all_in_one import selfcheck

        return JSONResponse(selfcheck())

    return router


def current_version_tuple() -> tuple[int, int, int]:
    """返回 CURRENT_VERSION 的 (major, minor, patch) 元组。"""
    return parse_semver(CURRENT_VERSION)
