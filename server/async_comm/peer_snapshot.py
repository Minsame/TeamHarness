"""PeerSnapshot 管理模块。

负责 peer 的 harness 本地快照的拉取、刷新、过期清理。每个 peer 的快照
存储在 `{base_dir}/peer_snapshots/{peer_id}/` 下，目录结构：

- ``harness/`` 子目录：peer 的 harness 文件副本（会话/规则/记忆等资产）
- ``manifest.json``：peer 的 manifest 副本（资产清单）
- ``vector_clock.json``：peer 的版本向量
- ``snapshot_meta.json``：快照元信息（snapshot_version, captured_at, peer_id, ttl_days）

过期判断基于 ``captured_at`` 时间戳与 ``ttl_days``（默认 30 天）比较。
快照策略：``on_demand``（按需，调用时才拉取）/ ``scheduled``（定时刷新，
由 daemon 调度）。

对应 Task 14。
"""

from __future__ import annotations

import json
import logging
import re
import shutil
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server.async_comm.constants import (
    DEFAULT_SNAPSHOT_POLICY,
    DEFAULT_SNAPSHOT_TTL_DAYS,
)
from server.async_comm.types import PeerSnapshot, VectorClock

logger = logging.getLogger(__name__)

# 快照根目录下的子目录名（所有 peer 快照都存于此）
PEER_SNAPSHOTS_DIRNAME = "peer_snapshots"
# 单个 peer 快照目录下的子目录/文件名
HARNESS_DIRNAME = "harness"
MANIFEST_FILENAME = "manifest.json"
VECTOR_CLOCK_FILENAME = "vector_clock.json"
SNAPSHOT_META_FILENAME = "snapshot_meta.json"

# snapshot_meta.json 的字段名
META_PEER_ID = "peer_id"
META_SNAPSHOT_VERSION = "snapshot_version"
META_CAPTURED_AT = "captured_at"
META_TTL_DAYS = "ttl_days"

# 版本号正则：v1, v2, v38 ...
_VERSION_RE = re.compile(r"^v(\d+)$")


def _utc_now_iso() -> str:
    """返回当前 UTC 时间的 ISO 字符串（含时区后缀 Z）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_iso(iso_str: str) -> datetime | None:
    """解析 ISO 时间字符串为 aware datetime。

    支持末尾 ``Z`` 与 ``+HH:MM`` 偏移；解析失败返回 None。
    """
    if not iso_str:
        return None
    raw = iso_str.strip()
    # 兼容末尾 Z（UTC）
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    # 若 naive，假定为 UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _atomic_write_text(path: Path, content: str) -> None:
    """原子写入文本文件：先写 .tmp 再 rename。

    防止写一半崩溃导致文件损坏。父目录不存在时自动创建。
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


def _atomic_write_json(path: Path, data: dict) -> None:
    """原子写入 JSON 文件（UTF-8，缩进 2，保留非 ASCII）。"""
    text = json.dumps(data, ensure_ascii=False, indent=2)
    _atomic_write_text(path, text)


def _read_json(path: Path) -> dict | None:
    """读取 JSON 文件；文件不存在或解析失败返回 None。"""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("读取 JSON 失败 %s: %s", path, exc)
        return None


