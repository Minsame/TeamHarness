"""AgentApiKeyService — API Key 颁发 / 轮换 / 反查。

对应 SubTask 5.10：
- 颁发：生成随机 key，存储 sha256(key)（不存原始 key），返回给调用方一次
- 轮换：旧 key 标记 status=rotated + rotated_at，颁发新 key，新 key.rotated_from=旧 id
- 反查：通过原始 key 计算 sha256 → 查 key_hash → 返回 agent_id
- 注销：status=revoked + revoked_at，反查时拒绝

设计要点：
- key 格式：th_<32位 hex>（前缀便于识别）
- key_hash 存储 sha256(key)，数据库泄露不暴露原始 key
- key_prefix 存储 key 前 8 字符明文（人类识别用，如 "th_a1b2c3"）
- 反查时 status != active → 拒绝
"""

from __future__ import annotations

import hashlib
import logging
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select, update

from server.infra_db.db import Database
from server.binding.models import AgentApiKey

logger = logging.getLogger(__name__)

# key 随机部分长度（hex 字符数），32 字节 = 64 hex；为可读性取 32 hex（16 字节熵）
KEY_RANDOM_HEX_LEN = 32
KEY_PREFIX_LEN = 8  # key_prefix 字段保留前 8 字符


@dataclass
class IssuedKey:
    """颁发的 API Key（含原始 key，仅返回一次）。"""

    api_key: str  # 原始 key，客户端持有
    agent_id: str
    key_id: str
    key_prefix: str


