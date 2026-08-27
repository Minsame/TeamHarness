"""AllInOneRuntime 域内测试（SubTask 3.7）。

覆盖：
- 启动 / 停止 / 上下文管理
- SQLite 元数据库初始化（表可写可读）
- 向量后端降级（sqlite-vec 缺失时走纯 SQLite）
- selfcheck 返回结构
- git provider 缺失时降级（pygit2 不存在时仍可启动）

不依赖 pygit2 / sqlite-vec 实际安装（用 monkeypatch 模拟缺失场景）。
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

from server.deploy.all_in_one import (
    AllInOneRuntime,
    _PureSqliteBackend,
    _SqliteVecBackend,
    run_all_in_one,
    selfcheck,
)
from server.deploy.config import DeployMode


# ---------------------------------------------------------------------------
# 启动 / 停止
# ---------------------------------------------------------------------------


class TestStartStop:
    def test_启动后started为True(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "test.db",
        )
        assert runtime.started is False
        runtime.start()
        try:
            assert runtime.started is True
            assert runtime.sqlite_conn is not None
        finally:
            runtime.stop()
        assert runtime.started is False

    def test_上下文管理自动启停(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "ctx.db",
        )
        with runtime as rt:
            assert rt.started is True
        assert runtime.started is False

    def test_重复启动幂等(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "idem.db",
        )
        runtime.start()
        runtime.start()  # 第二次应无副作用
        assert runtime.started is True
        runtime.stop()

    def test_data_dir自动创建(self, tmp_path: Path) -> None:
        target = tmp_path / "subdir" / "data"
        runtime = AllInOneRuntime(
            data_dir=target,
            sqlite_path=target / "test.db",
        )
        runtime.start()
        try:
            assert target.is_dir()
        finally:
            runtime.stop()


# ---------------------------------------------------------------------------
# SQLite 元数据库
# ---------------------------------------------------------------------------


class TestSqliteMetaDB:
    def test_启动后schema表存在(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "schema.db",
        )
        runtime.start()
        try:
            cur = runtime.sqlite_conn.cursor()
            # 三张核心表应存在
            for table in ("asset_index", "agent_binding", "index_sync_state"):
                cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
                    (table,),
                )
                assert cur.fetchone() is not None, f"表 {table} 未创建"
            # 索引应存在
            cur.execute(
                "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_asset_module'"
            )
            assert cur.fetchone() is not None
        finally:
            runtime.stop()

    def test_可写入读取asset_index(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "rw.db",
        )
        runtime.start()
        try:
            conn = runtime.sqlite_conn
            conn.execute(
                "INSERT INTO asset_index(id, type, owner, scope, git_path) "
                "VALUES (?, ?, ?, ?, ?)",
                ("rule-test-1", "rule", "alice", "team", "rules/test.md"),
            )
            row = conn.execute(
                "SELECT id, type, owner, scope, git_path FROM asset_index WHERE id=?",
                ("rule-test-1",),
            ).fetchone()
            assert row is not None
            assert row[0] == "rule-test-1"
            assert row[1] == "rule"
        finally:
            runtime.stop()

    def test_外键约束已启用(self, tmp_path: Path) -> None:
        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "fk.db",
        )
        runtime.start()
        try:
            cur = runtime.sqlite_conn.execute("PRAGMA foreign_keys")
            assert cur.fetchone()[0] == 1
        finally:
            runtime.stop()


# ---------------------------------------------------------------------------
# 向量后端降级
# ---------------------------------------------------------------------------


class TestVectorBackend:
    def test_纯SQLite兜底可写入(self, tmp_path: Path) -> None:
        """sqlite-vec 缺失时降级为纯 SQLite 向量存储。"""
        backend = _PureSqliteBackend(sqlite3.connect(":memory:"))
        backend.ensure_table("asset_embedding", dim=4)
        backend.upsert("asset_embedding", "asset-1", [0.1, 0.2, 0.3, 0.4])
        # search 返回空（无 KNN 能力），但不报错
        results = backend.search("asset_embedding", [0.1, 0.2, 0.3, 0.4], top_k=5)
        assert results == []

    def test_纯SQLite兜底覆盖写入(self, tmp_path: Path) -> None:
        backend = _PureSqliteBackend(sqlite3.connect(":memory:"))
        backend.ensure_table("asset_embedding", dim=2)
        backend.upsert("asset_embedding", "asset-1", [0.1, 0.2])
        backend.upsert("asset_embedding", "asset-1", [0.3, 0.4])  # 覆盖
        # 仅校验不报错（无 KNN 验证）

    def test_sqlite_vec_backend编码(self) -> None:
        """sqlite-vec 接受 float32 字节串。"""
        import struct

        from server.deploy.all_in_one import _encode_vec

        blob = _encode_vec([1.0, 2.0, 3.0])
        expected = struct.pack("3f", 1.0, 2.0, 3.0)
        assert blob == expected


# ---------------------------------------------------------------------------
# git provider 降级
# ---------------------------------------------------------------------------


class TestGitProviderDegrade:
    def test_pygit2缺失时git_provider为None但不影响启动(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """pygit2 缺失时 git_provider=None，但 SQLite / 向量后端仍可用。"""
        # 模拟 pygit2 缺失：让 import pygit2 抛 ImportError
        monkeypatch.setitem(sys.modules, "pygit2", None)

        runtime = AllInOneRuntime(
            data_dir=tmp_path,
            sqlite_path=tmp_path / "no-pygit2.db",
        )
        runtime.start()
        try:
            assert runtime.sqlite_conn is not None  # SQLite 仍可用
            assert runtime.vector_backend is not None  # 向量后端仍可用
            assert runtime.git_provider is None  # git 不可用
        finally:
            runtime.stop()


# ---------------------------------------------------------------------------
# selfcheck
# ---------------------------------------------------------------------------


class TestSelfcheck:
    def test_返回结构完整(self) -> None:
        result = selfcheck()
        assert "version" in result
        assert "mode" in result
        assert result["mode"] == DeployMode.ALL_IN_ONE.value
        assert "components" in result
        assert "sqlite" in result["components"]
        assert "sqlite_vec" in result["components"]
        assert "libgit2" in result["components"]
        assert "storage_backend" in result

    def test_sqlite_component_available(self) -> None:
        result = selfcheck()
        # sqlite 是 stdlib，必可用
        assert result["components"]["sqlite"]["available"] is True

    def test_storage_backend反映AllInOne默认(self) -> None:
        result = selfcheck()
        backend = result["storage_backend"]
        assert backend["meta_db"] == "sqlite"
        assert backend["git_provider"] == "libgit2"


# ---------------------------------------------------------------------------
# run_all_in_one
# ---------------------------------------------------------------------------


class TestRunAllInOne:
    def test_设置环境变量TEAMHARNESS_ALL_IN_ONE(self, tmp_path: Path) -> None:
        runtime = run_all_in_one(data_path=tmp_path)
        try:
            import os

            assert os.environ.get("TEAMHARNESS_ALL_IN_ONE") == "true"
            assert runtime.started is True
        finally:
            runtime.stop()

    def test_返回可用的runtime(self, tmp_path: Path) -> None:
        runtime = run_all_in_one(data_path=tmp_path)
        try:
            assert isinstance(runtime, AllInOneRuntime)
            assert runtime.sqlite_conn is not None
        finally:
            runtime.stop()
