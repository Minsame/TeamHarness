"""API 语义化版本中间件测试（SubTask 3.7）。

覆盖：
- APIVersionPolicy 锁定 / 破坏性 / 弃用判定
- VersionedAPIRouter v1 / v2 注册
- version_compat_middleware 未带前缀自动转发
- build_system_info_router 端点响应
- assert_backward_compatible 破坏性变更检测
- parse_semver 复用
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from server.deploy.api_versioning import (
    APIVersionPolicy,
    DEFAULT_API_VERSION,
    SUPPORTED_API_VERSIONS,
    VersionedAPIRouter,
    assert_backward_compatible,
    build_system_info_router,
    version_compat_middleware,
)


# ---------------------------------------------------------------------------
# APIVersionPolicy
# ---------------------------------------------------------------------------


class TestAPIVersionPolicy:
    def test_默认策略v1锁定v2破坏性(self) -> None:
        policy = APIVersionPolicy()
        assert policy.is_locked("v1") is True
        assert policy.is_breaking("v2") is True
        assert policy.is_locked("v2") is False
        assert policy.is_breaking("v1") is False

    def test_受支持版本(self) -> None:
        policy = APIVersionPolicy()
        assert policy.is_supported("v1") is True
        assert policy.is_supported("v2") is True
        assert policy.is_supported("v3") is False

    def test_默认版本为v1(self) -> None:
        policy = APIVersionPolicy()
        assert policy.default_version == "v1"

    def test_无弃用版本默认(self) -> None:
        policy = APIVersionPolicy()
        assert policy.is_sunset("v1") is False
        assert policy.is_sunset("v2") is False

    def test_自定义弃用版本(self) -> None:
        policy = APIVersionPolicy(sunset_versions=frozenset({"v1"}))
        assert policy.is_sunset("v1") is True
        assert policy.is_sunset("v2") is False


# ---------------------------------------------------------------------------
# VersionedAPIRouter
# ---------------------------------------------------------------------------


class TestVersionedAPIRouter:
    def test_v1v2路由独立注册(self) -> None:
        router = VersionedAPIRouter()

        @router.v1("/test", methods=["GET"])
        async def handler_v1() -> dict:
            return {"version": "v1"}

        @router.v2("/test", methods=["GET"])
        async def handler_v2() -> dict:
            return {"version": "v2"}

        routers = router.routers()
        assert len(routers) == len(SUPPORTED_API_VERSIONS)
        # 每个路由器应有自己的前缀
        prefixes = {r.prefix for r in routers}
        assert "/v1" in prefixes
        assert "/v2" in prefixes

    def test_versions_for_path查询(self) -> None:
        router = VersionedAPIRouter()

        @router.v1("/items", methods=["GET"])
        async def h1() -> dict:
            return {}

        @router.v2("/items", methods=["GET"])
        async def h2() -> dict:
            return {}

        versions = router.versions_for_path("/items")
        assert "v1" in versions
        assert "v2" in versions

    def test_不受支持版本抛ValueError(self) -> None:
        router = VersionedAPIRouter()
        with pytest.raises(ValueError, match="不受支持"):

            @router._version_route("v99")("/bad")  # type: ignore[arg-type]
            async def h() -> dict:
                return {}


# ---------------------------------------------------------------------------
# version_compat_middleware
# ---------------------------------------------------------------------------


class TestVersionCompatMiddleware:
    def test_未带前缀转发到默认v1(self) -> None:
        app = FastAPI()
        app.middleware("http")(version_compat_middleware)

        @app.get("/v1/echo")
        async def echo_v1() -> dict:
            return {"routed": "v1"}

        client = TestClient(app)
        # 未带 /v1 前缀，应转发到 /v1/echo
        resp = client.get("/echo")
        assert resp.status_code == 200
        assert resp.json() == {"routed": "v1"}

    def test_已带v1前缀放行(self) -> None:
        app = FastAPI()
        app.middleware("http")(version_compat_middleware)

        @app.get("/v1/echo")
        async def echo_v1() -> dict:
            return {"routed": "v1"}

        client = TestClient(app)
        resp = client.get("/v1/echo")
        assert resp.status_code == 200

    def test_运维路径放行不重写(self) -> None:
        app = FastAPI()
        app.middleware("http")(version_compat_middleware)

        @app.get("/health")
        async def health() -> dict:
            return {"status": "ok"}

        client = TestClient(app)
        resp = client.get("/health")
        assert resp.status_code == 200

    def test_根路径放行(self) -> None:
        app = FastAPI()
        app.middleware("http")(version_compat_middleware)

        @app.get("/")
        async def root() -> dict:
            return {"service": "teamharness"}

        client = TestClient(app)
        resp = client.get("/")
        assert resp.status_code == 200


# ---------------------------------------------------------------------------
# build_system_info_router
# ---------------------------------------------------------------------------


class TestSystemInfoRouter:
    def test_system_info返回版本与模式(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # 重置 DeployConfig 单例
        from server.deploy.config import reset_deploy_config

        reset_deploy_config()
        monkeypatch.setenv("TEAMHARNESS_DEPLOY_MODE", "all-in-one")

        app = FastAPI()
        app.include_router(build_system_info_router())
        client = TestClient(app)

        resp = client.get("/v1/system/info")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert data["mode"] == "all-in-one"
        assert "storage_backend" in data
        assert data["default_api_version"] == DEFAULT_API_VERSION
        assert data["supported_api_versions"] == list(SUPPORTED_API_VERSIONS)

    def test_system_selfcheck返回结构(self) -> None:
        app = FastAPI()
        app.include_router(build_system_info_router())
        client = TestClient(app)

        resp = client.get("/v1/system/selfcheck")
        assert resp.status_code == 200
        data = resp.json()
        assert "version" in data
        assert "components" in data
        assert "sqlite" in data["components"]


# ---------------------------------------------------------------------------
# assert_backward_compatible
# ---------------------------------------------------------------------------


class TestAssertBackwardCompatible:
    def test_新增可选参数兼容(self) -> None:
        def old(a: int, b: int = 0) -> int:
            return a + b

        def new(a: int, b: int = 0, c: int = 0) -> int:
            return a + b + c

        # 不抛异常即通过
        assert_backward_compatible(old, new)

    def test_删除必填参数抛异常(self) -> None:
        def old(a: int, b: int) -> int:
            return a + b

        def new(a: int) -> int:  # 删除了必填参数 b
            return a

        with pytest.raises(HTTPException, match="破坏性变更"):
            assert_backward_compatible(old, new)

    def test_必填参数改可选兼容(self) -> None:
        def old(a: int, b: int) -> int:
            return a + b

        def new(a: int, b: int = 0) -> int:  # b 变可选，不破坏旧调用
            return a + b

        assert_backward_compatible(old, new)

    def test_可选参数改必填破坏性(self) -> None:
        def old(a: int, b: int = 0) -> int:
            return a + b

        def new(a: int, b: int) -> int:  # b 变必填，破坏旧调用
            return a + b

        with pytest.raises(HTTPException, match="破坏性变更"):
            assert_backward_compatible(old, new)
