"""SubTask 5.10 测试：API Key 颁发/轮换/反查。"""

from __future__ import annotations

import pytest

from server.binding.auth_service import AgentApiKeyService, IssuedKey


class TestIssueApiKey:
    """API Key 颁发。"""

    def test_issue_returns_raw_key(self, auth_service):
        """颁发返回原始 key（仅本次）。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert isinstance(issued, IssuedKey)
        assert issued.api_key.startswith("th_")
        assert issued.agent_id == "agent-1"
        assert issued.key_id.startswith("key-")
        assert issued.key_prefix == issued.api_key[:8]
        # 原始 key 长度：th_ + 32 hex = 35 字符
        assert len(issued.api_key) == 3 + 32

    def test_issue_multiple_keys_same_agent(self, auth_service):
        """同一 agent 可持多个 active key。"""
        k1 = auth_service.issue(member_id="alice", agent_id="agent-1")
        k2 = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert k1.api_key != k2.api_key
        assert k1.key_id != k2.key_id
        keys = auth_service.list_keys(agent_id="agent-1", status="active")
        assert len(keys) == 2

    def test_key_hash_not_raw_key(self, auth_service):
        """数据库存的是 sha256(key)，不是原始 key。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        keys = auth_service.list_keys(agent_id="agent-1")
        assert len(keys) == 1
        # key_hash 不应等于原始 key
        assert keys[0].key_hash != issued.api_key
        # key_hash 长度 = sha256 = 64 hex
        assert len(keys[0].key_hash) == 64
        # 原始 key 不应出现在任何字段
        assert keys[0].key_prefix != issued.api_key


class TestLookupAgentId:
    """API Key 反查 agent_id（鉴权入口）。"""

    def test_lookup_active_key(self, auth_service):
        """active key 反查成功。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        agent_id = auth_service.lookup_agent_id(issued.api_key)
        assert agent_id == "agent-1"

    def test_lookup_invalid_key(self, auth_service):
        """非法 key → None。"""
        assert auth_service.lookup_agent_id("th_nonexistent") is None

    def test_lookup_empty_key(self, auth_service):
        """空 key → None。"""
        assert auth_service.lookup_agent_id("") is None

    def test_lookup_rotated_key_rejected(self, auth_service):
        """轮换后旧 key 反查返回 None。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        # 轮换
        auth_service.rotate(issued.key_id)
        # 旧 key 反查 → None
        assert auth_service.lookup_agent_id(issued.api_key) is None

    def test_lookup_revoked_key_rejected(self, auth_service):
        """注销后 key 反查返回 None。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert auth_service.revoke(issued.key_id) is True
        assert auth_service.lookup_agent_id(issued.api_key) is None

    def test_lookup_updates_last_used_at(self, auth_service):
        """反查成功时更新 last_used_at。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert auth_service.lookup_agent_id(issued.api_key) == "agent-1"
        keys = auth_service.list_keys(agent_id="agent-1")
        assert keys[0].last_used_at is not None


class TestRotateApiKey:
    """API Key 轮换。"""

    def test_rotate_returns_new_key(self, auth_service):
        """轮换返回新 key，旧 key 失效。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        new = auth_service.rotate(issued.key_id)
        assert new.api_key != issued.api_key
        assert new.key_id != issued.key_id
        assert new.agent_id == issued.agent_id
        # 新 key 反查成功
        assert auth_service.lookup_agent_id(new.api_key) == "agent-1"
        # 旧 key 反查失败
        assert auth_service.lookup_agent_id(issued.api_key) is None

    def test_rotate_sets_rotated_from(self, auth_service):
        """新 key 的 rotated_from 指向旧 key_id。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        new = auth_service.rotate(issued.key_id)
        new_key = auth_service.lookup_by_id(new.key_id)
        assert new_key is not None
        assert new_key.rotated_from == issued.key_id
        # 旧 key 状态为 rotated
        old_key = auth_service.lookup_by_id(issued.key_id)
        assert old_key.status == "rotated"
        assert old_key.rotated_at is not None

    def test_rotate_nonexistent_raises(self, auth_service):
        """轮换不存在的 key_id → ValueError。"""
        with pytest.raises(ValueError, match="不存在"):
            auth_service.rotate("key-nonexistent")

    def test_rotate_rotated_key_raises(self, auth_service):
        """轮换已 rotated 的 key → ValueError。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        auth_service.rotate(issued.key_id)
        with pytest.raises(ValueError, match="非 active"):
            auth_service.rotate(issued.key_id)

    def test_rotate_chain(self, auth_service):
        """链式轮换：k1 → k2 → k3，仅 k3 active。"""
        k1 = auth_service.issue(member_id="alice", agent_id="agent-1")
        k2 = auth_service.rotate(k1.key_id)
        k3 = auth_service.rotate(k2.key_id)
        # k3 active
        assert auth_service.lookup_agent_id(k3.api_key) == "agent-1"
        # k1, k2 已 rotated
        assert auth_service.lookup_agent_id(k1.api_key) is None
        assert auth_service.lookup_agent_id(k2.api_key) is None
        active_keys = auth_service.list_keys(agent_id="agent-1", status="active")
        assert len(active_keys) == 1
        assert active_keys[0].id == k3.key_id


class TestRevokeApiKey:
    """API Key 注销。"""

    def test_revoke_active_key(self, auth_service):
        """注销 active key。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert auth_service.revoke(issued.key_id) is True
        key = auth_service.lookup_by_id(issued.key_id)
        assert key.status == "revoked"
        assert key.revoked_at is not None
        assert auth_service.lookup_agent_id(issued.api_key) is None

    def test_revoke_already_revoked_returns_false(self, auth_service):
        """重复注销返回 False。"""
        issued = auth_service.issue(member_id="alice", agent_id="agent-1")
        assert auth_service.revoke(issued.key_id) is True
        assert auth_service.revoke(issued.key_id) is False

    def test_revoke_nonexistent_returns_false(self, auth_service):
        """注销不存在的 key_id → False。"""
        assert auth_service.revoke("key-nonexistent") is False


class TestListKeys:
    """API Key 列表查询。"""

    def test_list_by_member(self, auth_service):
        """按 member_id 过滤。"""
        auth_service.issue(member_id="alice", agent_id="a1")
        auth_service.issue(member_id="bob", agent_id="a2")
        keys = auth_service.list_keys(member_id="alice")
        assert len(keys) == 1
        assert keys[0].member_id == "alice"

    def test_list_by_status(self, auth_service):
        """按 status 过滤。"""
        k1 = auth_service.issue(member_id="alice", agent_id="a1")
        auth_service.issue(member_id="alice", agent_id="a1")
        auth_service.revoke(k1.key_id)
        active = auth_service.list_keys(status="active")
        revoked = auth_service.list_keys(status="revoked")
        assert len(active) == 1
        assert len(revoked) == 1
