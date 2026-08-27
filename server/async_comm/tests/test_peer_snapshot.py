"""Task 14 测试：PeerSnapshotManager 快照管理。

覆盖 refresh / get / stale / expire / list / load_vector_clock 等场景。
使用 tmp_path fixture 做测试隔离；过期测试用 ttl_days=0 或 mock 时间。
"""

from __future__ import annotations

import json
from pathlib import Path

from server.async_comm.peer_snapshot import (
    HARNESS_DIRNAME,
    MANIFEST_FILENAME,
    PEER_SNAPSHOTS_DIRNAME,
    SNAPSHOT_META_FILENAME,
    VECTOR_CLOCK_FILENAME,
    PeerSnapshotManager,
)
from server.async_comm.types import PeerSnapshot, VectorClock


class TestPeerSnapshotManagerRefresh:
    """refresh_snapshot 刷新快照。"""

    def test_refresh_creates_snapshot_directory(self, tmp_path: Path):
        """refresh 后创建 peer_snapshots/{peer_id}/ 目录及 harness 子目录。"""
        mgr = PeerSnapshotManager(tmp_path)
        snap = mgr.refresh_snapshot("bob")

        peer_dir = tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob"
        assert peer_dir.is_dir()
        assert (peer_dir / HARNESS_DIRNAME).is_dir()
        assert (peer_dir / SNAPSHOT_META_FILENAME).is_file()
        assert snap.peer_id == "bob"
        assert snap.snapshot_version == "v1"
        assert snap.captured_at != ""
        # 路径字段被填充
        assert snap.harness_path == peer_dir / HARNESS_DIRNAME
        assert snap.manifest_path == peer_dir / MANIFEST_FILENAME
        assert snap.vector_clock_path == peer_dir / VECTOR_CLOCK_FILENAME

    def test_refresh_version_increments(self, tmp_path: Path):
        """多次 refresh 版本号递增 v1, v2, v3。"""
        mgr = PeerSnapshotManager(tmp_path)
        v1 = mgr.refresh_snapshot("alice")
        v2 = mgr.refresh_snapshot("alice")
        v3 = mgr.refresh_snapshot("alice")
        assert v1.snapshot_version == "v1"
        assert v2.snapshot_version == "v2"
        assert v3.snapshot_version == "v3"

    def test_refresh_writes_harness_files(self, tmp_path: Path):
        """harness_files 按相对路径写入 harness/ 子目录。"""
        mgr = PeerSnapshotManager(tmp_path)
        snap = mgr.refresh_snapshot(
            "bob",
            harness_files={
                "rules/gotchas.md": "# 规则\n",
                "memory/INDEX.md": "# 索引\n",
            },
        )
        assert snap.harness_path.is_dir()
        assert (snap.harness_path / "rules" / "gotchas.md").read_text(
            encoding="utf-8"
        ) == "# 规则\n"
        assert (snap.harness_path / "memory" / "INDEX.md").read_text(
            encoding="utf-8"
        ) == "# 索引\n"

    def test_refresh_writes_manifest(self, tmp_path: Path):
        """manifest 写入 manifest.json，可读回。"""
        mgr = PeerSnapshotManager(tmp_path)
        manifest = {"version": "1.0", "assets": ["rules", "memory"]}
        snap = mgr.refresh_snapshot("bob", manifest=manifest)
        assert snap.manifest_path.is_file()
        data = json.loads(snap.manifest_path.read_text(encoding="utf-8"))
        assert data == manifest

    def test_refresh_writes_vector_clock(self, tmp_path: Path):
        """vector_clock 写入 vector_clock.json，可读回。"""
        mgr = PeerSnapshotManager(tmp_path)
        vc = VectorClock(counters={"alice": 3, "bob": 5})
        snap = mgr.refresh_snapshot("bob", vector_clock=vc)
        assert snap.vector_clock_path.is_file()
        data = json.loads(snap.vector_clock_path.read_text(encoding="utf-8"))
        assert data == {"alice": 3, "bob": 5}
        # 返回的 PeerSnapshot 也带 vector_clock
        assert snap.vector_clock.counters == {"alice": 3, "bob": 5}

    def test_refresh_overwrites_harness_files(self, tmp_path: Path):
        """再次 refresh 时 harness 目录被清空重写（旧文件不残留）。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot(
            "bob",
            harness_files={"old.md": "old", "keep.md": "keep"},
        )
        snap = mgr.refresh_snapshot(
            "bob",
            harness_files={"new.md": "new"},
        )
        # 旧文件 old.md / keep.md 应已不存在
        assert not (snap.harness_path / "old.md").exists()
        assert not (snap.harness_path / "keep.md").exists()
        assert (snap.harness_path / "new.md").read_text(encoding="utf-8") == "new"

    def test_refresh_writes_snapshot_meta(self, tmp_path: Path):
        """snapshot_meta.json 含 peer_id / snapshot_version / captured_at / ttl_days。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=15)
        mgr.refresh_snapshot("bob")
        meta_path = (
            tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob" / SNAPSHOT_META_FILENAME
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        assert meta["peer_id"] == "bob"
        assert meta["snapshot_version"] == "v1"
        assert "captured_at" in meta
        assert meta["ttl_days"] == 15

    def test_refresh_no_harness_files_creates_empty_harness_dir(self, tmp_path: Path):
        """不传 harness_files 时 harness/ 目录仍被创建（空）。"""
        mgr = PeerSnapshotManager(tmp_path)
        snap = mgr.refresh_snapshot("bob")
        assert snap.harness_path.is_dir()
        assert list(snap.harness_path.iterdir()) == []

    def test_refresh_skips_path_traversal(self, tmp_path: Path):
        """harness_files 中含 .. 或绝对路径时跳过（防路径穿越）。"""
        mgr = PeerSnapshotManager(tmp_path)
        # 绝对路径与 .. 路径应被跳过，正常路径应被写入
        abs_path = str(tmp_path / "evil.txt")
        snap = mgr.refresh_snapshot(
            "bob",
            harness_files={
                "safe.md": "safe",
                "../escape.txt": "escape",
                abs_path: "abs",
            },
        )
        assert (snap.harness_path / "safe.md").read_text(encoding="utf-8") == "safe"
        assert not (snap.harness_path / "../escape.txt").exists()
        assert not (tmp_path / "evil.txt").exists()


class TestPeerSnapshotManagerGet:
    """get_snapshot / get_snapshot_version 读取。"""

    def test_get_snapshot_returns_peer_snapshot(self, tmp_path: Path):
        """存在快照时 get_snapshot 返回 PeerSnapshot。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot("bob", manifest={"a": 1})
        snap = mgr.get_snapshot("bob")
        assert snap is not None
        assert isinstance(snap, PeerSnapshot)
        assert snap.peer_id == "bob"
        assert snap.snapshot_version == "v1"
        assert snap.captured_at != ""

    def test_get_snapshot_returns_none_when_missing(self, tmp_path: Path):
        """不存在快照时 get_snapshot 返回 None。"""
        mgr = PeerSnapshotManager(tmp_path)
        assert mgr.get_snapshot("unknown") is None

    def test_get_snapshot_returns_none_for_dir_without_meta(self, tmp_path: Path):
        """目录存在但无 meta 时返回 None（视为不存在）。"""
        mgr = PeerSnapshotManager(tmp_path)
        # 手动创建空 peer 目录，不写 meta
        (tmp_path / PEER_SNAPSHOTS_DIRNAME / "ghost").mkdir(parents=True)
        assert mgr.get_snapshot("ghost") is None

    def test_get_snapshot_version_empty_when_no_snapshot(self, tmp_path: Path):
        """get_snapshot_version 无快照返回空字符串。"""
        mgr = PeerSnapshotManager(tmp_path)
        assert mgr.get_snapshot_version("unknown") == ""

    def test_get_snapshot_version_returns_current_version(self, tmp_path: Path):
        """get_snapshot_version 返回当前版本号。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot("alice")
        mgr.refresh_snapshot("alice")
        mgr.refresh_snapshot("alice")
        assert mgr.get_snapshot_version("alice") == "v3"


class TestPeerSnapshotManagerStale:
    """is_stale 过期判断。"""

    def test_is_stale_true_when_no_snapshot(self, tmp_path: Path):
        """无快照返回 True（视为过期）。"""
        mgr = PeerSnapshotManager(tmp_path)
        assert mgr.is_stale("unknown") is True

    def test_is_stale_false_for_fresh_snapshot(self, tmp_path: Path):
        """新快照（ttl_days=30）返回 False。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")
        assert mgr.is_stale("bob") is False

    def test_is_stale_true_when_ttl_days_zero(self, tmp_path: Path):
        """ttl_days=0 时新快照也立即过期。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        assert mgr.is_stale("bob") is True

    def test_is_stale_true_when_captured_at_old(self, tmp_path: Path):
        """captured_at 早于 ttl_days 时返回 True。

        通过手动篡改 meta 中的 captured_at 模拟旧时间。
        """
        mgr = PeerSnapshotManager(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")
        # 手动将 captured_at 改为 60 天前
        meta_path = (
            tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob" / SNAPSHOT_META_FILENAME
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["captured_at"] = "2020-01-01T00:00:00Z"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        assert mgr.is_stale("bob") is True

    def test_is_stale_false_when_captured_at_recent(self, tmp_path: Path):
        """captured_at 在 ttl_days 内返回 False。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")
        # 手动将 captured_at 改为 1 小时前（仍在 ttl 内）
        meta_path = (
            tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob" / SNAPSHOT_META_FILENAME
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["captured_at"] = "2026-08-12T00:00:00Z"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        # 仅当当前时间 > 2026-08-12 + 30 天时才会过期；测试环境假设不会
        # 用一个明确接近当前的日期避免时间漂移导致的 flaky
        # 这里改用 5 天前，更稳健
        from datetime import datetime, timedelta, timezone

        recent = (datetime.now(timezone.utc) - timedelta(days=5)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        meta["captured_at"] = recent
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        assert mgr.is_stale("bob") is False

    def test_is_stale_true_when_captured_at_invalid(self, tmp_path: Path):
        """captured_at 解析失败时保守视为过期。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")
        meta_path = (
            tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob" / SNAPSHOT_META_FILENAME
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["captured_at"] = "not-a-date"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")
        assert mgr.is_stale("bob") is True


class TestPeerSnapshotManagerExpire:
    """expire_stale 清理过期快照。"""

    def test_expire_stale_removes_expired_snapshots(self, tmp_path: Path):
        """expire_stale 删除过期快照目录。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        mgr.refresh_snapshot("alice")
        # ttl_days=0 → 所有快照立即过期
        expired = mgr.expire_stale()
        assert set(expired) == {"bob", "alice"}
        assert not (tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob").exists()
        assert not (tmp_path / PEER_SNAPSHOTS_DIRNAME / "alice").exists()

    def test_expire_stale_returns_expired_peer_ids(self, tmp_path: Path):
        """expire_stale 返回被清理的 peer_id 列表。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=0)
        mgr.refresh_snapshot("bob")
        mgr.refresh_snapshot("carol")
        expired = mgr.expire_stale()
        assert sorted(expired) == ["bob", "carol"]

    def test_expire_stale_keeps_fresh_snapshots(self, tmp_path: Path):
        """expire_stale 不清理未过期快照。"""
        mgr = PeerSnapshotManager(tmp_path, ttl_days=30)
        mgr.refresh_snapshot("bob")  # 新鲜
        # 再造一个过期快照（手动改 captured_at）
        mgr.refresh_snapshot("alice")
        meta_path = (
            tmp_path
            / PEER_SNAPSHOTS_DIRNAME
            / "alice"
            / SNAPSHOT_META_FILENAME
        )
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        meta["captured_at"] = "2020-01-01T00:00:00Z"
        meta_path.write_text(json.dumps(meta), encoding="utf-8")

        expired = mgr.expire_stale()
        assert expired == ["alice"]
        # bob 的快照应仍在
        assert mgr.get_snapshot("bob") is not None
        assert mgr.get_snapshot("alice") is None

    def test_expire_stale_empty_when_no_snapshots(self, tmp_path: Path):
        """无快照时 expire_stale 返回空列表。"""
        mgr = PeerSnapshotManager(tmp_path)
        assert mgr.expire_stale() == []

    def test_expire_stale_skips_non_directory_entries(self, tmp_path: Path):
        """snapshots_root 下非目录文件应被跳过（不报错）。"""
        mgr = PeerSnapshotManager(tmp_path)
        # 在 snapshots_root 下放一个普通文件
        (tmp_path / PEER_SNAPSHOTS_DIRNAME / "stray.txt").write_text(
            "x", encoding="utf-8"
        )
        expired = mgr.expire_stale()
        assert expired == []
        # 文件未被删除
        assert (tmp_path / PEER_SNAPSHOTS_DIRNAME / "stray.txt").is_file()


class TestPeerSnapshotManagerLoadVectorClock:
    """load_vector_clock 加载版本向量。"""

    def test_load_vector_clock_returns_vector_clock(self, tmp_path: Path):
        """有 vector_clock 文件时返回 VectorClock。"""
        mgr = PeerSnapshotManager(tmp_path)
        vc = VectorClock(counters={"alice": 2, "bob": 4})
        mgr.refresh_snapshot("bob", vector_clock=vc)
        loaded = mgr.load_vector_clock("bob")
        assert isinstance(loaded, VectorClock)
        assert loaded.counters == {"alice": 2, "bob": 4}

    def test_load_vector_clock_empty_when_no_file(self, tmp_path: Path):
        """无 vector_clock 文件时返回空 VectorClock。"""
        mgr = PeerSnapshotManager(tmp_path)
        # 即使有快照但未写 vector_clock（refresh 时会写空 vc，这里直接验证 unknown peer）
        loaded = mgr.load_vector_clock("unknown")
        assert isinstance(loaded, VectorClock)
        assert loaded.counters == {}

    def test_load_vector_clock_empty_when_no_snapshot(self, tmp_path: Path):
        """无快照时返回空 VectorClock。"""
        mgr = PeerSnapshotManager(tmp_path)
        result = mgr.load_vector_clock("ghost")
        assert result.counters == {}

    def test_load_vector_clock_empty_when_file_corrupt(self, tmp_path: Path):
        """vector_clock.json 损坏时返回空 VectorClock。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot("bob")
        # 手动写一个非法 JSON
        vc_path = (
            tmp_path / PEER_SNAPSHOTS_DIRNAME / "bob" / VECTOR_CLOCK_FILENAME
        )
        vc_path.write_text("not-json", encoding="utf-8")
        result = mgr.load_vector_clock("bob")
        assert result.counters == {}


class TestPeerSnapshotManagerList:
    """list_snapshots 列出所有快照。"""

    def test_list_snapshots_returns_all(self, tmp_path: Path):
        """list_snapshots 返回所有快照。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot("alice")
        mgr.refresh_snapshot("bob")
        mgr.refresh_snapshot("carol")
        snaps = mgr.list_snapshots()
        assert len(snaps) == 3
        peer_ids = {s.peer_id for s in snaps}
        assert peer_ids == {"alice", "bob", "carol"}
        # 每个都是 PeerSnapshot 实例
        for s in snaps:
            assert isinstance(s, PeerSnapshot)
            assert s.snapshot_version == "v1"

    def test_list_snapshots_empty_when_none(self, tmp_path: Path):
        """无快照时返回空列表。"""
        mgr = PeerSnapshotManager(tmp_path)
        assert mgr.list_snapshots() == []

    def test_list_snapshots_skips_dirs_without_meta(self, tmp_path: Path):
        """只有目录但无 meta 的 peer 不出现在列表中。"""
        mgr = PeerSnapshotManager(tmp_path)
        mgr.refresh_snapshot("bob")
        # 手动创建一个空 peer 目录
        (tmp_path / PEER_SNAPSHOTS_DIRNAME / "ghost").mkdir(parents=True)
        snaps = mgr.list_snapshots()
        peer_ids = {s.peer_id for s in snaps}
        assert peer_ids == {"bob"}


class TestPeerSnapshotManagerInit:
    """__init__ 配置与目录创建。"""

    def test_init_creates_snapshots_root(self, tmp_path: Path):
        """__init__ 创建 peer_snapshots 根目录。"""
        base = tmp_path / "async_comm"
        assert not base.exists()
        PeerSnapshotManager(base)
        assert (base / PEER_SNAPSHOTS_DIRNAME).is_dir()

    def test_init_idempotent_when_root_exists(self, tmp_path: Path):
        """重复初始化不报错（mkdir exist_ok）。"""
        base = tmp_path / "async_comm"
        PeerSnapshotManager(base)
        # 再次初始化不抛异常
        PeerSnapshotManager(base)

    def test_init_with_string_path(self, tmp_path: Path):
        """__init__ 接受字符串路径（自动转 Path）。"""
        mgr = PeerSnapshotManager(str(tmp_path))
        snap = mgr.refresh_snapshot("bob")
        assert snap.peer_id == "bob"
