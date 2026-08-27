"""资产管理 API（前端界面用）。

提供按 owner / scope / type / category 过滤的资产列表、详情、共享范围修改能力。
资产写入仍走 git PR + webhook 同步路径，本模块只读 + 修改 scope。

端点：
- GET /v1/assets              资产列表（支持多维度过滤 + 分页）
- GET /v1/assets/{asset_id}   资产详情
- PATCH /v1/assets/{asset_id}/scope  修改资产共享范围
- GET /v1/members/{member_id}/stats   个人资产统计

资产图谱（关联关系）：
- GET /v1/assets/{asset_id}/links       查询资产关联（正向+反向）
- POST /v1/assets/{asset_id}/links      添加关联
- DELETE /v1/assets/{asset_id}/links/{link_id}  删除关联
- GET /v1/assets/{asset_id}/graph       多跳图遍历（depth=1~3）

ACL 精细化（restricted 资产授权）：
- GET /v1/assets/{asset_id}/acl         查询 ACL 列表
- POST /v1/assets/{asset_id}/acl        添加授权
- DELETE /v1/assets/{asset_id}/acl/{acl_id}  撤销授权
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.exc import IntegrityError

from server.binding.auth_service import AgentApiKeyService
from server.infra_db.asset_index import AssetFilter, AssetIndex
from server.infra_db.db import Database

assets_router = APIRouter(prefix="/v1", tags=["assets"])

# 模块级服务（启动时由 configure_assets_api 注入）
_DB: Database | None = None
_ASSET_INDEX: AssetIndex | None = None
_AUTH: AgentApiKeyService | None = None


def configure_assets_api(database: Database) -> None:
    """注入 Database 实例（由 FastAPI 启动事件调用）。"""
    global _DB, _ASSET_INDEX, _AUTH
    _DB = database
    _ASSET_INDEX = AssetIndex(database)
    _AUTH = AgentApiKeyService(database)


def _require() -> AssetIndex:
    if _ASSET_INDEX is None:
        raise HTTPException(status_code=503, detail="AssetIndex 服务未配置")
    return _ASSET_INDEX


def _require_db() -> Database:
    if _DB is None:
        raise HTTPException(status_code=503, detail="Database 未配置")
    return _DB


def _require_auth() -> AgentApiKeyService:
    if _AUTH is None:
        raise HTTPException(status_code=503, detail="AgentApiKeyService 未配置")
    return _AUTH


# ---------------------------------------------------------------------------
# 鉴权依赖（API Key → member_id）
# ---------------------------------------------------------------------------


def require_member(x_api_key: str = Header(default="", alias="X-API-Key")) -> str:
    """校验 X-API-Key 头，返回 member_id。

    - 缺失 / 空 key → 401
    - 无效 / 已失效 key → 401
    - 有效 key → 返回 member_id（注入到端点签名）
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key 头")
    svc = _require_auth()
    member_id = svc.lookup_member_id(x_api_key)
    if member_id is None:
        raise HTTPException(status_code=401, detail="无效或已失效的 API Key")
    return member_id


# ---------------------------------------------------------------------------
# scope 访问控制辅助
# ---------------------------------------------------------------------------


def _has_acl_grant(sess, asset_id: str, member_id: str) -> bool:
    """检查 member_id 是否被 asset 的 ACL 授权（restricted 资产用）。"""
    from server.infra_db.models import AssetAcl
    from sqlalchemy import select

    stmt = select(AssetAcl).where(
        AssetAcl.asset_id == asset_id,
        AssetAcl.grantee_type == "user",
        AssetAcl.grantee_id == member_id,
    )
    return sess.scalars(stmt).first() is not None


def _can_view_asset(row, sess, member_id: str) -> bool:
    """判断 member_id 是否可查看某资产（用于 scope 访问控制）。

    - owner == member_id → 允许（任何 scope）
    - scope ∈ {team, public} → 允许
    - scope == private → 仅 owner（已在前一条覆盖）
    - scope == restricted → 仅 ACL 授权用户
    """
    if row.owner == member_id:
        return True
    if row.scope in ("team", "public"):
        return True
    if row.scope == "restricted":
        return _has_acl_grant(sess, row.id, member_id)
    # private（非 owner）→ 拒绝
    return False


