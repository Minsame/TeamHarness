"""All-in-One 单二进制运行时（SubTask 3.1）。

设计目标：5 人团队下载单二进制后无需任何外部依赖即可启动。
内嵌三件套：
- SQLite：替代 PostgreSQL 元数据库（API 兼容）
- sqlite-vec：替代 PGVector / Qdrant 向量后端（PGVector 风格的 API 抽象）
- libgit2（pygit2）：内嵌 git 操作，无需系统 git

实际打包由 deploy/all_in_one.spec（PyInstaller spec）完成，
本模块提供运行时启动入口 AllInOneRuntime 与 run_all_in_one() 函数。
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from server.deploy.config import (
    CURRENT_VERSION,
    DeployConfig,
    DeployMode,
    StorageBackend,
    StorageKind,
    data_dir,
    get_deploy_config,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内嵌 SQLite 元数据库（替代 PG）
# ---------------------------------------------------------------------------

# asset_index 等核心表的最小 SQLite schema（与 infra_db PG schema 字段对齐）
# 真正的 schema 由 infra_db Agent 2 维护；此处只提供 All-in-One 启动兜底，
# 缺少向量库时仍能写入元数据与装配表。
_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS asset_index (
    id TEXT PRIMARY KEY,
    type TEXT NOT NULL,
    owner TEXT NOT NULL,
    scope TEXT NOT NULL,
    content_hash TEXT,
    embedding_id TEXT,
    version TEXT,
    tags TEXT,                -- JSON 数组
    git_path TEXT NOT NULL,
    git_commit TEXT,
    module_path TEXT DEFAULT '',
    category TEXT,
    related_to TEXT,          -- JSON 数组
    schema_version INTEGER DEFAULT 1,
    created_at TEXT,
    updated_at TEXT,
    indexed_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_asset_module ON asset_index(module_path);
CREATE INDEX IF NOT EXISTS idx_asset_category ON asset_index(category);

CREATE TABLE IF NOT EXISTS agent_binding (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    agent_role TEXT,
    asset_id TEXT NOT NULL,
    binding_type TEXT,        -- fixed / on-demand
    priority TEXT,
    enabled INTEGER DEFAULT 1,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS index_sync_state (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    last_synced_commit TEXT,
    last_sync_at TEXT,
    status TEXT
);
"""


