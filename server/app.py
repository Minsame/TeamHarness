"""TeamHarness 服务端统一入口。

按 --role 启动不同 FastAPI app，聚合对应模块的 router。

Usage:
    python -m server.app --role asset  --host 0.0.0.0 --port 8080
    python -m server.app --role recall --host 0.0.0.0 --port 8081
    python -m server.app --role distill --host 0.0.0.0 --port 8082
"""

from __future__ import annotations

import argparse
import logging
import os
import sys

logger = logging.getLogger("server.app")


# ---------------------------------------------------------------------------
# 各 role 的 router 注册表
# ---------------------------------------------------------------------------

def _register_asset_routers(app) -> None:
    """asset 角色路由：webhook / binding / governance / assets / team / system。"""
    from server.infra_git.webhook import router as webhook_router
    from server.binding.api import (
        auth_router,
        binding_router,
        category_router,
        tool_router,
    )
    from server.governance.metrics import governance_router
    from server.assets.api import assets_router
    from server.team.api import team_router
    from server.comm.api import comm_router, well_known_router

    app.include_router(webhook_router)
    app.include_router(binding_router)
    app.include_router(category_router)
    app.include_router(auth_router)
    app.include_router(tool_router)
    app.include_router(governance_router)
    app.include_router(assets_router)
    app.include_router(team_router)
    app.include_router(comm_router)
    # A2A 公开发现端点（无鉴权）：GET /.well-known/agent-card.json
    app.include_router(well_known_router)
    logger.info("asset role: webhook + binding + governance + assets + team + comm + well-known routers registered")


def _register_recall_routers(app) -> None:
    """recall 角色路由：recall / system。"""
    from server.recall.api import recall_router

    app.include_router(recall_router)
    logger.info("recall role: recall router registered")


def _register_distill_routers(app) -> None:
    """distill 角色路由：llm / system。"""
    from server.distill_personal.api import llm_router

    app.include_router(llm_router)
    logger.info("distill role: llm router registered")


def _register_system_router(app) -> None:
    """所有角色共用的 system 路由（/v1/system/info, /v1/system/selfcheck）。"""
    from server.deploy.api_versioning import build_system_info_router

    app.include_router(build_system_info_router())


ROLE_ROUTERS = {
    "asset": _register_asset_routers,
    "recall": _register_recall_routers,
    "distill": _register_distill_routers,
}


# ---------------------------------------------------------------------------
# DB 初始化（Docker 环境下尝试连接 PostgreSQL）
# ---------------------------------------------------------------------------

def _init_db_if_configured():
    """如果 DATABASE_URL 存在，创建 Database 实例并初始化 schema。

    返回 Database 实例（失败返回 None，服务仍可启动但 API 返回 503）。
    """
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        logger.info("DATABASE_URL 未设置，跳过 DB 初始化")
        return None

    try:
        from server.infra_db.db import create_database
        from server.infra_db.schema_initializer import init_schema

        # 导入所有 model 模块，确保 Base.metadata 包含全部表定义
        import server.infra_db.models  # noqa: F401
        import server.binding.models  # noqa: F401
        import server.governance.models  # noqa: F401
        import server.distill_team.models  # noqa: F401
        import server.common.models  # noqa: F401
        import server.comm.models  # noqa: F401

        db = create_database()
        init_schema(db.sync_engine)
        logger.info("数据库 schema 初始化完成: %s", db_url.split("@")[-1])
        return db
    except Exception as exc:
        logger.warning("数据库初始化失败（服务仍可启动，API 将返回 503）: %s", exc)
        return None


# ---------------------------------------------------------------------------
# 服务注入（各 role 按需注入 Service 实例）
# ---------------------------------------------------------------------------

def _configure_services(role: str, db) -> None:
    """按 role 注入 Service 实例到各 API 模块的全局变量。"""
    if db is None:
        logger.warning("Database 未初始化，跳过服务注入（API 将返回 503）")
        return

    if role == "asset":
        _configure_asset_services(db)
    elif role == "recall":
        _configure_recall_services(db)
    elif role == "distill":
        _configure_distill_services(db)


