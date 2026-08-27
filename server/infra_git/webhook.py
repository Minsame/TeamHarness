"""webhook 接收端点。

对应技术方案 SubTask 1.4：POST /v1/webhook/git，接收 GitLab/Gitea webhook，
校验 secret 签名（HMAC-SHA256），以 commit SHA 为幂等键。

签名校验策略：
- GitLab：X-Gitlab-Token 头携带配置的 secret（明文比对），
  或 X-Gitlab-Token 用于 HMAC 共享密钥时的明文比对（GitLab 原生不支持 HMAC，
  此处兼容 GitLab 的 plain token 模式）。
- Gitea / 通用：X-Gitea-Signature / X-Hub-Signature-256 携带 HMAC-SHA256 摘要。
未签名或签名不符 → 401 拒绝。
"""

from __future__ import annotations

import hashlib
import hmac
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from fastapi import APIRouter, Header, HTTPException, Request

from server.common.models import WebhookEvent

# webhook 子路由，由主应用 include
router = APIRouter(prefix="/v1/webhook", tags=["webhook"])


# ---------------------------------------------------------------------------
# 签名校验
# ---------------------------------------------------------------------------


def compute_hmac_sha256(secret: str, body: bytes) -> str:
    """计算 body 的 HMAC-SHA256 十六进制摘要。"""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def verify_signature(
    body: bytes,
    headers: dict[str, str],
    secret: str,
) -> bool:
    """校验 webhook 签名。

    优先 HMAC-SHA256（Gitea/通用），兼容 GitLab plain token。
    headers 键大小写不敏感由调用方保证（FastAPI Header 已处理）。
    """
    # 1. HMAC-SHA256（Gitea X-Gitea-Signature / GitHub 风格 X-Hub-Signature-256）
    for h in ("x-gitea-signature", "x-hub-signature-256", "x-gitlab-signature"):
        sig = headers.get(h)
        if sig:
            expected = compute_hmac_sha256(secret, body)
            # Gitea 头不带 "sha256=" 前缀；GitHub 风格带前缀，统一处理
            sig_clean = sig.removeprefix("sha256=")
            return hmac.compare_digest(sig_clean, expected)

    # 2. GitLab plain token 模式（X-Gitlab-Token 与 secret 明文比对）
    gitlab_token = headers.get("x-gitlab-token")
    if gitlab_token is not None:
        return hmac.compare_digest(gitlab_token, secret)

    return False


# ---------------------------------------------------------------------------
# 事件归一化
# ---------------------------------------------------------------------------


def detect_provider(headers: dict[str, str]) -> str:
    """根据请求头识别 webhook 来源。"""
    if "x-gitlab-token" in headers or "x-gitlab-event" in headers:
        return "gitlab"
    if "x-gitea-event" in headers or "x-gitea-signature" in headers:
        return "gitea"
    if "x-gogs-event" in headers:  # Gitea 早期兼容 Gogs
        return "gitea"
    return "unknown"


def normalize_event(payload: dict[str, Any], headers: dict[str, str]) -> WebhookEvent:
    """将 GitLab/Gitea 原生 payload 归一化为 WebhookEvent。

    支持 push 和 pull_request 事件（PR merged 时触发同步）。
    """
    provider = detect_provider(headers)
    event_type = headers.get("x-gitlab-event") or headers.get("x-gitea-event") or "push"

    # PR 事件处理（Gitea pull_request / GitLab merge_request）
    is_pr_merged = False
    if event_type in ("pull_request", "merge_request"):
        action = payload.get("action", "")
        # Gitea: action=closed + pull_request.merged=true
        # GitLab: object_attributes.action=merge
        pr = payload.get("pull_request") or payload.get("object_attributes", {})
        if action == "closed" and pr.get("merged") is True:
            is_pr_merged = True
        elif action == "merge":
            is_pr_merged = True

    if is_pr_merged:
        # PR merged：用 merge_commit 作为 after
        pr = payload.get("pull_request") or payload.get("object_attributes", {})
        repo = (
            payload.get("repository", {}).get("full_name")
            or payload.get("project", {}).get("path_with_namespace", "")
        )
        after = pr.get("merge_commit_sha", "") or pr.get("merge_commit_sha", "")
        ref = pr.get("base", {}).get("ref", "main") if "base" in pr else pr.get("target_branch", "main")
        return WebhookEvent(
            provider=provider,
            event_type="pr_merged",
            repo=repo,
            before="",
            after=after,
            ref=ref,
            raw=payload,
        )

    if provider == "gitlab":
        # GitLab push event
        repo = (
            payload.get("project", {}).get("path_with_namespace")
            or payload.get("repository", {}).get("homepage", "")
        )
        before = payload.get("before", "")
        after = payload.get("after", "")
        ref = payload.get("ref", "")
    elif provider == "gitea":
        repo = (
            payload.get("repository", {}).get("full_name")
            or payload.get("repository", {}).get("html_url", "")
        )
        before = payload.get("before", payload.get("commits", [{}])[0].get("id", "") if payload.get("commits") else "")
        after = payload.get("after", "")
        ref = payload.get("ref", "")
    else:
        repo = payload.get("repository", {}).get("full_name", "")
        before = payload.get("before", "")
        after = payload.get("after", "")
        ref = payload.get("ref", "")

    return WebhookEvent(
        provider=provider,
        event_type=event_type,
        repo=repo,
        before=before,
        after=after,
        ref=ref,
        raw=payload,
    )


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