def _assert_owner(row, member_id: str) -> None:
    """断言 member_id 是资产 owner，否则 403。"""
    if row.owner != member_id:
        raise HTTPException(status_code=403, detail="仅 asset owner 可执行此操作")


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class AssetSummary(BaseModel):
    """资产摘要（列表项）。"""

    id: str
    type: str
    owner: str
    scope: str
    module_path: str = ""
    category: str | None = None
    status: str = "active"
    version: str = "0.0.1"
    tags: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    git_path: str = ""
    content_preview: str = ""  # 内容前 200 字符预览


class AssetDetail(BaseModel):
    """资产详情。"""

    id: str
    type: str
    owner: str
    scope: str
    module_path: str = ""
    category: str | None = None
    status: str = "active"
    version: str = "0.0.1"
    tags: list[str] = Field(default_factory=list)
    related_to: list[str] = Field(default_factory=list)
    created_at: str | None = None
    updated_at: str | None = None
    git_path: str = ""
    git_commit: str = ""
    content: str = ""  # 完整内容快照
    embedding_id: str | None = None


class AssetListResponse(BaseModel):
    """资产列表响应（含分页）。"""

    items: list[AssetSummary]
    total: int
    limit: int
    offset: int


class UpdateScopeRequest(BaseModel):
    """修改资产共享范围请求。"""

    scope: str = Field(..., description="新共享范围：private / team / restricted / public")


class MemberStats(BaseModel):
    """成员资产统计。"""

    member_id: str
    total: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)
    by_scope: dict[str, int] = Field(default_factory=dict)
    by_status: dict[str, int] = Field(default_factory=dict)
    by_module: dict[str, int] = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def _row_to_summary(row) -> AssetSummary:
    """ORM 行转摘要。"""
    import json

    tags: list[str] = []
    if row.tags:
        try:
            tags = json.loads(row.tags) if isinstance(row.tags, str) else row.tags
        except (json.JSONDecodeError, TypeError):
            tags = []
    preview = (row.content_snapshot or "")[:200]
    return AssetSummary(
        id=row.id,
        type=row.type,
        owner=row.owner,
        scope=row.scope,
        module_path=row.module_path or "",
        category=row.category,
        status=row.status,
        version=row.version,
        tags=tags,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        git_path=row.git_path,
        content_preview=preview,
    )


def _row_to_detail(row) -> AssetDetail:
    """ORM 行转详情。"""
    import json

    tags: list[str] = []
    if row.tags:
        try:
            tags = json.loads(row.tags) if isinstance(row.tags, str) else row.tags
        except (json.JSONDecodeError, TypeError):
            tags = []
    related: list[str] = []
    if row.related_to:
        try:
            related = (
                json.loads(row.related_to)
                if isinstance(row.related_to, str)
                else row.related_to
            )
        except (json.JSONDecodeError, TypeError):
            related = []
    return AssetDetail(
        id=row.id,
        type=row.type,
        owner=row.owner,
        scope=row.scope,
        module_path=row.module_path or "",
        category=row.category,
        status=row.status,
        version=row.version,
        tags=tags,
        related_to=related,
        created_at=row.created_at.isoformat() if row.created_at else None,
        updated_at=row.updated_at.isoformat() if row.updated_at else None,
        git_path=row.git_path,
        git_commit=row.git_commit,
        content=row.content_snapshot or "",
        embedding_id=row.embedding_id,
    )


