"""webhook 接收端点测试。

对应 SubTask 1.4：
- POST /v1/webhook/git
- HMAC-SHA256 签名校验（Gitea/通用），GitLab plain token 模式
- 未签名 / 签名不符 → 401
- 未配置 secret → 503
- 幂等：同 commit 多次只处理一次
- detect_provider / normalize_event
"""

from __future__ import annotations

import hashlib
import hmac
import json
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from server.common.models import WebhookEvent
from server.infra_git.webhook import (
    ProcessedTracker,
    compute_hmac_sha256,
    configure_webhook,
    detect_provider,
    normalize_event,
    router,
    verify_signature,
)


@pytest.fixture
def app():
    a = FastAPI()
    a.include_router(router)
    return a


@pytest.fixture
def client(app):
    return TestClient(app)


# ---------------------------------------------------------------------------
# 签名校验
# ---------------------------------------------------------------------------


def test_compute_hmac_matches_known_vector():
    """HMAC-SHA256 与标准向量对齐。"""
    sig = compute_hmac_sha256("secret", b'{"hello":"world"}')
    # 用 hmac 模块独立计算一次校验
    expected = hmac.new(b"secret", b'{"hello":"world"}', hashlib.sha256).hexdigest()
    assert sig == expected


def test_verify_signature_gitea_hmac():
    body = b'{"after":"abc"}'
    sig = compute_hmac_sha256("s3cret", body)
    assert verify_signature(body, {"x-gitea-signature": sig}, "s3cret") is True


def test_verify_signature_hub_with_sha256_prefix():
    body = b'{"x":1}'
    sig = compute_hmac_sha256("k", body)
    assert verify_signature(body, {"x-hub-signature-256": f"sha256={sig}"}, "k") is True


def test_verify_signature_gitlab_plain_token():
    body = b'{"x":1}'
    assert verify_signature(body, {"x-gitlab-token": "plain-tok"}, "plain-tok") is True


def test_verify_signature_wrong_secret():
    body = b'{"x":1}'
    sig = compute_hmac_sha256("real-secret", body)
    assert verify_signature(body, {"x-gitea-signature": sig}, "wrong-secret") is False


def test_verify_signature_missing_header():
    assert verify_signature(b'{"x":1}', {}, "secret") is False


def test_verify_signature_gitlab_signature_hmac():
    body = b'{"x":1}'
    sig = compute_hmac_sha256("k", body)
    assert verify_signature(body, {"x-gitlab-signature": sig}, "k") is True


# ---------------------------------------------------------------------------
# detect_provider / normalize_event
# ---------------------------------------------------------------------------


def test_detect_provider_gitlab():
    assert detect_provider({"x-gitlab-event": "Push Hook"}) == "gitlab"
    assert detect_provider({"x-gitlab-token": "t"}) == "gitlab"


def test_detect_provider_gitea():
    assert detect_provider({"x-gitea-event": "push"}) == "gitea"
    assert detect_provider({"x-gitea-signature": "s"}) == "gitea"
    assert detect_provider({"x-gogs-event": "push"}) == "gitea"


def test_detect_provider_unknown():
    assert detect_provider({}) == "unknown"


def test_normalize_event_gitlab_push():
    payload = {
        "project": {"path_with_namespace": "team/repo"},
        "before": "shaa",
        "after": "shab",
        "ref": "refs/heads/main",
    }
    ev = normalize_event(payload, {"x-gitlab-event": "Push Hook"})
    assert ev.provider == "gitlab"
    assert ev.event_type == "Push Hook"
    assert ev.repo == "team/repo"
    assert ev.before == "shaa"
    assert ev.after == "shab"
    assert ev.ref == "refs/heads/main"


def test_normalize_event_gitea_push():
    payload = {
        "repository": {"full_name": "team/repo"},
        "before": "shaa",
        "after": "shab",
        "ref": "refs/heads/main",
    }
    ev = normalize_event(payload, {"x-gitea-event": "push"})
    assert ev.provider == "gitea"
    assert ev.repo == "team/repo"
    assert ev.after == "shab"