class PeerSnapshotManager:
    """Peer harness 本地快照管理器。

    负责 peer 快照的拉取、刷新、过期清理。线程不安全，调用方需自行加锁
    或保证单线程访问。
    """

    def __init__(
        self,
        base_dir: Path,
        *,
        ttl_days: int = DEFAULT_SNAPSHOT_TTL_DAYS,
        snapshot_policy: str = DEFAULT_SNAPSHOT_POLICY,
    ) -> None:
        """初始化 PeerSnapshot 管理器。

        Args:
            base_dir: 快照根目录（如 ``.teamharness/async_comm/``）。
            ttl_days: 快照过期天数（默认 30）。
            snapshot_policy: 快照策略（``on_demand`` / ``scheduled``）。
        """
        self._base_dir = Path(base_dir)
        self._snapshots_root = self._base_dir / PEER_SNAPSHOTS_DIRNAME
        self._ttl_days = int(ttl_days)
        self._snapshot_policy = str(snapshot_policy)
        # 确保根目录存在（mkdir parents 不报错已存在）
        self._snapshots_root.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # 路径辅助
    # ------------------------------------------------------------------

    def _peer_dir(self, peer_id: str) -> Path:
        """返回某 peer 的快照根目录路径。"""
        return self._snapshots_root / peer_id

    def _harness_dir(self, peer_id: str) -> Path:
        """返回某 peer 快照的 harness 子目录路径。"""
        return self._peer_dir(peer_id) / HARNESS_DIRNAME

    def _manifest_path(self, peer_id: str) -> Path:
        """返回某 peer 快照的 manifest.json 路径。"""
        return self._peer_dir(peer_id) / MANIFEST_FILENAME

    def _vector_clock_path(self, peer_id: str) -> Path:
        """返回某 peer 快照的 vector_clock.json 路径。"""
        return self._peer_dir(peer_id) / VECTOR_CLOCK_FILENAME

    def _meta_path(self, peer_id: str) -> Path:
        """返回某 peer 快照的 snapshot_meta.json 路径。"""
        return self._peer_dir(peer_id) / SNAPSHOT_META_FILENAME

    # ------------------------------------------------------------------
    # 元信息读写
    # ------------------------------------------------------------------

    def _read_meta(self, peer_id: str) -> dict | None:
        """读取 peer 的 snapshot_meta.json；不存在返回 None。"""
        return _read_json(self._meta_path(peer_id))

    def _next_version(self, peer_id: str) -> str:
        """根据现有 meta 计算下一个版本号（v1, v2, ...）。

        无 meta 或解析失败时返回 ``v1``。
        """
        meta = self._read_meta(peer_id)
        if not meta:
            return "v1"
        prev = str(meta.get(META_SNAPSHOT_VERSION, "")).strip()
        match = _VERSION_RE.match(prev)
        if not match:
            return "v1"
        return f"v{int(match.group(1)) + 1}"

    def _load_vector_clock_from_path(self, path: Path) -> VectorClock:
        """从指定路径加载 VectorClock；文件不存在/无效返回空 VectorClock。"""
        data = _read_json(path)
        if not isinstance(data, dict):
            return VectorClock()
        # vector_clock.json 顶层即 {peer_id: counter}
        try:
            return VectorClock.from_dict(data)
        except (TypeError, ValueError) as exc:
            logger.warning("反序列化 VectorClock 失败 %s: %s", path, exc)
            return VectorClock()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def get_snapshot(self, peer_id: str) -> PeerSnapshot | None:
        """获取 peer 的本地快照（不拉取，仅读本地）。

        不存在返回 None；存在则返回 PeerSnapshot（填充 harness_path 等路径）。
        """
        peer_dir = self._peer_dir(peer_id)
        if not peer_dir.is_dir():
            return None
        meta = self._read_meta(peer_id)
        if not meta:
            # 目录存在但无 meta，视为不存在
            return None
        vc = self._load_vector_clock_from_path(self._vector_clock_path(peer_id))
        return PeerSnapshot(
            peer_id=peer_id,
            snapshot_version=str(meta.get(META_SNAPSHOT_VERSION, "")),
            captured_at=str(meta.get(META_CAPTURED_AT, "")),
            harness_path=self._harness_dir(peer_id),
            manifest_path=self._manifest_path(peer_id),
            vector_clock_path=self._vector_clock_path(peer_id),
            vector_clock=vc,
        )

    def refresh_snapshot(
        self,
        peer_id: str,
        *,
        harness_files: dict[str, str] | None = None,
        manifest: dict | None = None,
        vector_clock: VectorClock | None = None,
    ) -> PeerSnapshot:
        """刷新 peer 的本地快照（写入/覆盖）。

        Args:
            peer_id: peer 标识。
            harness_files: ``{相对路径: 文件内容}`` 写入 harness/ 子目录。
            manifest: 写入 manifest.json。
            vector_clock: 写入 vector_clock.json。

        生成新的 snapshot_version（如 v1, v2 递增），captured_at 设为当前 UTC ISO。
        返回更新后的 PeerSnapshot。
        """
        peer_dir = self._peer_dir(peer_id)
        peer_dir.mkdir(parents=True, exist_ok=True)

        # 1. 写 harness_files（先清空 harness 子目录，避免残留旧文件）
        harness_dir = self._harness_dir(peer_id)
        if harness_dir.is_dir():
            shutil.rmtree(harness_dir)
        harness_dir.mkdir(parents=True, exist_ok=True)
        if harness_files:
            for rel_path, content in harness_files.items():
                # 防止路径穿越（如 ../../etc/passwd）
                rel = Path(rel_path)
                if rel.is_absolute() or any(
                    part == ".." for part in rel.parts
                ):
                    logger.warning(
                        "跳过非法 harness 相对路径 %r (peer=%s)",
                        rel_path,
                        peer_id,
                    )
                    continue
                target = harness_dir / rel
                _atomic_write_text(target, content)

        # 2. 写 manifest.json
        if manifest is not None:
            _atomic_write_json(self._manifest_path(peer_id), manifest)

        # 3. 写 vector_clock.json
        vc = vector_clock if vector_clock is not None else VectorClock()
        _atomic_write_json(self._vector_clock_path(peer_id), vc.to_dict())

        # 4. 写 snapshot_meta.json（版本递增 + 当前时间）
        version = self._next_version(peer_id)
        captured_at = _utc_now_iso()
        meta = {
            META_PEER_ID: peer_id,
            META_SNAPSHOT_VERSION: version,
            META_CAPTURED_AT: captured_at,
            META_TTL_DAYS: self._ttl_days,
        }
        _atomic_write_json(self._meta_path(peer_id), meta)

        return PeerSnapshot(
            peer_id=peer_id,
            snapshot_version=version,
            captured_at=captured_at,
            harness_path=harness_dir,
            manifest_path=self._manifest_path(peer_id),
            vector_clock_path=self._vector_clock_path(peer_id),
            vector_clock=vc,
        )

    def is_stale(self, peer_id: str) -> bool:
        """判断 peer 快照是否过期（超过 ttl_days）。

        无快照返回 True（视为过期）。
        """
        meta = self._read_meta(peer_id)
        if not meta:
            return True
        captured_at = str(meta.get(META_CAPTURED_AT, ""))
        captured_dt = _parse_iso(captured_at)
        if captured_dt is None:
            # 时间解析失败，保守视为过期
            return True
        now = datetime.now(timezone.utc)
        # ttl_days <= 0 表示立即过期
        if self._ttl_days <= 0:
            return True
        return (now - captured_dt) > timedelta(days=self._ttl_days)

    def expire_stale(self) -> list[str]:
        """清理所有过期快照（删除目录）。

        返回被清理的 peer_id 列表。
        """
        expired: list[str] = []
        if not self._snapshots_root.is_dir():
            return expired
        for entry in self._snapshots_root.iterdir():
            if not entry.is_dir():
                continue
            peer_id = entry.name
            if self.is_stale(peer_id):
                try:
                    shutil.rmtree(entry)
                    expired.append(peer_id)
                except OSError as exc:
                    logger.warning(
                        "删除 peer 快照目录失败 %s: %s", entry, exc
                    )
        return expired

    def list_snapshots(self) -> list[PeerSnapshot]:
        """列出所有本地快照。"""
        result: list[PeerSnapshot] = []
        if not self._snapshots_root.is_dir():
            return result
        for entry in sorted(self._snapshots_root.iterdir()):
            if not entry.is_dir():
                continue
            snap = self.get_snapshot(entry.name)
            if snap is not None:
                result.append(snap)
        return result

    def get_snapshot_version(self, peer_id: str) -> str:
        """获取 peer 快照的当前版本号（如 ``v38``）。

        无快照返回空字符串。
        """
        meta = self._read_meta(peer_id)
        if not meta:
            return ""
        return str(meta.get(META_SNAPSHOT_VERSION, ""))

    def load_vector_clock(self, peer_id: str) -> VectorClock:
        """加载 peer 快照中的 vector_clock。

        无快照或无 vector_clock 文件返回空 VectorClock()。
        """
        return self._load_vector_clock_from_path(self._vector_clock_path(peer_id))


__all__ = ["PeerSnapshotManager"]