@dataclass
class AllInOneRuntime:
    """All-in-One 单二进制运行时实例。

    封装内嵌三件套的初始化与生命周期：
    - sqlite_conn：元数据库连接（sqlite3）
    - vector_backend：向量后端实例（sqlite-vec 或纯 SQLite 兜底）
    - git_provider：libgit2 provider（pygit2）
    """

    data_dir: Path
    sqlite_path: Path
    sqlite_conn: Any = None  # sqlite3.Connection
    vector_backend: Any = None  # VectorBackend 实例
    git_provider: Any = None  # Libgit2Provider 实例
    config: DeployConfig = field(default_factory=DeployConfig)

    @property
    def started(self) -> bool:
        return self.sqlite_conn is not None

    def start(self) -> None:
        """启动 All-in-One：初始化 SQLite + 向量后端 + libgit2。"""
        if self.started:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._init_sqlite()
        self._init_vector_backend()
        self._init_git_provider()
        logger.info(
            "All-in-One 启动完成: data_dir=%s version=%s",
            self.data_dir,
            self.config.get_version(),
        )

    def stop(self) -> None:
        """关闭运行时，释放资源。"""
        if self.sqlite_conn is not None:
            try:
                self.sqlite_conn.commit()
                self.sqlite_conn.close()
            finally:
                self.sqlite_conn = None
        if self.git_provider is not None:
            # Libgit2Provider 无显式 close，置空交由 GC 回收
            self.git_provider = None
        self.vector_backend = None

    # ---- SQLite 元数据库 ----

    def _init_sqlite(self) -> None:
        import sqlite3

        self.sqlite_conn = sqlite3.connect(
            str(self.sqlite_path),
            check_same_thread=False,
            isolation_level=None,  # 自动提交，匹配 PG 风格
        )
        self.sqlite_conn.row_factory = sqlite3.Row
        self.sqlite_conn.executescript(_SQLITE_SCHEMA)
        # 启用外键约束（SQLite 默认关闭）
        self.sqlite_conn.execute("PRAGMA foreign_keys = ON")
        logger.info("SQLite 元数据库就绪: %s", self.sqlite_path)

    # ---- 向量后端（sqlite-vec 优先，纯 SQLite 兜底） ----

    def _init_vector_backend(self) -> None:
        """初始化内嵌向量后端。

        sqlite-vec 是 SQLite 扩展，提供 KNN 向量检索能力，替代 PGVector / Qdrant。
        若运行环境未安装 sqlite-vec，降级为纯 SQLite（仅存 embedding 二进制，
        不提供 KNN 检索，召回退化为 BM25 关键词检索）。
        """
        try:
            import sqlite_vec  # type: ignore[import-not-found]

            conn = self.sqlite_conn
            try:
                # sqlite-vec 0.1.x: db = sqlite_vec.load(conn)
                db = sqlite_vec.load(conn)
                # 注入 vector0 / vec0 虚拟表模块
                conn.enable_load_extension(True)
            except AttributeError:
                # 较新版本通过 loadable_extension 路径加载
                db = None
            self.vector_backend = _SqliteVecBackend(conn, db)
            # 创建向量虚拟表（dim 默认 1536，与 OpenAI text-embedding-3-small 对齐）
            self.vector_backend.ensure_table("asset_embedding", dim=1536)
            logger.info("向量后端就绪: sqlite-vec（KNN 检索可用）")
        except ImportError:
            self.vector_backend = _PureSqliteBackend(self.sqlite_conn)
            self.vector_backend.ensure_table("asset_embedding", dim=1536)
            logger.warning(
                "sqlite-vec 未安装，降级为纯 SQLite 向量存储（无 KNN，召回走 BM25）"
            )

    # ---- Git Provider（libgit2/pygit2） ----

    def _init_git_provider(self) -> None:
        """初始化内嵌 libgit2 git provider。

        pygit2 缺失时延迟到首次 git 操作才报错，
        允许 All-in-One 启动（仅元数据/向量能力可用，git 不可用）。
        """
        try:
            from server.infra_git.git_provider import Libgit2Provider

            repo_path = self.data_dir / "repo"
            repo_path.mkdir(parents=True, exist_ok=True)
            # 若仓库目录为空，初始化一个 bare repo 作为占位（pygit2 直接 init）
            try:
                import pygit2  # type: ignore[import-not-found]

                if not (repo_path / ".git").exists() and not any(repo_path.iterdir()):
                    pygit2.init_repository(str(repo_path), initial_head="main")
            except ImportError:
                logger.warning("pygit2 未安装，libgit2 git 操作不可用")
            self.git_provider = Libgit2Provider(repo_path)
            logger.info("Git Provider 就绪: libgit2 (%s)", repo_path)
        except Exception as exc:  # pragma: no cover - 兜底日志
            logger.warning("libgit2 初始化失败，git 操作不可用：%s", exc)
            self.git_provider = None

    # ---- 上下文管理 ----

    def __enter__(self) -> "AllInOneRuntime":
        self.start()
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.stop()


# ---------------------------------------------------------------------------
# 向量后端抽象
# ---------------------------------------------------------------------------


class _VectorBackend:
    """向量后端最小抽象。"""

    def ensure_table(self, table: str, dim: int) -> None:  # pragma: no cover - 抽象
        raise NotImplementedError

    def upsert(self, table: str, asset_id: str, embedding: list[float]) -> None:  # pragma: no cover
        raise NotImplementedError

    def search(self, table: str, query: list[float], top_k: int = 10) -> list[tuple[str, float]]:  # pragma: no cover
        raise NotImplementedError