@assets_router.get("/assets", response_model=AssetListResponse)
def list_assets(
    owner: str | None = Query(None, description="按 owner 过滤"),
    scope: str | None = Query(None, description="按 scope 过滤：private/team/restricted/public"),
    type: str | None = Query(None, description="按类型过滤：rule/memory/skill/tool/prompt"),
    category: str | None = Query(None, description="按 category 过滤"),
    module_path: str | None = Query(None, description="按 module_path 精确过滤"),
    status: str | None = Query("active", description="按状态过滤：active/superseded/deleted"),
    limit: int = Query(50, ge=1, le=500, description="每页数量"),
    offset: int = Query(0, ge=0, description="偏移量"),
    member_id: str = Depends(require_member),
) -> AssetListResponse:
    """资产列表（支持多维度过滤 + 分页）。

    - 不传 owner 时返回全部资产（共享库视图）
    - 传 owner 时返回该成员的个人资产（个人规则库视图）
    - scope=team 过滤团队共享资产

    安全：传 owner 时校验 owner 与 API Key 对应的 member_id 一致，
    防止篡改前端 localStorage 查看他人个人资产。

    scope 访问控制：
    - private 资产：仅 owner 可见
    - team / public 资产：所有认证用户可见
    - restricted 资产：仅 ACL 授权用户可见
    """
    db = _require_db()
    from server.infra_db.models import AssetIndex as AssetIndexRow
    from sqlalchemy import func, select

    # 安全：owner 参数必须与 API Key 对应的 member_id 一致
    if owner and owner != member_id:
        raise HTTPException(
            status_code=403,
            detail="无权查看其他成员的个人资产",
        )

    filter_kwargs: dict[str, Any] = {}
    if owner:
        filter_kwargs["owners"] = [owner]
    if scope:
        filter_kwargs["scopes"] = [scope]
    if type:
        filter_kwargs["types"] = [type]
    if category:
        filter_kwargs["categories"] = [category]
    if module_path:
        filter_kwargs["module_paths"] = [module_path]
    if status:
        filter_kwargs["statuses"] = [status]

    # 先查总数（不分页）
    with db.session() as sess:
        count_stmt = select(func.count()).select_from(AssetIndexRow)
        if owner:
            count_stmt = count_stmt.where(AssetIndexRow.owner == owner)
        if scope:
            count_stmt = count_stmt.where(AssetIndexRow.scope == scope)
        if type:
            count_stmt = count_stmt.where(AssetIndexRow.type == type)
        if category:
            count_stmt = count_stmt.where(AssetIndexRow.category == category)
        if module_path:
            count_stmt = count_stmt.where(AssetIndexRow.module_path == module_path)
        if status:
            count_stmt = count_stmt.where(AssetIndexRow.status == status)
        total = int(sess.scalar(count_stmt) or 0)

    # 查列表（带分页，查 limit+1 以支持分页）
    svc = _require()
    asset_filter = AssetFilter(**filter_kwargs)
    # 查询上限放大，保证 scope 过滤后仍有足够结果
    raw_rows = svc.query(asset_filter, limit=max(limit + 1, 500))

    # 手动分页 + scope 访问控制过滤
    visible_rows = []
    with db.session() as sess:
        for r in raw_rows:
            if _can_view_asset(r, sess, member_id):
                visible_rows.append(r)

    total = min(total, len(visible_rows))
    paged = visible_rows[offset : offset + limit]
    items = [_row_to_summary(r) for r in paged]
    return AssetListResponse(items=items, total=total, limit=limit, offset=offset)