def _configure_asset_services(db) -> None:
    """注入 asset 角色的全部 Service（binding + governance + webhook）。"""
    # --- binding 域 ---
    try:
        from server.binding.api import configure_binding_api
        from server.binding.auth_service import AgentApiKeyService
        from server.binding.binding_service import BindingService
        from server.binding.category_suggest import CategorySuggestService
        from server.binding.tool_review import ToolReviewService

        configure_binding_api(
            binding=BindingService(db),
            category=CategorySuggestService(db),
            auth=AgentApiKeyService(db),
            tool=ToolReviewService(db),
        )
        logger.info("binding 服务注入完成")
    except Exception as exc:
        logger.warning("binding 服务注入失败: %s", exc)

    # --- governance 域 ---
    try:
        from server.governance.metrics import configure_governance, GovernanceMetrics
        from server.governance.dashboard import DashboardService

        configure_governance(
            GovernanceMetrics(db),
            dashboard=DashboardService(db),
        )
        logger.info("governance 服务注入完成")
    except Exception as exc:
        logger.warning("governance 服务注入失败: %s", exc)

    # --- webhook ---
    try:
        from server.infra_git.webhook import configure_webhook

        secret = os.environ.get("WEBHOOK_SECRET", "")
        configure_webhook(secret=secret)
        logger.info("webhook 配置注入完成")
    except Exception as exc:
        logger.warning("webhook 配置注入失败: %s", exc)

    # --- assets 域（前端界面用） ---
    try:
        from server.assets.api import configure_assets_api

        configure_assets_api(db)
        logger.info("assets 服务注入完成")
    except Exception as exc:
        logger.warning("assets 服务注入失败: %s", exc)

    # --- team 域（团队与成员管理） ---
    try:
        from server.team.api import configure_team_api

        configure_team_api(db)
        logger.info("team 服务注入完成")
    except Exception as exc:
        logger.warning("team 服务注入失败: %s", exc)

    # --- comm 域（成员 AI 通信 / 影子通信） ---
    try:
        from server.comm.api import configure_comm_api

        configure_comm_api(db)
        logger.info("comm 服务注入完成")
    except Exception as exc:
        logger.warning("comm 服务注入失败: %s", exc)


def _configure_recall_services(db) -> None:
    """注入 recall 角色的 RecallService（消除 R036 双轨制反模式）。

    构造完整依赖链并调用 configure_recall(svc)，与生产路径一致。
    依赖从环境变量配置：
    - EMBEDDING_ACTIVE_VERSION / EMBEDDING_SHADOW_VERSION / EMBEDDING_DIM
    - GIT_PROVIDER (gitlab / gitea / libgit2，默认 libgit2)
    - GIT_REPO_PATH / GITEA_BASE_URL / GITLAB_BASE_URL 等
    - REPO_URL（可选）

    依赖构造失败时降级为跳过（保留端点 503 行为，但日志明确说明原因）。
    """
    try:
        from pathlib import Path

        from server.infra_db.asset_index import AssetIndex
        from server.infra_db.counts_check import CountsChecker
        from server.infra_db.embedding import EmbeddingService
        from server.infra_db.sync import SyncService
        from server.infra_db.vectorstore import InMemoryVectorStore
        from server.infra_git.git_provider import create_git_provider
        from server.infra_git.restricted import create_restricted_reader
        from server.recall.api import configure_recall
        from server.recall.service import RecallService

        # 1. embedding_service（默认哈希 embedding，生产由 Agent 7 LLMProvider 注入）
        embedding_service = EmbeddingService()

        # 2. vector_store（默认 InMemory，生产 docker-compose 用 Qdrant/PGVector）
        vector_store = InMemoryVectorStore()

        # 3. asset_index（注入 embedding 版本配置）
        asset_index = AssetIndex(
            db,
            active_embedding_version=embedding_service.get_active_version(),
            shadow_embedding_version=embedding_service.get_shadow_version(),
        )

        # 4. counts_checker
        counts_checker = CountsChecker(db)

        # 5. git_provider（优先环境变量配置）
        git_provider = create_git_provider()

        # 6. repo_root / repo_url
        repo_root = os.environ.get("GIT_REPO_PATH", ".")
        repo_url = os.environ.get("REPO_URL", "")

        # 7. sync_service
        sync_service = SyncService(
            database=db,
            git_provider=git_provider,
            asset_index=asset_index,
            embedding_service=embedding_service,
            counts_checker=counts_checker,
            repo_root=repo_root,
        )

        # 8. restricted_reader
        restricted_reader = create_restricted_reader(Path(repo_root))

        # 9. head_resolver（从环境变量读取，未配置时返回空字符串）
        def _head_resolver() -> str:
            return os.environ.get("GIT_HEAD_SHA", "")

        # 10. RecallService + 注入（与测试侧 build_router(svc) 共用同一 Service 类）
        svc = RecallService(
            database=db,
            asset_index=asset_index,
            embedding_service=embedding_service,
            sync_service=sync_service,
            vector_store=vector_store,
            git_provider=git_provider,
            repo_root=repo_root,
            restricted_reader=restricted_reader,
            head_resolver=_head_resolver,
            repo_url=repo_url,
            offline_root=repo_root,
        )
        configure_recall(svc)
        logger.info(
            "recall 服务注入完成（embedding_version=%s, vector_store=%s, git_provider=%s）",
            embedding_service.get_active_version(),
            type(vector_store).__name__,
            type(git_provider).__name__,
        )
    except Exception as exc:
        # 依赖缺失（如 pygit2 未装、GITEA_BASE_URL 未配）时降级为跳过
        logger.warning("recall 服务注入失败（端点将返回 503）: %s", exc)