class _SqliteVecBackend(_VectorBackend):
    """sqlite-vec 向量后端。"""

    def __init__(self, conn: Any, db: Any) -> None:
        self.conn = conn
        self.db = db  # sqlite_vec 加载句柄（部分版本不用）

    def ensure_table(self, table: str, dim: int) -> None:
        # vec0 虚拟表：主键 + 向量字段
        self.conn.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS {table} USING vec0("
            f"asset_id TEXT PRIMARY KEY, embedding float[{dim}])"
        )

    def upsert(self, table: str, asset_id: str, embedding: list[float]) -> None:
        # vec0 不支持 INSERT OR REPLACE，先 delete 再 insert
        self.conn.execute(f"DELETE FROM {table} WHERE asset_id = ?", (asset_id,))
        self.conn.execute(
            f"INSERT INTO {table}(asset_id, embedding) VALUES (?, ?)",
            (asset_id, _encode_vec(embedding)),
        )

    def search(self, table: str, query: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        rows = self.conn.execute(
            f"SELECT asset_id, distance FROM {table} "
            f"WHERE embedding MATCH ? ORDER BY distance LIMIT ?",
            (_encode_vec(query), top_k),
        ).fetchall()
        # sqlite-vec 的 distance 越小越相似，转 similarity 越大越相似
        return [(r[0], 1.0 - float(r[1])) for r in rows]


class _PureSqliteBackend(_VectorBackend):
    """纯 SQLite 向量兜底（无 KNN，search 返回空，召回降级到 BM25）。"""

    def __init__(self, conn: Any) -> None:
        self.conn = conn

    def ensure_table(self, table: str, dim: int) -> None:
        self.conn.execute(
            f"CREATE TABLE IF NOT EXISTS {table} ("
            f"asset_id TEXT PRIMARY KEY, embedding BLOB, dim INTEGER)"
        )

    def upsert(self, table: str, asset_id: str, embedding: list[float]) -> None:
        import struct

        blob = struct.pack(f"{len(embedding)}f", *embedding)
        self.conn.execute(
            f"INSERT OR REPLACE INTO {table}(asset_id, embedding, dim) VALUES (?, ?, ?)",
            (asset_id, blob, len(embedding)),
        )

    def search(self, table: str, query: list[float], top_k: int = 10) -> list[tuple[str, float]]:
        # 无 KNN 能力，返回空，调用方应回退 BM25
        return []


def _encode_vec(vec: list[float]) -> bytes:
    """sqlite-vec 接受 float32 little-endian 字节串。"""
    import struct

    return struct.pack(f"{len(vec)}f", *vec)


# ---------------------------------------------------------------------------
# 启动入口
# ---------------------------------------------------------------------------


def run_all_in_one(
    *,
    data_path: str | Path | None = None,
    host: str = "127.0.0.1",
    port: int = 8080,
) -> AllInOneRuntime:
    """All-in-One 单二进制启动入口。

    被 PyInstaller 打包后的入口脚本调用，启动内嵌运行时与 FastAPI 应用。
    本函数不阻塞：调用方按需启动 uvicorn。
    """
    # 标记 All-in-One 模式（DeployConfig 探测依据）
    os.environ["TEAMHARNESS_ALL_IN_ONE"] = "true"

    base = Path(data_path) if data_path else data_dir()
    runtime = AllInOneRuntime(
        data_dir=base,
        sqlite_path=base / "teamharness.db",
    )
    runtime.start()
    # FastAPI app 与 uvicorn 启动由 server.app 等其他模块负责；
    # 本模块仅保证运行时就绪，避免与 Agent 4-9 的路由实现耦合。
    logger.info(
        "All-in-One 服务准备就绪: http://%s:%d (建议由 uvicorn 启动 FastAPI app)",
        host,
        port,
    )
    return runtime


def selfcheck() -> dict[str, Any]:
    """系统自检：返回部署模式 + 内嵌组件可用性 + 存储后端组合。

    供 /v1/system/selfcheck 端点暴露，也可被部署验证脚本调用。
    mode / storage_backend 读取真实 DeployConfig（按环境变量探测），
    components 仍检查 All-in-One 内嵌组件（sqlite / sqlite-vec / libgit2）的可用性，
    供运维确认「降级到 All-in-One 时是否能用」。
    """
    cfg = get_deploy_config()
    result: dict[str, Any] = {
        "version": cfg.get_version(),
        "mode": cfg.get_mode().value,
        "components": {},
    }
    # SQLite
    try:
        import sqlite3

        conn = sqlite3.connect(":memory:")
        conn.execute("SELECT 1")
        conn.close()
        result["components"]["sqlite"] = {"available": True}
    except Exception as exc:  # pragma: no cover
        result["components"]["sqlite"] = {"available": False, "error": str(exc)}
    # sqlite-vec
    try:
        import sqlite_vec  # noqa: F401  type: ignore[import-not-found]

        result["components"]["sqlite_vec"] = {"available": True}
    except ImportError:
        result["components"]["sqlite_vec"] = {
            "available": False,
            "note": "降级为纯 SQLite 向量存储（无 KNN）",
        }
    # libgit2 (pygit2)
    try:
        import pygit2  # type: ignore[import-not-found]

        result["components"]["libgit2"] = {
            "available": True,
            "version": pygit2.LIBGIT2_VERSION,
        }
    except ImportError:
        result["components"]["libgit2"] = {"available": False, "note": "pygit2 未安装"}
    # 后端组合（按真实部署模式 + 环境变量覆盖）
    backend: StorageBackend = cfg.get_storage_backend()
    result["storage_backend"] = backend.as_dict()
    return result


if __name__ == "__main__":  # pragma: no cover - CLI 入口
    # PyInstaller 打包后的可执行入口；启动后阻塞主线程
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    runtime = run_all_in_one()
    print("All-in-One 已启动，按 Ctrl+C 退出", file=sys.stderr)
    try:
        import time

        while True:
            time.sleep(60)
    except KeyboardInterrupt:
        runtime.stop()