class AgentApiKeyService:
    """API Key 颁发/轮换/反查服务。

    用法：
        svc = AgentApiKeyService(database)
        issued = svc.issue(member_id="alice", agent_id="agent-1")
        # 颁发后客户端持有 issued.api_key
        agent_id = svc.lookup_agent_id(issued.api_key)  # 反查
        rotated = svc.rotate(issued.key_id)  # 轮换
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    # ------------------------------------------------------------------
    # 颁发
    # ------------------------------------------------------------------

    def issue(
        self,
        *,
        member_id: str,
        agent_id: str,
    ) -> IssuedKey:
        """颁发新 API Key。

        - 同一 (member_id, agent_id) 已有 active key → 仍颁发新 key（允许多 key 并存）
          （调用方需先用 revoke 显式注销旧 key，或用 rotate 轮换）
        - 返回 IssuedKey（含原始 key，仅本次返回）
        """
        api_key = self._generate_key()
        key_hash = self._hash_key(api_key)
        key_id = f"key-{uuid.uuid4().hex[:12]}"
        with self._db.session() as sess:
            sess.add(
                AgentApiKey(
                    id=key_id,
                    agent_id=agent_id,
                    member_id=member_id,
                    key_hash=key_hash,
                    key_prefix=api_key[:KEY_PREFIX_LEN],
                    status="active",
                    rotated_from=None,
                    issued_at=datetime.now(timezone.utc),
                )
            )
        return IssuedKey(
            api_key=api_key,
            agent_id=agent_id,
            key_id=key_id,
            key_prefix=api_key[:KEY_PREFIX_LEN],
        )

    # ------------------------------------------------------------------
    # 轮换
    # ------------------------------------------------------------------

    def rotate(self, old_key_id: str) -> IssuedKey:
        """轮换：旧 key 标记 rotated，颁发新 key（rotated_from 指向旧 id）。

        旧 key 立即失效（status=rotated），不能再用于反查。
        """
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            old = sess.get(AgentApiKey, old_key_id)
            if old is None:
                raise ValueError(f"key_id 不存在：{old_key_id}")
            if old.status != "active":
                raise ValueError(f"key_id 状态非 active：{old.status}")
            # 标记旧 key
            old.status = "rotated"
            old.rotated_at = now
            # 颁发新 key
            new_key = self._generate_key()
            new_key_id = f"key-{uuid.uuid4().hex[:12]}"
            sess.add(
                AgentApiKey(
                    id=new_key_id,
                    agent_id=old.agent_id,
                    member_id=old.member_id,
                    key_hash=self._hash_key(new_key),
                    key_prefix=new_key[:KEY_PREFIX_LEN],
                    status="active",
                    rotated_from=old_key_id,
                    issued_at=now,
                )
            )
        return IssuedKey(
            api_key=new_key,
            agent_id=old.agent_id,
            key_id=new_key_id,
            key_prefix=new_key[:KEY_PREFIX_LEN],
        )

    # ------------------------------------------------------------------
    # 注销
    # ------------------------------------------------------------------

    def revoke(self, key_id: str) -> bool:
        """注销 API Key（不可逆）。"""
        now = datetime.now(timezone.utc)
        with self._db.session() as sess:
            result = sess.execute(
                update(AgentApiKey)
                .where(
                    AgentApiKey.id == key_id,
                    AgentApiKey.status == "active",
                )
                .values(status="revoked", revoked_at=now)
            )
            return result.rowcount > 0

    # ------------------------------------------------------------------
    # 反查（鉴权用）
    # ------------------------------------------------------------------

    def lookup_agent_id(self, api_key: str) -> str | None:
        """通过 API Key 反查 agent_id（鉴权入口）。

        - key 不存在 → None
        - key 已 rotated/revoked → None
        - key active → 返回 agent_id 并更新 last_used_at
        """
        if not api_key:
            return None
        key_hash = self._hash_key(api_key)
        with self._db.session() as sess:
            row = sess.scalars(
                select(AgentApiKey).where(AgentApiKey.key_hash == key_hash)
            ).first()
            if row is None:
                return None
            if row.status != "active":
                return None
            # 更新 last_used_at（不阻塞鉴权）
            row.last_used_at = datetime.now(timezone.utc)
            return row.agent_id

    def lookup_member_id(self, api_key: str) -> str | None:
        """通过 API Key 反查 member_id（资产鉴权用）。

        与 lookup_agent_id 同语义，仅返回字段不同：
        - key 不存在 / 非 active → None
        - key active → 返回 member_id 并更新 last_used_at
        """
        if not api_key:
            return None
        key_hash = self._hash_key(api_key)
        with self._db.session() as sess:
            row = sess.scalars(
                select(AgentApiKey).where(AgentApiKey.key_hash == key_hash)
            ).first()
            if row is None:
                return None
            if row.status != "active":
                return None
            row.last_used_at = datetime.now(timezone.utc)
            return row.member_id

    def lookup_by_id(self, key_id: str) -> AgentApiKey | None:
        """按 key_id 查询（管理用）。"""
        with self._db.session() as sess:
            return sess.get(AgentApiKey, key_id)

    def list_keys(
        self, *, member_id: str | None = None, agent_id: str | None = None, status: str | None = None
    ) -> list[AgentApiKey]:
        """查询 API Key 列表（不返回原始 key，仅元数据）。"""
        with self._db.session() as sess:
            stmt = select(AgentApiKey)
            if member_id:
                stmt = stmt.where(AgentApiKey.member_id == member_id)
            if agent_id:
                stmt = stmt.where(AgentApiKey.agent_id == agent_id)
            if status:
                stmt = stmt.where(AgentApiKey.status == status)
            stmt = stmt.order_by(AgentApiKey.issued_at.desc())
            return list(sess.scalars(stmt))

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _generate_key() -> str:
        """生成 API Key：th_ + 32 hex 随机字符。"""
        return f"th_{secrets.token_hex(KEY_RANDOM_HEX_LEN // 2)}"

    @staticmethod
    def _hash_key(api_key: str) -> str:
        """sha256(key) 存储（不存原始 key）。"""
        return hashlib.sha256(api_key.encode("utf-8")).hexdigest()


__all__ = [
    "AgentApiKeyService",
    "IssuedKey",
]
