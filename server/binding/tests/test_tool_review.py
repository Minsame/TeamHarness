"""SubTask 5.9 测试：tool PR Review 强制 CODEOWNERS + 签名验证。"""

from __future__ import annotations

import base64
from datetime import datetime

import pytest

from server.binding.tool_review import (
    PRFileInfo,
    ToolReviewResult,
    ToolReviewService,
    generate_ed25519_keypair,
    sign_tool_content,
)


def _signed_tool_content(body: str, private_pem: bytes) -> str:
    """构造含签名的 tool 文件内容（frontmatter + body）。"""
    sig = sign_tool_content(body, private_pem)
    return (
        "---\n"
        "id: tool-test\n"
        "type: tool\n"
        f"signature: {sig}\n"
        "signature_algorithm: ed25519\n"
        f"---\n{body}"
    )


def _unsigned_tool_content(body: str) -> str:
    """构造无签名的 tool 文件内容。"""
    return (
        "---\n"
        "id: tool-test\n"
        "type: tool\n"
        "---\n"
        f"{body}"
    )


class TestToolReviewSignature:
    """签名验证。"""

    def test_valid_signature_approved(self, tool_review_service, ed25519_keypair):
        """合法签名 + 至少 1 名 trusted reviewer → approved。"""
        priv_pem, _ = ed25519_keypair
        body = "print('hello')"
        content = _signed_tool_content(body, priv_pem)
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = tool_review_service.review_file(info, pr_id="pr-1")
        assert result.signature_present is True
        assert result.signature_valid is True
        assert result.codeowners_approved is True
        assert result.trusted_reviewers_count == 1
        assert result.decision == "approved"

    def test_missing_signature_rejected(self, tool_review_service):
        """缺 signature 字段 → rejected。"""
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=_unsigned_tool_content("print('hi')"),
            approvers=["alice"],
        )
        result = tool_review_service.review_file(info, pr_id="pr-2")
        assert result.signature_present is False
        assert result.signature_valid is False
        assert result.decision == "rejected"
        assert "缺少 signature" in result.reason

    def test_invalid_signature_rejected(self, tool_review_service, ed25519_keypair):
        """签名不匹配 body → rejected。"""
        priv_pem, _ = ed25519_keypair
        # 对 body A 签名，但提交 body B
        sig = sign_tool_content("print('A')", priv_pem)
        content = (
            "---\n"
            f"signature: {sig}\n"
            "signature_algorithm: ed25519\n"
            "---\nprint('B')"
        )
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = tool_review_service.review_file(info, pr_id="pr-3")
        assert result.signature_present is True
        assert result.signature_valid is False
        assert result.decision == "rejected"
        assert "签名验证失败" in result.reason

    def test_unsupported_algorithm_rejected(
        self, tool_review_service, ed25519_keypair
    ):
        """非 ed25519 算法 → rejected。"""
        priv_pem, _ = ed25519_keypair
        sig = sign_tool_content("body", priv_pem)
        content = (
            "---\n"
            f"signature: {sig}\n"
            "signature_algorithm: rsa\n"
            "---\nbody"
        )
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = tool_review_service.review_file(info, pr_id="pr-4")
        assert result.signature_present is True
        assert result.signature_valid is False
        assert "不支持" in result.reason

    def test_no_public_key_rejected(self, database, ed25519_keypair):
        """未配置公钥 → signature_valid=False。"""
        priv_pem, _ = ed25519_keypair
        body = "x"
        content = _signed_tool_content(body, priv_pem)
        svc = ToolReviewService(
            database,
            trusted_reviewers={"alice"},
            public_key=None,  # 未配置
        )
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = svc.review_file(info, pr_id="pr-5")
        assert result.signature_valid is False
        assert result.decision == "rejected"


class TestToolReviewCodeowners:
    """CODEOWNERS 验证。"""

    def test_no_trusted_approver_rejected(self, tool_review_service, ed25519_keypair):
        """无 trusted reviewer approve → rejected。"""
        priv_pem, _ = ed25519_keypair
        content = _signed_tool_content("body", priv_pem)
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["carol"],  # carol 不在 trusted_reviewers
        )
        result = tool_review_service.review_file(info, pr_id="pr-6")
        assert result.codeowners_approved is False
        assert result.trusted_reviewers_count == 0
        assert result.decision == "rejected"
        assert "trusted reviewer" in result.reason

    def test_min_trusted_reviewers_2(self, database, ed25519_keypair):
        """min_trusted_reviewers=2 → 至少 2 名 trusted reviewer。"""
        priv_pem, pub_raw = ed25519_keypair
        content = _signed_tool_content("body", priv_pem)
        svc = ToolReviewService(
            database,
            trusted_reviewers={"alice", "bob", "carol"},
            public_key=pub_raw,
            min_trusted_reviewers=2,
        )
        # 只有 alice approve → 不够
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = svc.review_file(info, pr_id="pr-7")
        assert result.decision == "rejected"
        # alice + bob → 够
        info2 = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice", "bob"],
        )
        result2 = svc.review_file(info2, pr_id="pr-8")
        assert result2.decision == "approved"

    def test_trusted_and_untrusted_mix(self, tool_review_service, ed25519_keypair):
        """trusted + untrusted approver 混合 → 只数 trusted。"""
        priv_pem, _ = ed25519_keypair
        content = _signed_tool_content("body", priv_pem)
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice", "carol", "dave"],  # alice trusted，carol/dave 不
        )
        result = tool_review_service.review_file(info, pr_id="pr-9")
        assert result.trusted_reviewers_count == 1
        assert result.codeowners_approved is True


class TestToolReviewPersist:
    """审查留痕。"""

    def test_record_persisted(self, tool_review_service, ed25519_keypair):
        """审查结果写 tool_review_record 表。"""
        priv_pem, _ = ed25519_keypair
        content = _signed_tool_content("body", priv_pem)
        info = PRFileInfo(
            asset_path="tools/x.py",
            commit_sha="abc",
            content=content,
            approvers=["alice"],
        )
        result = tool_review_service.review_file(info, pr_id="pr-10")
        assert result.record_id.startswith("review-")
        records = tool_review_service.list_reviews(pr_id="pr-10")
        assert len(records) == 1
        r = records[0]
        assert r.signature_present is True
        assert r.signature_valid is True
        assert r.codeowners_approved is True
        assert r.decision == "approved"

    def test_review_pr_multiple_files(self, tool_review_service, ed25519_keypair):
        """review_pr 处理多文件。"""
        priv_pem, _ = ed25519_keypair
        good = PRFileInfo(
            asset_path="tools/good.py",
            commit_sha="abc",
            content=_signed_tool_content("good body", priv_pem),
            approvers=["alice"],
        )
        bad = PRFileInfo(
            asset_path="tools/bad.py",
            commit_sha="abc",
            content=_unsigned_tool_content("bad body"),
            approvers=["alice"],
        )
        results = tool_review_service.review_pr(pr_id="pr-11", files=[good, bad])
        assert len(results) == 2
        decisions = {r.asset_path: r.decision for r in results}
        assert decisions["tools/good.py"] == "approved"
        assert decisions["tools/bad.py"] == "rejected"
