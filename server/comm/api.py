"""comm 域 FastAPI 路由。

端点前缀：/v1/comm
鉴权：X-API-Key 头 → member_id（复用 team/api.py 的 require_member 模式）

端点列表：
- GET  /v1/comm/peers              列出 peer（含在线状态 + 标签）
- POST /v1/comm/heartbeat          前端定期心跳
- POST /v1/comm/ask                发起询问
- POST /v1/comm/answer             回复询问
- POST /v1/comm/reconcile          更新对账结果
- GET  /v1/comm/conversations      对话历史查询
- GET  /v1/comm/conversations/{event_id}/thread  对话线程
- GET  /v1/comm/shadow-log         影子对账状态
- GET  /v1/comm/outbox             outbox 待投递消息
"""

from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query
from pydantic import BaseModel, Field

from server.binding.auth_service import AgentApiKeyService
from server.comm.service import CommService
from server.infra_db.db import Database

comm_router = APIRouter(prefix="/v1/comm", tags=["comm"])

# 模块级服务（启动时由 configure_comm_api 注入，唯一注入路径）
_DB: Database | None = None
_AUTH: AgentApiKeyService | None = None
_SVC: CommService | None = None


def configure_comm_api(database: Database) -> None:
    """注入 Database 实例（唯一注入路径，生产与测试均通过此函数）。

    参见 gotchas.md R036：不存在 build_router(svc) 旁路，避免双轨制。
    """
    global _DB, _AUTH, _SVC
    _DB = database
    _AUTH = AgentApiKeyService(database)
    _SVC = CommService(database)


def _require_db() -> Database:
    if _DB is None:
        raise HTTPException(status_code=503, detail="Database 未配置")
    return _DB


def _require_auth() -> AgentApiKeyService:
    if _AUTH is None:
        raise HTTPException(status_code=503, detail="AgentApiKeyService 未配置")
    return _AUTH


def _require_svc() -> CommService:
    if _SVC is None:
        raise HTTPException(status_code=503, detail="CommService 未配置")
    return _SVC


# ---------------------------------------------------------------------------
# 鉴权依赖
# ---------------------------------------------------------------------------


def require_member(x_api_key: str = Header(default="", alias="X-API-Key")) -> str:
    """校验 X-API-Key 头，返回 member_id。

    - 缺失 / 空 key → 401
    - 无效 / 已失效 key → 401
    - 有效 key → 返回 member_id
    """
    if not x_api_key:
        raise HTTPException(status_code=401, detail="缺少 X-API-Key 头")
    svc = _require_auth()
    member_id = svc.lookup_member_id(x_api_key)
    if member_id is None:
        raise HTTPException(status_code=401, detail="无效或已失效的 API Key")
    return member_id


# ---------------------------------------------------------------------------
# 请求/响应模型
# ---------------------------------------------------------------------------


class HeartbeatRequest(BaseModel):
    endpoint: str = Field(default="", description="P2P 模式下自报的 endpoint")


class AskRequest(BaseModel):
    to_peer: str = Field(..., min_length=1, max_length=64, description="目标 peer_id")
    question: str = Field(..., min_length=1, description="提问内容")
    in_reply_to: str = Field(default="", description="回复链：关联的 ask 事件 ID")


class AnswerRequest(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="原 ask 事件 ID")
    answer: str = Field(..., min_length=1, description="回答内容")
    realtime: bool = Field(..., description="True=实时回答, False=影子模拟回答")
    based_on: str = Field(default="", description="影子快照版本（如 bob_v38）")
    snapshot_stale: bool = Field(default=False, description="快照是否过期")


class ReconcileRequest(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=64, description="simulated_answer 事件 ID")
    verdict: str = Field(..., description="confirmed / revised / needs_human_review")
    revised_answer: str = Field(default="", description="修订后的回答（verdict=revised 时填写）")


class PeerInfo(BaseModel):
    member_id: str
    display_name: str
    tags: list[str] = Field(default_factory=list)
    online: bool
    last_heartbeat: str | None = None


# ---------------------------------------------------------------------------
# 入参校验（R042：grantee_id / member_id 格式验证）
# ---------------------------------------------------------------------------


def _validate_member_id(value: str, field_name: str = "member_id") -> None:
    """校验 member_id / peer_id 格式：非空 string，长度 ≤ 64。"""
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"{field_name} 不能为空")
    if len(value) > 64:
        raise HTTPException(status_code=400, detail=f"{field_name} 长度不能超过 64 字符")


# ===========================================================================
# 端点
# ===========================================================================


@comm_router.get("/peers", response_model=list[PeerInfo])
def list_peers(member_id: str = Depends(require_member)) -> list[PeerInfo]:
    """列出所有 peer（含在线状态 + 标签）。

    capabilities（标签）从 Member.tags 实时读取（spec 设计决策，不缓存）。
    在线状态从 CommPeerStatus.last_heartbeat 判断（≤ 120s 视为在线）。
    """
    svc = _require_svc()
    peers = svc.list_peers(current_member=member_id)
    return [PeerInfo(**p) for p in peers]


