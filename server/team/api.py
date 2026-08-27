"""团队管理 API。

提供成员管理、团队管理、团队成员管理、团队树查询能力。

端点：
- 成员管理
    GET    /v1/team/members                    列出所有成员（需 admin）
    POST   /v1/team/members                    添加成员（需 admin）
    GET    /v1/team/members/{member_id}        查看成员详情
    PATCH  /v1/team/members/{member_id}        修改成员（需 admin）
    DELETE /v1/team/members/{member_id}        删除成员（需 admin，不能删自己）

- 团队管理
    GET    /v1/team/teams                      列出所有团队
    GET    /v1/team/teams/tree                 获取完整团队树（嵌套 JSON）
    GET    /v1/team/teams/{team_id}            查看团队详情
    POST   /v1/team/teams                      创建顶层团队（需 admin）
    POST   /v1/team/teams/{team_id}/sub        创建子团队（需 team admin）
    PATCH  /v1/team/teams/{team_id}            修改团队（需 team admin）
    DELETE /v1/team/teams/{team_id}            删除团队（需 team admin，级联删除）
    GET    /v1/team/teams/{team_id}/subtree    获取某团队的子树

- 团队成员管理
    GET    /v1/team/teams/{team_id}/members    列出团队成员
    POST   /v1/team/teams/{team_id}/members    添加成员到团队（需 team admin）
    DELETE /v1/team/teams/{team_id}/members/{member_id}  从团队移除成员（需 team admin）

鉴权规则：
- 系统 admin：members.role=admin，可管理所有团队和成员
- 团队 admin：team_members.role=admin（该团队的），可管理该团队的成员和子团队
- 普通成员：只能查看团队树和自己所在的团队
"""

from __future__ import annotations

import json
import uuid
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from server.binding.auth_service import AgentApiKeyService
from server.infra_db.db import Database
from server.infra_db.models import Member, Team, TeamMember

team_router = APIRouter(prefix="/v1/team", tags=["team"])

# 模块级服务（启动时由 configure_team_api 注入，是唯一的注入路径）
_DB: Database | None = None
_AUTH: AgentApiKeyService | None = None


def configure_team_api(database: Database) -> None:
    """注入 Database 实例（由 FastAPI 启动事件调用）。

    这是本模块唯一的 Service 注入路径——生产与测试均通过此函数注入，
    不存在 build_router(svc) 旁路，避免双轨制（参见 gotchas.md）。
    """
    global _DB, _AUTH
    _DB = database
    _AUTH = AgentApiKeyService(database)


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
    - 首次调用且 members 表为空 → 自动将当前用户创建为 admin
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key 头")
    svc = _require_auth()
    member_id = svc.lookup_member_id(x_api_key)
    if member_id is None:
        raise HTTPException(status_code=401, detail="无效或已失效的 API Key")
    # 首次调用：members 表为空时，自动将当前用户创建为 admin
    _ensure_bootstrap_admin(member_id)
    return member_id


def _ensure_bootstrap_admin(member_id: str) -> None:
    """首次调用：如果 members 表为空，自动将当前用户创建为 admin。"""
    db = _require_db()
    with db.session() as sess:
        count = sess.scalar(select(func.count()).select_from(Member))
        if count == 0:
            sess.add(
                Member(
                    member_id=member_id,
                    display_name=member_id,
                    role="admin",
                    status="active",
                    tags=json.dumps(["admin"], ensure_ascii=False),
                    created_by="system",
                )
            )
            sess.commit()


# ---------------------------------------------------------------------------
# 鉴权辅助（系统 admin / 团队 admin 校验）
# ---------------------------------------------------------------------------


def _require_system_admin(sess, member_id: str) -> Member:
    """断言 member_id 是系统 admin，否则 403。返回 Member 行。"""
    row = sess.get(Member, member_id)
    if row is None:
        raise HTTPException(status_code=404, detail=f"成员不存在：{member_id}")
    if row.role != "admin":
        raise HTTPException(status_code=403, detail="需要系统 admin 权限")
    return row


def _require_team_admin(sess, team_id: str, member_id: str) -> Team:
    """断言 member_id 是 team_id 的 admin 或系统 admin，否则 403。返回 Team 行。"""
    team = sess.get(Team, team_id)
    if team is None:
        raise HTTPException(status_code=404, detail=f"团队不存在：{team_id}")
    # 系统 admin 直接通过
    member = sess.get(Member, member_id)
    if member is not None and member.role == "admin":
        return team
    # 团队 admin
    tm = sess.scalars(
        select(TeamMember).where(
            TeamMember.team_id == team_id,
            TeamMember.member_id == member_id,
        )
    ).first()
    if tm is None or tm.role != "admin":
        raise HTTPException(status_code=403, detail="需要团队 admin 权限")
    return team


