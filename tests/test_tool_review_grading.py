"""ToolReviewService 分级审查测试。

固化自主对话内联脚本，确保可重复执行。

覆盖：
- memory/skill/prompt 路径 → 放行（非审查类型）
- rule 路径 + CODEOWNERS 审批 → approved
- rule 路径 无 CODEOWNERS → rejected
- tool 路径 无签名 → rejected
- tool 路径 有签名 无 CODEOWNERS → rejected
- tool 路径 有签名 + CODEOWNERS → approved（占位验签）
- 签名算法非 ed25519 → rejected
- 多 trusted reviewer 计数
"""

from __future__ import annotations

from server.binding.tool_review import ToolReviewService, PRFileInfo


def _make_svc(trusted=None, public_key=b"\x00" * 32, min_reviewers=1):
    """构造 service（database=None，pr_id='' 时不持久化）。"""
    return ToolReviewService(
        database=None,
        trusted_reviewers=trusted or {"alice", "bob"},
        public_key=public_key,
        min_trusted_reviewers=min_reviewers,
    )


# ---------------------------------------------------------------------------
# 非审查类型：放行
# ---------------------------------------------------------------------------


def test_memory_path_pass_through():
    """memory 路径 → 非审查类型 → approved。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="memory/note.md",
        commit_sha="x",
        content="# note",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved"
    assert "非审查类型" in r.reason


def test_skill_path_pass_through():
    """skill 路径 → 非审查类型 → approved。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="skills/summarize.md",
        commit_sha="x",
        content="# skill",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved"


def test_prompt_path_pass_through():
    """prompt 路径 → 非审查类型 → approved。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="prompts/code-review.md",
        commit_sha="x",
        content="# prompt",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved"


# ---------------------------------------------------------------------------
# rule 类型：仅 CODEOWNERS
# ---------------------------------------------------------------------------


def test_rule_with_codeowners_approved():
    """rule 路径 + CODEOWNERS 审批 → approved（无需签名）。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="rules/lint.md",
        commit_sha="x",
        content="# lint rule",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved"
    assert not r.signature_present  # rule 不检查签名


def test_rule_without_codeowners_rejected():
    """rule 路径 无 trusted reviewer → rejected。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="rules/lint.md",
        commit_sha="x",
        content="# lint rule",
        reviewers=["charlie"],
        approvers=["charlie"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert "trusted reviewer" in r.reason


# ---------------------------------------------------------------------------
# tool 类型：CODEOWNERS + 签名（强制两项）
# ---------------------------------------------------------------------------


def test_tool_without_signature_rejected():
    """tool 路径 无 signature 字段 → rejected。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="tools/deploy.sh",
        commit_sha="x",
        content="#!/bin/sh\necho hi",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert not r.signature_present
    assert "signature" in r.reason.lower()


def test_tool_with_signature_no_codeowners_rejected():
    """tool 路径 有签名 无 CODEOWNERS → rejected。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="tools/deploy.sh",
        commit_sha="x",
        content="---\nsignature: fake-sig\n---\n#!/bin/sh",
        reviewers=["charlie"],
        approvers=["charlie"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert r.signature_present  # 字段存在
    assert "trusted reviewer" in r.reason


def test_tool_with_signature_and_codeowners_approved():
    """tool 路径 有签名 + CODEOWNERS → approved（真实 Ed25519 验签通过）。"""
    import base64
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives import serialization

    # 生成密钥对
    priv = Ed25519PrivateKey.generate()
    pub = priv.public_key()
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )

    # body 是 frontmatter 之后的部分
    body = "#!/bin/sh\necho hi"
    sig_bytes = priv.sign(body.encode("utf-8"))
    valid_b64_sig = base64.b64encode(sig_bytes).decode()

    svc = _make_svc(public_key=pub_bytes)
    f = PRFileInfo(
        asset_path="tools/deploy.sh",
        commit_sha="x",
        content=f"---\nsignature: {valid_b64_sig}\n---\n{body}",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved", f"expected approved, got {r.decision}: {r.reason}"
    assert r.signature_valid


def test_tool_wrong_signature_algorithm():
    """tool 签名算法非 ed25519 → rejected。"""
    svc = _make_svc()
    f = PRFileInfo(
        asset_path="tools/deploy.sh",
        commit_sha="x",
        content="---\nsignature: fake-sig\nsignature_algorithm: rsa\n---\n#!/bin/sh",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert "算法" in r.reason or "algorithm" in r.reason.lower()


def test_tool_no_public_key_configured():
    """tool 有签名但未配置公钥 → rejected。"""
    svc = _make_svc(public_key=None)
    f = PRFileInfo(
        asset_path="tools/deploy.sh",
        commit_sha="x",
        content="---\nsignature: fake-sig\n---\n#!/bin/sh",
        reviewers=["alice"],
        approvers=["alice"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert "公钥" in r.reason


# ---------------------------------------------------------------------------
# 多 reviewer 计数
# ---------------------------------------------------------------------------


def test_min_trusted_reviewers_threshold():
    """min_trusted_reviewers=2，只 1 人审批 → rejected。"""
    svc = _make_svc(min_reviewers=2)
    f = PRFileInfo(
        asset_path="rules/lint.md",
        commit_sha="x",
        content="# lint",
        reviewers=["alice"],
        approvers=["alice"],  # 只有 1 个 trusted
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert "1/2" in r.reason


def test_min_trusted_reviewers_met():
    """min_trusted_reviewers=2，2 人审批 → approved。"""
    svc = _make_svc(min_reviewers=2)
    f = PRFileInfo(
        asset_path="rules/lint.md",
        commit_sha="x",
        content="# lint",
        reviewers=["alice", "bob"],
        approvers=["alice", "bob"],
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "approved"


def test_non_trusted_approver_not_counted():
    """非 trusted 的 approver 不计入计数。"""
    svc = _make_svc(trusted={"alice"}, min_reviewers=1)
    f = PRFileInfo(
        asset_path="rules/lint.md",
        commit_sha="x",
        content="# lint",
        reviewers=["charlie", "dave"],
        approvers=["charlie", "dave"],  # 都不是 trusted
    )
    r = svc.review_file(f, pr_id="")
    assert r.decision == "rejected"
    assert "0/1" in r.reason


# ---------------------------------------------------------------------------
# review_pr 批量审查
# ---------------------------------------------------------------------------


def test_review_pr_multiple_files():
    """review_pr 审查多文件，任一 rejected 不影响其他文件结果。"""
    svc = _make_svc()
    files = [
        PRFileInfo(asset_path="memory/note.md", commit_sha="x", content="# note", approvers=["alice"]),
        PRFileInfo(asset_path="rules/lint.md", commit_sha="x", content="# lint", approvers=["alice"]),
        PRFileInfo(asset_path="tools/deploy.sh", commit_sha="x", content="#!/bin/sh", approvers=["alice"]),
    ]
    results = svc.review_pr(pr_id="", files=files)
    assert len(results) == 3
    assert results[0].decision == "approved"  # memory
    assert results[1].decision == "approved"  # rule + codeowners
    assert results[2].decision == "rejected"  # tool 无签名