@dataclass
class ProcessedTracker:
    """已处理 commit SHA 内存幂等集合（生产环境由 infra_db 持久化）。

    对应技术方案 8.12 webhook 幂等：以 commit SHA 为幂等键，
    同一 commit 多次触发只处理一次。
    """

    processed: set[str] = field(default_factory=set)

    def is_processed(self, sha: str) -> bool:
        # 0000000... 表示删除分支等无 after commit 的情况，直接放过
        if set(sha) == {"0"}:
            return False
        return sha in self.processed

    def mark_processed(self, sha: str) -> None:
        if set(sha) == {"0"}:
            return
        self.processed.add(sha)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


# 全局默认配置（生产由应用启动时注入）。secret 为空表示未配置 → 拒绝所有请求。
_WEBHOOK_SECRET: str = ""
_SYNC_HANDLER: Callable[[WebhookEvent], None] | None = None
_TRACKER: ProcessedTracker = ProcessedTracker()


def configure_webhook(
    *,
    secret: str,
    sync_handler: Callable[[WebhookEvent], None] | None = None,
    tracker: ProcessedTracker | None = None,
) -> None:
    """注入 webhook 配置（由 FastAPI 启动事件调用）。"""
    global _WEBHOOK_SECRET, _SYNC_HANDLER, _TRACKER
    _WEBHOOK_SECRET = secret
    _SYNC_HANDLER = sync_handler
    if tracker is not None:
        _TRACKER = tracker


@router.post("/git")
async def receive_git_webhook(
    request: Request,
    x_gitlab_token: str | None = Header(default=None),
    x_gitlab_event: str | None = Header(default=None),
    x_gitea_event: str | None = Header(default=None),
    x_gitea_signature: str | None = Header(default=None),
    x_hub_signature_256: str | None = Header(default=None),
    x_gitlab_signature: str | None = Header(default=None),
) -> dict[str, Any]:
    """接收 GitLab/Gitea webhook，校验签名后触发同步。"""
    if not _WEBHOOK_SECRET:
        # 未配置 secret，拒绝所有请求（防未授权触发）
        raise HTTPException(status_code=503, detail="webhook secret 未配置")

    body = await request.body()
    headers = {
        k: v
        for k, v in {
            "x-gitlab-token": x_gitlab_token,
            "x-gitlab-event": x_gitlab_event,
            "x-gitea-event": x_gitea_event,
            "x-gitea-signature": x_gitea_signature,
            "x-hub-signature-256": x_hub_signature_256,
            "x-gitlab-signature": x_gitlab_signature,
        }.items()
        if v is not None
    }

    if not verify_signature(body, headers, _WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="签名校验失败")

    import json

    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="非法 JSON payload")

    event = normalize_event(payload, headers)

    # 幂等：同一 after commit 只处理一次
    if _TRACKER.is_processed(event.after):
        return {"status": "duplicated", "commit": event.after}

    # 触发同步（同步逻辑由 infra_db / recall 注入；本域只负责接收与校验）
    if _SYNC_HANDLER is not None:
        _SYNC_HANDLER(event)

    _TRACKER.mark_processed(event.after)
    return {"status": "accepted", "provider": event.provider, "commit": event.after}