# ---------------------------------------------------------------------------
# 响应模型
# ---------------------------------------------------------------------------


class MemberInfo(BaseModel):
    """成员信息。"""

    member_id: str
    display_name: str
    role: str
    status: str
    tags: list[str] = Field(default_factory=list)
    created_by: str
    created_at: str | None = None


class TeamInfo(BaseModel):
    """团队信息（扁平）。"""

    id: str
    name: str
    parent_team_id: str | None = None
    owner_id: str
    path: str
    description: str
    created_at: str | None = None


class TeamTreeNode(BaseModel):
    """团队树节点（嵌套）。"""

    id: str
    name: str
    parent_team_id: str | None = None
    owner_id: str
    path: str
    description: str
    member_count: int = 0
    children: list["TeamTreeNode"] = Field(default_factory=list)


class TeamMemberInfo(BaseModel):
    """团队成员关联信息。"""

    id: str
    team_id: str
    member_id: str
    role: str
    added_by: str
    added_at: str | None = None
    display_name: str = ""  # 关联 Member.display_name 便于展示


# ---------------------------------------------------------------------------
# 请求模型
# ---------------------------------------------------------------------------


class CreateMemberRequest(BaseModel):
    member_id: str = Field(..., min_length=1, max_length=64)
    display_name: str | None = None
    role: str | None = Field(None, description="admin/member，默认 member")
    tags: list[str] = Field(..., min_length=1, description="成员标签，至少填一个，如：前端/后端/全栈/测试/运维")


class UpdateMemberRequest(BaseModel):
    role: str | None = None
    status: str | None = None
    display_name: str | None = None
    tags: list[str] | None = None


class CreateTeamRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=128)
    description: str | None = None


class UpdateTeamRequest(BaseModel):
    name: str | None = None
    description: str | None = None


class AddTeamMemberRequest(BaseModel):
    member_id: str = Field(..., min_length=1, max_length=64)
    role: str | None = Field(None, description="admin/member，默认 member")


# ---------------------------------------------------------------------------
# 行 → 响应模型 转换
# ---------------------------------------------------------------------------