def test_normalize_event_unknown_fallback():
    payload = {
        "repository": {"full_name": "team/repo"},
        "after": "shab",
    }
    ev = normalize_event(payload, {})
    assert ev.provider == "unknown"
    assert ev.after == "shab"


# ---------------------------------------------------------------------------
# 幂等
# ---------------------------------------------------------------------------


def test_processed_tracker_zero_sha_skipped():
    """0000... 表示删除分支，不参与幂等。"""
    t = ProcessedTracker()
    assert t.is_processed("0" * 40) is False
    t.mark_processed("0" * 40)
    # 仍可重复处理
    assert t.is_processed("0" * 40) is False


def test_processed_tracker_dedup():
    t = ProcessedTracker()
    assert not t.is_processed("abc")
    t.mark_processed("abc")
    assert t.is_processed("abc")


# ---------------------------------------------------------------------------
# 端点
# ---------------------------------------------------------------------------


def test_endpoint_rejects_when_secret_unconfigured(client: TestClient):
    """未配置 secret → 503。"""
    resp = client.post("/v1/webhook/git", content=b'{"x":1}')
    assert resp.status_code == 503


def test_endpoint_rejects_invalid_signature(client: TestClient):
    configure_webhook(secret="real-secret")
    resp = client.post(
        "/v1/webhook/git",
        content=b'{"after":"sha"}',
        headers={"x-gitea-signature": "deadbeef"},
    )
    assert resp.status_code == 401


def test_endpoint_rejects_missing_signature(client: TestClient):
    configure_webhook(secret="real-secret")
    resp = client.post("/v1/webhook/git", content=b'{"after":"sha"}')
    assert resp.status_code == 401


def test_endpoint_accepts_valid_gitea_signature(client: TestClient):
    body = json.dumps({"after": "sha-1", "repository": {"full_name": "t/r"}, "ref": "refs/heads/main"})
    sig = compute_hmac_sha256("s3cret", body.encode())
    configure_webhook(secret="s3cret")

    received: list[WebhookEvent] = []

    def handler(ev: WebhookEvent):
        received.append(ev)

    configure_webhook(secret="s3cret", sync_handler=handler)

    resp = client.post(
        "/v1/webhook/git",
        content=body,
        headers={"x-gitea-signature": sig, "x-gitea-event": "push"},
    )
    assert resp.status_code == 200
    body_json = resp.json()
    assert body_json["status"] == "accepted"
    assert body_json["commit"] == "sha-1"
    assert body_json["provider"] == "gitea"
    assert received and received[0].after == "sha-1"


def test_endpoint_accepts_gitlab_plain_token(client: TestClient):
    body = json.dumps({"after": "sha-2", "project": {"path_with_namespace": "t/r"}})
    configure_webhook(secret="plain-tok")
    resp = client.post(
        "/v1/webhook/git",
        content=body,
        headers={"x-gitlab-token": "plain-tok", "x-gitlab-event": "Push Hook"},
    )
    assert resp.status_code == 200
    assert resp.json()["provider"] == "gitlab"


def test_endpoint_idempotent(client: TestClient):
    """同 commit 多次触发只处理一次。"""
    body = json.dumps({"after": "sha-3", "repository": {"full_name": "t/r"}})
    sig = compute_hmac_sha256("s", body.encode())
    calls: list[str] = []

    def handler(ev: WebhookEvent):
        calls.append(ev.after)

    configure_webhook(secret="s", sync_handler=handler)

    r1 = client.post(
        "/v1/webhook/git",
        content=body,
        headers={"x-gitea-signature": sig, "x-gitea-event": "push"},
    )
    r2 = client.post(
        "/v1/webhook/git",
        content=body,
        headers={"x-gitea-signature": sig, "x-gitea-event": "push"},
    )
    assert r1.json()["status"] == "accepted"
    assert r2.json()["status"] == "duplicated"
    assert calls == ["sha-3"]  # 只调用一次


def test_endpoint_rejects_invalid_json(client: TestClient):
    body = b"not-json"
    sig = compute_hmac_sha256("s", body)
    configure_webhook(secret="s")
    resp = client.post(
        "/v1/webhook/git",
        content=body,
        headers={"x-gitea-signature": sig, "x-gitea-event": "push"},
    )
    assert resp.status_code == 400