def _configure_distill_services(db) -> None:
    """注入 distill 角色的 Service（LLMProvider + BudgetManager）。

    通过 LLM_PROVIDER 环境变量选择 provider 类型：
    - "openai"（默认）：OpenAI 兼容格式（OpenAI/DeepSeek/Moonshot/通义千问/vLLM）
    - "anthropic"：Anthropic Claude Messages API 格式

    依赖环境变量：
    - LLM_PROVIDER：provider 类型（openai/anthropic，默认 openai）
    - LLM_BASE_URL：API 端点
    - LLM_API_KEY：API 密钥
    - LLM_MODEL：模型名

    未配置 LLM_BASE_URL/LLM_API_KEY 时降级为 MockLLMProvider（返回固定 JSON，用于流程验证）。
    """
    from server.distill_personal.api import configure_distill_api
    from server.distill_personal.budget import BudgetManager
    from server.distill_personal.llm_provider import create_llm_provider

    base_url = os.environ.get("LLM_BASE_URL", "").strip()
    api_key = os.environ.get("LLM_API_KEY", "").strip()
    model = os.environ.get("LLM_MODEL", "").strip()
    provider_type = os.environ.get("LLM_PROVIDER", "openai").strip().lower()

    if base_url and api_key:
        llm_provider = create_llm_provider(
            provider=provider_type,
            base_url=base_url,
            api_key=api_key,
            model=model or None,
        )
        logger.info(
            "distill LLMProvider 配置完成: provider=%s, base_url=%s, model=%s",
            provider_type, base_url, model,
        )
    else:
        # 降级：MockLLMProvider，返回固定 JSON，用于流程验证
        llm_provider = _MockLLMProvider()
        logger.warning(
            "distill LLM_BASE_URL/LLM_API_KEY 未配置，降级为 MockLLMProvider"
            "（返回固定响应，仅用于流程验证）"
        )

    budget_mgr = BudgetManager(default_daily_budget=100_000)
    configure_distill_api(llm_provider=llm_provider, budget_mgr=budget_mgr)
    logger.info("distill 服务注入完成（BudgetManager default_daily_budget=100_000）")


class _MockLLMProvider:
    """Mock LLM Provider（未配置真实 LLM 时降级使用）。

    返回固定 JSON 响应，满足 schema 校验，用于端到端流程验证。
    实现 LLMChatLike 协议（chat 方法）。
    """

    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        schema: dict | None = None,
        model: str | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> dict:
        # 有 schema 时返回 skip=true 的空资产（让 Deep 阶段正常走 SKIP 分支）
        if schema is not None:
            import json

            return {
                "content": json.dumps({
                    "skip": True,
                    "asset": {},
                    "confidence": 0.0,
                }),
                "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
                "model": "mock-llm",
            }
        # 无 schema 时返回固定文本
        return {
            "content": "[MockLLM] LLM_BASE_URL 未配置，返回固定响应",
            "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
            "model": "mock-llm",
        }


# ---------------------------------------------------------------------------
# FastAPI app 构建
# ---------------------------------------------------------------------------

def create_app(role: str):
    """创建 FastAPI app 并按 role 注册路由。"""
    from fastapi import FastAPI
    from fastapi.middleware.cors import CORSMiddleware

    if role not in ROLE_ROUTERS:
        raise ValueError(f"未知 role: {role}，可选: {list(ROLE_ROUTERS.keys())}")

    app = FastAPI(
        title=f"TeamHarness {role}-service",
        version="1.0.0",
        description=f"TeamHarness {role} 服务（docker-compose 部署）",
    )

    # CORS 中间件：允许跨域访问（同源时无副作用，跨域时放行预检）
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # 注册 role 专属路由
    ROLE_ROUTERS[role](app)

    # 所有角色都注册 system 路由
    _register_system_router(app)

    # 根路径健康检查
    @app.get("/healthz")
    async def healthz():
        return {"status": "ok", "role": role}

    logger.info("FastAPI app 创建完成 (role=%s)", role)
    return app


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="TeamHarness 服务端启动")
    parser.add_argument(
        "--role",
        required=True,
        choices=list(ROLE_ROUTERS.keys()),
        help="服务角色",
    )
    parser.add_argument("--host", default="0.0.0.0", help="监听地址")
    parser.add_argument("--port", type=int, default=8080, help="监听端口")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    logger.info("启动 TeamHarness %s-service (host=%s, port=%d)", args.role, args.host, args.port)

    # 初始化 DB + 注入服务（失败不阻塞启动，API 返回 503）
    db = _init_db_if_configured()
    _configure_services(args.role, db)

    app = create_app(args.role)

    import uvicorn

    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