@comm_router.post("/heartbeat")
def heartbeat(
    req: HeartbeatRequest,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """更新自己的心跳（前端定期调用，维护在线状态）。"""
    svc = _require_svc()
    return svc.heartbeat(member_id=member_id, endpoint=req.endpoint)


@comm_router.post("/ask")
def ask(
    req: AskRequest,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """发起询问（记录 ask 事件）。

    - 在线 peer → realtime=true, status=delivered
    - 离线 peer → degraded=true, status=pending_delivery（影子联络）
    """
    _validate_member_id(req.to_peer, "to_peer")
    svc = _require_svc()
    try:
        return svc.record_ask(
            from_member=member_id,
            to_peer=req.to_peer,
            question=req.question,
            in_reply_to=req.in_reply_to,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


@comm_router.post("/answer")
def answer(
    req: AnswerRequest,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """回复询问（记录 answer 事件，更新原 ask 状态）。

    realtime=True → realtime_answer（在线实时回答）
    realtime=False → simulated_answer（影子联络模拟回答）
    """
    _validate_member_id(req.event_id, "event_id")
    svc = _require_svc()
    try:
        return svc.record_answer(
            event_id=req.event_id,
            answer=req.answer,
            realtime=req.realtime,
            based_on=req.based_on,
            snapshot_stale=req.snapshot_stale,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@comm_router.post("/reconcile")
def reconcile(
    req: ReconcileRequest,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """更新对账结果（peer 上线后对比模拟回答与真实回答）。

    verdict: confirmed / revised / needs_human_review
    """
    if req.verdict not in ("confirmed", "revised", "needs_human_review"):
        raise HTTPException(status_code=400, detail="verdict 只能是 confirmed/revised/needs_human_review")
    svc = _require_svc()
    try:
        return svc.update_reconciliation(
            event_id=req.event_id,
            verdict=req.verdict,
            revised_answer=req.revised_answer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@comm_router.get("/conversations")
def list_conversations(
    peer: str | None = Query(None, description="对方 peer_id 过滤"),
    type: str | None = Query(None, description="事件类型过滤"),
    direction: str | None = Query(None, description="outgoing/incoming"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """查询对话历史（R041：current_member 从认证上下文推导，不信任客户端传入）。"""
    if direction is not None and direction not in ("outgoing", "incoming"):
        raise HTTPException(status_code=400, detail="direction 只能是 outgoing/incoming")
    if peer:
        _validate_member_id(peer, "peer")
    svc = _require_svc()
    return svc.list_conversations(
        current_member=member_id,
        peer=peer,
        event_type=type,
        direction=direction,
        limit=limit,
        offset=offset,
    )


@comm_router.get("/conversations/{event_id}/thread")
def get_thread(
    event_id: str,
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """获取对话线程（基于 in_reply_to 回复链展开）。"""
    _validate_member_id(event_id, "event_id")
    svc = _require_svc()
    try:
        return svc.get_thread(event_id=event_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@comm_router.get("/shadow-log")
def list_shadow_log(
    status: str | None = Query(None, description="状态过滤"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """查询影子对账状态（degraded=true 的事件）。"""
    svc = _require_svc()
    return svc.list_shadow_log(
        current_member=member_id,
        status=status,
        limit=limit,
        offset=offset,
    )


@comm_router.get("/outbox")
def list_outbox(
    status: str | None = Query("pending_delivery", description="状态过滤，默认 pending_delivery"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    member_id: str = Depends(require_member),
) -> dict[str, Any]:
    """查询 outbox（待投递消息）。"""
    svc = _require_svc()
    return svc.list_outbox(
        current_member=member_id,
        status=status,
        limit=limit,
        offset=offset,
    )


# ===========================================================================
# A2A AgentCard 端点（公开，无需鉴权）
# 让外部 A2A 标准客户端能通过 /.well-known/agent-card.json 发现 TeamHarness。
# 参考：https://a2a-protocol.org/latest/specification/
# ===========================================================================

well_known_router = APIRouter(prefix="/.well-known", tags=["well-known"])


@well_known_router.get("/agent-card.json")
def get_agent_card(member_id: str | None = Query(None, description="可选：查询单个 member 的 AgentCard")) -> dict[str, Any]:
    """A2A AgentCard 端点（公开发现，无鉴权）。

    不传 member_id → 返回服务端目录（所有 member 的 AgentCard 摘要）。
    传 member_id → 返回该 member 的 AgentCard（含 capabilities / skills）。
    """
    db = _require_db()
    from server.infra_db.models import Member

    def _build_card(m: Member) -> dict[str, Any]:
        try:
            tags: list[str] = json.loads(m.tags) if m.tags else []
        except (json.JSONDecodeError, TypeError):
            tags = []
        return {
            "name": m.display_name or m.member_id,
            "member_id": m.member_id,
            "role": m.role,
            "tags": tags,
            "capabilities": {
                "extensions": [
                    "https://teamharness.org/a2a-ext/shadow-comm"
                ],
                "supported_tools": [
                    "ask_peer",
                    "list_peers",
                    "share_asset",
                    "search_team_assets",
                    "resume_conversation",
                ],
            },
            "skills": [
                {"name": t, "description": f"成员擅长 {t} 方向"}
                for t in tags
            ],
        }

    with db.session() as sess:
        if member_id:
            # R042：member_id 格式校验
            _validate_member_id(member_id, "member_id")
            m = sess.get(Member, member_id)
            if m is None or m.status != "active":
                raise HTTPException(status_code=404, detail=f"member {member_id} 不存在或未激活")
            return _build_card(m)
        else:
            members = (
                sess.query(Member)
                .filter(Member.status == "active")
                .order_by(Member.member_id)
                .all()
            )
            return {
                "name": "TeamHarness",
                "description": "Shadow Communication Hub for AI-to-AI Collaboration",
                "version": "1.0.0",
                "protocol": "a2a+teamharness-shadow-comm-ext",
                "extensions": [
                    {
                        "uri": "https://teamharness.org/a2a-ext/shadow-comm",
                        "description": "Offline simulation + reconcile protocol",
                        "required": False,
                    },
                ],
                "members": [_build_card(m) for m in members],
            }


__all__ = ["comm_router", "well_known_router", "configure_comm_api"]