@assets_router.get("/assets/{asset_id}", response_model=AssetDetail)
def get_asset(
    asset_id: str,
    member_id: str = Depends(require_member),
) -> AssetDetail:
    """获取资产详情。

    scope 访问控制：
    - 无权访问返回 404（不泄露资产存在性）
    - private：仅 owner
    - team / public：所有认证用户
    - restricted：仅 ACL 授权用户
    """
    svc = _require()
    db = _require_db()
    row = svc.get_by_id(asset_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    with db.session() as sess:
        if not _can_view_asset(row, sess, member_id):
            # 无权访问 → 404（不泄露存在性）
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
    return _row_to_detail(row)


@assets_router.patch("/assets/{asset_id}/scope")
def update_asset_scope(
    asset_id: str,
    req: UpdateScopeRequest,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """修改资产共享范围（选择性共享，仅 owner 可改）。

    - private → team：将私有资产共享给团队
    - team → private：取消共享（仅 owner 可见）
    - restricted：受限（需鉴权网关）
    - public：公开
    """
    valid_scopes = {"private", "team", "restricted", "public"}
    if req.scope not in valid_scopes:
        raise HTTPException(
            status_code=400,
            detail=f"无效 scope：{req.scope}，可选：{valid_scopes}",
        )

    db = _require_db()
    from server.infra_db.models import AssetIndex as AssetIndexRow

    with db.session() as sess:
        row = sess.get(AssetIndexRow, asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        _assert_owner(row, member_id)
        old_scope = row.scope
        row.scope = req.scope
        sess.commit()

    return {
        "asset_id": asset_id,
        "old_scope": old_scope,
        "new_scope": req.scope,
        "message": f"共享范围已从 {old_scope} 修改为 {req.scope}",
    }


@assets_router.get("/members/{member_id}/stats", response_model=MemberStats)
def get_member_stats(
    member_id: str,
    caller_member_id: str = Depends(require_member),
) -> MemberStats:
    """获取成员资产统计（按 type/scope/status/module 分组计数）。

    安全：只能查自己的统计，不能查他人。
    """
    if member_id != caller_member_id:
        raise HTTPException(
            status_code=403,
            detail="无权查看其他成员的统计",
        )
    db = _require_db()
    from server.infra_db.models import AssetIndex as AssetIndexRow
    from sqlalchemy import func, select

    with db.session() as sess:
        # 总数
        total = int(
            sess.scalar(
                select(func.count())
                .select_from(AssetIndexRow)
                .where(AssetIndexRow.owner == member_id)
            )
            or 0
        )
        if total == 0:
            return MemberStats(member_id=member_id)

        # 按 type 分组
        by_type: dict[str, int] = {}
        stmt = (
            select(AssetIndexRow.type, func.count())
            .where(AssetIndexRow.owner == member_id)
            .group_by(AssetIndexRow.type)
        )
        for t, c in sess.execute(stmt):
            by_type[t] = int(c)

        # 按 scope 分组
        by_scope: dict[str, int] = {}
        stmt = (
            select(AssetIndexRow.scope, func.count())
            .where(AssetIndexRow.owner == member_id)
            .group_by(AssetIndexRow.scope)
        )
        for s, c in sess.execute(stmt):
            by_scope[s] = int(c)

        # 按 status 分组
        by_status: dict[str, int] = {}
        stmt = (
            select(AssetIndexRow.status, func.count())
            .where(AssetIndexRow.owner == member_id)
            .group_by(AssetIndexRow.status)
        )
        for st, c in sess.execute(stmt):
            by_status[st] = int(c)

        # 按 module_path 分组（top 10）
        by_module: dict[str, int] = {}
        stmt = (
            select(AssetIndexRow.module_path, func.count())
            .where(AssetIndexRow.owner == member_id)
            .group_by(AssetIndexRow.module_path)
            .order_by(func.count().desc())
            .limit(10)
        )
        for mp, c in sess.execute(stmt):
            by_module[mp or "(未分类)"] = int(c)

    return MemberStats(
        member_id=member_id,
        total=total,
        by_type=by_type,
        by_scope=by_scope,
        by_status=by_status,
        by_module=by_module,
    )


# ---------------------------------------------------------------------------
# 资产图谱（关联关系）
# ---------------------------------------------------------------------------


class AssetLinkInfo(BaseModel):
    """资产关联信息。"""

    link_id: str
    src_asset_id: str
    dst_asset_id: str
    link_type: str
    direction: str = "outgoing"  # outgoing（正向）/ incoming（反向）
    created_at: str | None = None
    # 对端资产摘要（便于前端展示）
    peer_id: str = ""
    peer_type: str = ""
    peer_owner: str = ""
    peer_category: str | None = None
    peer_module_path: str = ""


class AssetLinksResponse(BaseModel):
    """资产关联列表响应。"""

    asset_id: str
    outgoing: list[AssetLinkInfo]  # 本资产指向他人
    incoming: list[AssetLinkInfo]  # 他人指向本资产


class CreateLinkRequest(BaseModel):
    """添加关联请求。"""

    dst_asset_id: str = Field(..., description="目标资产 ID")
    link_type: str = Field("related_to", description="关联类型：derived_from/supersedes/related_to/module_parent/triggers")


class CreateLinkResponse(BaseModel):
    link_id: str
    message: str


class GraphNode(BaseModel):
    """图节点。"""

    id: str
    type: str
    owner: str
    category: str | None = None
    module_path: str = ""


class GraphEdge(BaseModel):
    """图边。"""

    src: str
    dst: str
    link_type: str


class GraphResponse(BaseModel):
    """多跳图遍历响应。"""

    root_asset_id: str
    depth: int
    nodes: list[GraphNode]
    edges: list[GraphEdge]


def _link_to_info(row, direction: str) -> AssetLinkInfo:
    """ORM 行转关联信息（需 join 对端资产）。"""
    peer_id = row.dst_asset_id if direction == "outgoing" else row.src_asset_id
    return AssetLinkInfo(
        link_id=row.id,
        src_asset_id=row.src_asset_id,
        dst_asset_id=row.dst_asset_id,
        link_type=row.link_type,
        direction=direction,
        created_at=row.created_at.isoformat() if row.created_at else None,
        peer_id=peer_id,
    )


@assets_router.get("/assets/{asset_id}/links", response_model=AssetLinksResponse)
def get_asset_links(
    asset_id: str,
    member_id: str = Depends(require_member),
) -> AssetLinksResponse:
    """查询资产关联（正向 outgoing + 反向 incoming）。

    scope 访问控制：仅可查询自己有权查看的资产的关联。
    """
    db = _require_db()
    from server.infra_db.models import AssetLink, AssetIndex as AssetIndexRow
    from sqlalchemy import select

    with db.session() as sess:
        # 验证资产存在 + 访问权限
        row = sess.get(AssetIndexRow, asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        if not _can_view_asset(row, sess, member_id):
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")

        # 正向：本资产作为 src
        outgoing: list[AssetLinkInfo] = []
        stmt = select(AssetLink).where(AssetLink.src_asset_id == asset_id)
        for link in sess.scalars(stmt):
            info = _link_to_info(link, "outgoing")
            # 查对端摘要
            peer = sess.get(AssetIndexRow, link.dst_asset_id)
            if peer:
                info.peer_type = peer.type
                info.peer_owner = peer.owner
                info.peer_category = peer.category
                info.peer_module_path = peer.module_path or ""
            outgoing.append(info)

        # 反向：本资产作为 dst
        incoming: list[AssetLinkInfo] = []
        stmt = select(AssetLink).where(AssetLink.dst_asset_id == asset_id)
        for link in sess.scalars(stmt):
            info = _link_to_info(link, "incoming")
            peer = sess.get(AssetIndexRow, link.src_asset_id)
            if peer:
                info.peer_type = peer.type
                info.peer_owner = peer.owner
                info.peer_category = peer.category
                info.peer_module_path = peer.module_path or ""
            incoming.append(info)

    return AssetLinksResponse(asset_id=asset_id, outgoing=outgoing, incoming=incoming)


@assets_router.post("/assets/{asset_id}/links", response_model=CreateLinkResponse)
def create_asset_link(
    asset_id: str,
    req: CreateLinkRequest,
    member_id: str = Depends(require_member),
) -> CreateLinkResponse:
    """添加资产关联（仅 owner 可创建）。"""
    valid_link_types = {"derived_from", "supersedes", "related_to", "module_parent", "triggers"}
    if req.link_type not in valid_link_types:
        raise HTTPException(
            status_code=400,
            detail=f"无效 link_type：{req.link_type}，可选：{valid_link_types}",
        )

    db = _require_db()
    import uuid
    from server.infra_db.models import AssetLink, AssetIndex as AssetIndexRow

    with db.session() as sess:
        # 验证源资产存在 + owner 校验
        src_row = sess.get(AssetIndexRow, asset_id)
        if src_row is None:
            raise HTTPException(status_code=404, detail=f"源资产不存在：{asset_id}")
        _assert_owner(src_row, member_id)
        # 验证目标资产存在
        if sess.get(AssetIndexRow, req.dst_asset_id) is None:
            raise HTTPException(status_code=404, detail=f"目标资产不存在：{req.dst_asset_id}")
        if asset_id == req.dst_asset_id:
            raise HTTPException(status_code=400, detail="不能自关联")

        link_id = str(uuid.uuid4())
        link = AssetLink(
            id=link_id,
            src_asset_id=asset_id,
            dst_asset_id=req.dst_asset_id,
            link_type=req.link_type,
        )
        sess.add(link)
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"关联已存在：{asset_id} --[{req.link_type}]--> {req.dst_asset_id}",
            )

    return CreateLinkResponse(link_id=link_id, message=f"关联已添加：{asset_id} --[{req.link_type}]--> {req.dst_asset_id}")


@assets_router.delete("/assets/{asset_id}/links/{link_id}")
def delete_asset_link(
    asset_id: str,
    link_id: str,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """删除资产关联（仅 asset owner 可删除）。"""
    db = _require_db()
    from server.infra_db.models import AssetLink, AssetIndex as AssetIndexRow

    with db.session() as sess:
        # 验证源资产存在 + owner 校验
        src_row = sess.get(AssetIndexRow, asset_id)
        if src_row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        _assert_owner(src_row, member_id)
        link = sess.get(AssetLink, link_id)
        if link is None:
            raise HTTPException(status_code=404, detail=f"关联不存在：{link_id}")
        if link.src_asset_id != asset_id and link.dst_asset_id != asset_id:
            raise HTTPException(status_code=403, detail="该关联不属于此资产")
        sess.delete(link)
        sess.commit()

    return {"link_id": link_id, "message": "关联已删除"}


@assets_router.get("/assets/{asset_id}/graph", response_model=GraphResponse)
def get_asset_graph(
    asset_id: str,
    depth: int = Query(2, ge=1, le=3, description="遍历深度（1~3）"),
    member_id: str = Depends(require_member),
) -> GraphResponse:
    """多跳图遍历（BFS）。

    从 asset_id 出发，沿 asset_link 边遍历 depth 跳，返回所有节点和边。
    scope 访问控制：仅可遍历自己有权查看的资产。
    """
    db = _require_db()
    from server.infra_db.models import AssetLink, AssetIndex as AssetIndexRow
    from sqlalchemy import select

    with db.session() as sess:
        # 验证根资产 + 访问权限
        root = sess.get(AssetIndexRow, asset_id)
        if root is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        if not _can_view_asset(root, sess, member_id):
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")

        # BFS 遍历
        visited: set[str] = {asset_id}
        nodes: list[GraphNode] = [
            GraphNode(
                id=root.id,
                type=root.type,
                owner=root.owner,
                category=root.category,
                module_path=root.module_path or "",
            )
        ]
        edges: list[GraphEdge] = []
        seen_edges: set[tuple[str, str, str]] = set()
        current_layer = {asset_id}

        for _ in range(depth):
            next_layer: set[str] = set()
            for node_id in current_layer:
                # 查出边
                stmt = select(AssetLink).where(AssetLink.src_asset_id == node_id)
                for link in sess.scalars(stmt):
                    edge_key = (link.src_asset_id, link.dst_asset_id, link.link_type)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append(GraphEdge(src=link.src_asset_id, dst=link.dst_asset_id, link_type=link.link_type))
                    if link.dst_asset_id not in visited:
                        visited.add(link.dst_asset_id)
                        next_layer.add(link.dst_asset_id)
                # 查入边
                stmt = select(AssetLink).where(AssetLink.dst_asset_id == node_id)
                for link in sess.scalars(stmt):
                    edge_key = (link.src_asset_id, link.dst_asset_id, link.link_type)
                    if edge_key not in seen_edges:
                        seen_edges.add(edge_key)
                        edges.append(GraphEdge(src=link.src_asset_id, dst=link.dst_asset_id, link_type=link.link_type))
                    if link.src_asset_id not in visited:
                        visited.add(link.src_asset_id)
                        next_layer.add(link.src_asset_id)

            # 加载新节点
            for nid in next_layer:
                row = sess.get(AssetIndexRow, nid)
                if row:
                    nodes.append(
                        GraphNode(
                            id=row.id,
                            type=row.type,
                            owner=row.owner,
                            category=row.category,
                            module_path=row.module_path or "",
                        )
                    )
            current_layer = next_layer

    return GraphResponse(root_asset_id=asset_id, depth=depth, nodes=nodes, edges=edges)


# ---------------------------------------------------------------------------
# ACL 精细化（restricted 资产授权）
# ---------------------------------------------------------------------------


class AclEntry(BaseModel):
    """ACL 条目。"""

    acl_id: str
    asset_id: str
    grantee_type: str  # user / agent / role
    grantee_id: str
    permission: str  # read / execute / admin
    granted_by: str = ""
    granted_at: str | None = None


class AclListResponse(BaseModel):
    asset_id: str
    acls: list[AclEntry]


class CreateAclRequest(BaseModel):
    """添加授权请求。"""

    grantee_type: str = Field(..., description="授权对象类型：user/agent/role")
    grantee_id: str = Field(..., min_length=1, max_length=128, description="授权对象 ID（member_id / agent_id / role_name）")
    permission: str = Field("read", description="权限：read/execute/admin")
    granted_by: str = Field("", max_length=128, description="授权人（member_id）")


class CreateAclResponse(BaseModel):
    acl_id: str
    message: str


@assets_router.get("/assets/{asset_id}/acl", response_model=AclListResponse)
def get_asset_acl(
    asset_id: str,
    member_id: str = Depends(require_member),
) -> AclListResponse:
    """查询资产的 ACL 列表（restricted 资产的精准授权，仅 owner 可查）。"""
    db = _require_db()
    from server.infra_db.models import AssetAcl, AssetIndex as AssetIndexRow
    from sqlalchemy import select

    with db.session() as sess:
        row = sess.get(AssetIndexRow, asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        _assert_owner(row, member_id)

        acls: list[AclEntry] = []
        stmt = select(AssetAcl).where(AssetAcl.asset_id == asset_id)
        for row in sess.scalars(stmt):
            acls.append(
                AclEntry(
                    acl_id=row.id,
                    asset_id=row.asset_id,
                    grantee_type=row.grantee_type,
                    grantee_id=row.grantee_id,
                    permission=row.permission,
                    granted_by=row.granted_by,
                    granted_at=row.granted_at.isoformat() if row.granted_at else None,
                )
            )

    return AclListResponse(asset_id=asset_id, acls=acls)


@assets_router.post("/assets/{asset_id}/acl", response_model=CreateAclResponse)
def create_asset_acl(
    asset_id: str,
    req: CreateAclRequest,
    member_id: str = Depends(require_member),
) -> CreateAclResponse:
    """添加 ACL 授权（仅 owner 可授权）。"""
    valid_grantee_types = {"user", "agent", "role"}
    if req.grantee_type not in valid_grantee_types:
        raise HTTPException(
            status_code=400,
            detail=f"无效 grantee_type：{req.grantee_type}，可选：{valid_grantee_types}",
        )
    valid_permissions = {"read", "execute", "admin"}
    if req.permission not in valid_permissions:
        raise HTTPException(
            status_code=400,
            detail=f"无效 permission：{req.permission}，可选：{valid_permissions}",
        )
    # grantee_id 格式校验：只允许字母、数字、下划线、连字符
    import re
    if not re.match(r'^[a-zA-Z0-9_\-]+$', req.grantee_id):
        raise HTTPException(
            status_code=400,
            detail="grantee_id 只允许字母、数字、下划线、连字符",
        )

    db = _require_db()
    import uuid
    from server.infra_db.models import AssetAcl, AssetIndex as AssetIndexRow

    with db.session() as sess:
        row = sess.get(AssetIndexRow, asset_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        _assert_owner(row, member_id)

        acl_id = str(uuid.uuid4())
        acl = AssetAcl(
            id=acl_id,
            asset_id=asset_id,
            grantee_type=req.grantee_type,
            grantee_id=req.grantee_id,
            permission=req.permission,
            granted_by=req.granted_by or member_id,
        )
        sess.add(acl)
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(
                status_code=409,
                detail=f"ACL 已存在：{req.grantee_type}:{req.grantee_id} 对 {asset_id} 已授权",
            )

    return CreateAclResponse(acl_id=acl_id, message=f"已授权 {req.grantee_type}:{req.grantee_id} 对 {asset_id} 的 {req.permission} 权限")


@assets_router.delete("/assets/{asset_id}/acl/{acl_id}")
def delete_asset_acl(
    asset_id: str,
    acl_id: str,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """撤销 ACL 授权（仅 asset owner 可撤销）。"""
    db = _require_db()
    from server.infra_db.models import AssetAcl, AssetIndex as AssetIndexRow

    with db.session() as sess:
        # 验证源资产存在 + owner 校验
        src_row = sess.get(AssetIndexRow, asset_id)
        if src_row is None:
            raise HTTPException(status_code=404, detail=f"资产不存在：{asset_id}")
        _assert_owner(src_row, member_id)
        acl = sess.get(AssetAcl, acl_id)
        if acl is None:
            raise HTTPException(status_code=404, detail=f"ACL 不存在：{acl_id}")
        if acl.asset_id != asset_id:
            raise HTTPException(status_code=403, detail="该 ACL 不属于此资产")
        sess.delete(acl)
        sess.commit()

    return {"acl_id": acl_id, "message": "授权已撤销"}


__all__ = ["assets_router", "configure_assets_api"]