def _member_to_info(row: Member) -> MemberInfo:
    try:
        tags = json.loads(row.tags) if row.tags else []
    except (json.JSONDecodeError, TypeError):
        tags = []
    return MemberInfo(
        member_id=row.member_id,
        display_name=row.display_name,
        role=row.role,
        status=row.status,
        tags=tags,
        created_by=row.created_by,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _team_to_info(row: Team) -> TeamInfo:
    return TeamInfo(
        id=row.id,
        name=row.name,
        parent_team_id=row.parent_team_id,
        owner_id=row.owner_id,
        path=row.path,
        description=row.description,
        created_at=row.created_at.isoformat() if row.created_at else None,
    )


def _team_member_to_info(tm: TeamMember, member: Member | None = None) -> TeamMemberInfo:
    return TeamMemberInfo(
        id=tm.id,
        team_id=tm.team_id,
        member_id=tm.member_id,
        role=tm.role,
        added_by=tm.added_by,
        added_at=tm.added_at.isoformat() if tm.added_at else None,
        display_name=member.display_name if member else "",
    )


# ---------------------------------------------------------------------------
# 团队树构建
# ---------------------------------------------------------------------------


def _build_team_tree(sess, root_id: str | None) -> list[TeamTreeNode]:
    """构建团队树。

    - root_id=None：构建完整树（所有顶层团队为根）
    - root_id=xxx：构建以 xxx 为根的子树（含根节点本身）
    """
    # 查所有团队（按 path 排序保证稳定顺序）
    all_teams: list[Team] = list(sess.scalars(select(Team).order_by(Team.path)))
    team_map: dict[str, Team] = {t.id: t for t in all_teams}

    # 计算每个团队的成员数
    member_counts: dict[str, int] = {}
    stmt = select(TeamMember.team_id, func.count()).group_by(TeamMember.team_id)
    for tid, cnt in sess.execute(stmt):
        member_counts[tid] = int(cnt)

    # 构建子节点映射
    children_map: dict[str | None, list[Team]] = {}
    for t in all_teams:
        children_map.setdefault(t.parent_team_id, []).append(t)

    def build_node(team: Team) -> TeamTreeNode:
        children = [build_node(c) for c in children_map.get(team.id, [])]
        return TeamTreeNode(
            id=team.id,
            name=team.name,
            parent_team_id=team.parent_team_id,
            owner_id=team.owner_id,
            path=team.path,
            description=team.description,
            member_count=member_counts.get(team.id, 0),
            children=children,
        )

    if root_id is None:
        # 顶层团队（parent_team_id 为空）
        roots = children_map.get(None, [])
        return [build_node(t) for t in roots]

    root = team_map.get(root_id)
    if root is None:
        return []
    return [build_node(root)]


def _delete_team_recursive(sess, team_id: str) -> None:
    """递归删除团队及其子团队。

    ForeignKey ondelete=CASCADE 已配置，但显式递归删除更安全
    （避免 SQLite 未开启 PRAGMA foreign_keys=ON 时级联失效）。
    """
    # 先删子团队
    for child in sess.scalars(select(Team).where(Team.parent_team_id == team_id)):
        _delete_team_recursive(sess, child.id)
    # 再删自己（team_members 由 ondelete=CASCADE 自动清理，这里也显式删以防万一）
    for tm in sess.scalars(select(TeamMember).where(TeamMember.team_id == team_id)):
        sess.delete(tm)
    team = sess.get(Team, team_id)
    if team is not None:
        sess.delete(team)


# ===========================================================================
# 成员管理端点
# ===========================================================================


@team_router.get("/tags", response_model=list[str])
def list_all_tags(member_id: str = Depends(require_member)) -> list[str]:
    """返回系统中所有已使用的成员标签（去重），用于前端标签输入建议。"""
    db = _require_db()
    with db.session() as sess:
        rows = sess.scalars(select(Member.tags))
    tag_set: set[str] = set()
    for raw in rows:
        try:
            for t in json.loads(raw) if raw else []:
                t = t.strip()
                if t:
                    tag_set.add(t)
        except (json.JSONDecodeError, TypeError):
            pass
    return sorted(tag_set)


@team_router.get("/members", response_model=list[MemberInfo])
def list_members(member_id: str = Depends(require_member)) -> list[MemberInfo]:
    """列出所有成员（需系统 admin）。"""
    db = _require_db()
    with db.session() as sess:
        _require_system_admin(sess, member_id)
        stmt = select(Member).order_by(Member.created_at)
        return [_member_to_info(row) for row in sess.scalars(stmt)]


@team_router.post("/members", response_model=MemberInfo, status_code=201)
def create_member(
    req: CreateMemberRequest,
    member_id: str = Depends(require_member),
) -> MemberInfo:
    """添加成员（需系统 admin）。"""
    if req.role is not None and req.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role 只能是 admin/member")

    db = _require_db()
    with db.session() as sess:
        _require_system_admin(sess, member_id)
        if sess.get(Member, req.member_id) is not None:
            raise HTTPException(status_code=409, detail=f"成员已存在：{req.member_id}")
        row = Member(
            member_id=req.member_id,
            display_name=req.display_name or req.member_id,
            role=req.role or "member",
            status="active",
            tags=json.dumps(req.tags, ensure_ascii=False),
            created_by=member_id,
        )
        sess.add(row)
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(status_code=409, detail=f"成员已存在：{req.member_id}")
        sess.refresh(row)
    return _member_to_info(row)


@team_router.get("/members/{member_id}", response_model=MemberInfo)
def get_member(
    member_id: str,
    caller: str = Depends(require_member),
) -> MemberInfo:
    """查看成员详情。"""
    db = _require_db()
    with db.session() as sess:
        row = sess.get(Member, member_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"成员不存在：{member_id}")
    return _member_to_info(row)


@team_router.patch("/members/{member_id}", response_model=MemberInfo)
def update_member(
    member_id: str,
    req: UpdateMemberRequest,
    caller: str = Depends(require_member),
) -> MemberInfo:
    """修改成员（改 role/status/display_name，需系统 admin）。"""
    if req.role is not None and req.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role 只能是 admin/member")
    if req.status is not None and req.status not in ("active", "disabled"):
        raise HTTPException(status_code=400, detail="status 只能是 active/disabled")
    # 守卫：管理员不可以把自己降级为 member（避免自锁，与 delete 端点"不能删自己"对称）
    if req.role == "member" and member_id == caller:
        raise HTTPException(
            status_code=400,
            detail="管理员不可以把自己降级为 member（会导致失去管理权限）",
        )

    db = _require_db()
    with db.session() as sess:
        _require_system_admin(sess, caller)
        row = sess.get(Member, member_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"成员不存在：{member_id}")
        if req.role is not None:
            row.role = req.role
        if req.status is not None:
            row.status = req.status
        if req.display_name is not None:
            row.display_name = req.display_name
        if req.tags is not None:
            row.tags = json.dumps(req.tags, ensure_ascii=False)
        sess.commit()
        sess.refresh(row)
    return _member_to_info(row)


@team_router.delete("/members/{member_id}")
def delete_member(
    member_id: str,
    caller: str = Depends(require_member),
) -> dict[str, Any]:
    """删除成员（需系统 admin，不能删自己）。"""
    if member_id == caller:
        raise HTTPException(status_code=400, detail="不能删除自己")

    db = _require_db()
    with db.session() as sess:
        _require_system_admin(sess, caller)
        row = sess.get(Member, member_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"成员不存在：{member_id}")
        sess.delete(row)
        sess.commit()
    return {"member_id": member_id, "message": "成员已删除"}


# ===========================================================================
# 团队管理端点
# ===========================================================================
# 注意：/teams/tree 必须在 /teams/{team_id} 之前注册，
# 否则 "tree" 会被解释为 team_id 路径参数。


@team_router.get("/teams", response_model=list[TeamInfo])
def list_teams(member_id: str = Depends(require_member)) -> list[TeamInfo]:
    """列出所有团队（扁平列表，含 parent_team_id 便于前端重建树）。"""
    db = _require_db()
    with db.session() as sess:
        stmt = select(Team).order_by(Team.path)
        return [_team_to_info(row) for row in sess.scalars(stmt)]


@team_router.get("/teams/tree", response_model=list[TeamTreeNode])
def get_team_tree(member_id: str = Depends(require_member)) -> list[TeamTreeNode]:
    """获取完整团队树（嵌套 JSON，顶层团队为根）。"""
    db = _require_db()
    with db.session() as sess:
        return _build_team_tree(sess, root_id=None)


@team_router.post("/teams", response_model=TeamInfo, status_code=201)
def create_top_team(
    req: CreateTeamRequest,
    member_id: str = Depends(require_member),
) -> TeamInfo:
    """创建顶层团队（需系统 admin）。

    - 生成 id = f"team-{uuid.uuid4().hex[:12]}"
    - 顶层团队 path = /{team_id}
    - 创建者自动成为团队的 admin（写入 team_members）
    """
    db = _require_db()
    with db.session() as sess:
        _require_system_admin(sess, member_id)
        team_id = f"team-{uuid.uuid4().hex[:12]}"
        row = Team(
            id=team_id,
            name=req.name,
            parent_team_id=None,
            owner_id=member_id,
            path=f"/{team_id}",
            description=req.description or "",
        )
        sess.add(row)
        # 创建者自动成为团队的 admin
        sess.add(
            TeamMember(
                id=str(uuid.uuid4()),
                team_id=team_id,
                member_id=member_id,
                role="admin",
                added_by=member_id,
            )
        )
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(status_code=409, detail="团队创建失败（ID 冲突）")
        sess.refresh(row)
    return _team_to_info(row)


@team_router.get("/teams/{team_id}", response_model=TeamInfo)
def get_team(
    team_id: str,
    member_id: str = Depends(require_member),
) -> TeamInfo:
    """查看团队详情。"""
    db = _require_db()
    with db.session() as sess:
        row = sess.get(Team, team_id)
        if row is None:
            raise HTTPException(status_code=404, detail=f"团队不存在：{team_id}")
    return _team_to_info(row)


@team_router.patch("/teams/{team_id}", response_model=TeamInfo)
def update_team(
    team_id: str,
    req: UpdateTeamRequest,
    member_id: str = Depends(require_member),
) -> TeamInfo:
    """修改团队（需 team admin）。"""
    db = _require_db()
    with db.session() as sess:
        team = _require_team_admin(sess, team_id, member_id)
        if req.name is not None:
            team.name = req.name
        if req.description is not None:
            team.description = req.description
        sess.commit()
        sess.refresh(team)
    return _team_to_info(team)


@team_router.delete("/teams/{team_id}")
def delete_team(
    team_id: str,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """删除团队（需 team admin，级联删除子团队和成员关联）。"""
    db = _require_db()
    with db.session() as sess:
        _require_team_admin(sess, team_id, member_id)
        _delete_team_recursive(sess, team_id)
        sess.commit()
    return {"team_id": team_id, "message": "团队及其子团队已删除"}


@team_router.post("/teams/{team_id}/sub", response_model=TeamInfo, status_code=201)
def create_sub_team(
    team_id: str,
    req: CreateTeamRequest,
    member_id: str = Depends(require_member),
) -> TeamInfo:
    """创建子团队（需 team admin）。

    - 子团队 path = {parent.path}/{team_id}
    - 创建者自动成为子团队的 admin
    """
    db = _require_db()
    with db.session() as sess:
        parent = _require_team_admin(sess, team_id, member_id)
        new_id = f"team-{uuid.uuid4().hex[:12]}"
        row = Team(
            id=new_id,
            name=req.name,
            parent_team_id=team_id,
            owner_id=member_id,
            path=f"{parent.path}/{new_id}",
            description=req.description or "",
        )
        sess.add(row)
        # 创建者自动成为子团队的 admin
        sess.add(
            TeamMember(
                id=str(uuid.uuid4()),
                team_id=new_id,
                member_id=member_id,
                role="admin",
                added_by=member_id,
            )
        )
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(status_code=409, detail="子团队创建失败")
        sess.refresh(row)
    return _team_to_info(row)


@team_router.get("/teams/{team_id}/subtree", response_model=list[TeamTreeNode])
def get_team_subtree(
    team_id: str,
    member_id: str = Depends(require_member),
) -> list[TeamTreeNode]:
    """获取某团队的子树（含该团队本身作为根）。"""
    db = _require_db()
    with db.session() as sess:
        if sess.get(Team, team_id) is None:
            raise HTTPException(status_code=404, detail=f"团队不存在：{team_id}")
        return _build_team_tree(sess, root_id=team_id)


# ===========================================================================
# 团队成员管理端点
# ===========================================================================


@team_router.get("/teams/{team_id}/members", response_model=list[TeamMemberInfo])
def list_team_members(
    team_id: str,
    member_id: str = Depends(require_member),
) -> list[TeamMemberInfo]:
    """列出团队成员。"""
    db = _require_db()
    with db.session() as sess:
        if sess.get(Team, team_id) is None:
            raise HTTPException(status_code=404, detail=f"团队不存在：{team_id}")
        stmt = (
            select(TeamMember)
            .where(TeamMember.team_id == team_id)
            .order_by(TeamMember.added_at)
        )
        items: list[TeamMemberInfo] = []
        for tm in sess.scalars(stmt):
            member_row = sess.get(Member, tm.member_id)
            items.append(_team_member_to_info(tm, member_row))
    return items


@team_router.post("/teams/{team_id}/members", response_model=TeamMemberInfo, status_code=201)
def add_team_member(
    team_id: str,
    req: AddTeamMemberRequest,
    member_id: str = Depends(require_member),
) -> TeamMemberInfo:
    """添加成员到团队（需 team admin）。"""
    if req.role is not None and req.role not in ("admin", "member"):
        raise HTTPException(status_code=400, detail="role 只能是 admin/member")

    db = _require_db()
    with db.session() as sess:
        _require_team_admin(sess, team_id, member_id)
        # 校验成员存在
        member_row = sess.get(Member, req.member_id)
        if member_row is None:
            raise HTTPException(status_code=404, detail=f"成员不存在：{req.member_id}")
        # 校验未重复加入
        existing = sess.scalars(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.member_id == req.member_id,
            )
        ).first()
        if existing is not None:
            raise HTTPException(status_code=409, detail=f"成员已在团队中：{req.member_id}")
        tm = TeamMember(
            id=str(uuid.uuid4()),
            team_id=team_id,
            member_id=req.member_id,
            role=req.role or "member",
            added_by=member_id,
        )
        sess.add(tm)
        try:
            sess.commit()
        except IntegrityError:
            sess.rollback()
            raise HTTPException(status_code=409, detail=f"成员已在团队中：{req.member_id}")
        sess.refresh(tm)
    return _team_member_to_info(tm, member_row)


@team_router.delete("/teams/{team_id}/members/{member_id}")
def remove_team_member(
    team_id: str,
    member_id: str,
    caller: str = Depends(require_member),
) -> dict[str, Any]:
    """从团队移除成员（需 team admin）。"""
    db = _require_db()
    with db.session() as sess:
        _require_team_admin(sess, team_id, caller)
        tm = sess.scalars(
            select(TeamMember).where(
                TeamMember.team_id == team_id,
                TeamMember.member_id == member_id,
            )
        ).first()
        if tm is None:
            raise HTTPException(status_code=404, detail=f"成员不在团队中：{member_id}")
        sess.delete(tm)
        sess.commit()
    return {
        "team_id": team_id,
        "member_id": member_id,
        "message": "成员已从团队移除",
    }


__all__ = ["team_router", "configure_team_api"]
