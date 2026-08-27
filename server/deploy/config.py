"""DeployConfig：部署配置与运行模式探测。

对应技术方案 SubTask 3.1（All-in-One）、3.2（docker-compose），
以及缺陷修复 7.1 单机部署。本模块是 Agent 3 的公共 API 契约提供方，
供 Agent 10 集成测试调用。

公共 API 契约：
    DeployConfig:
        get_mode()           → DeployMode 枚举（all_in_one / docker_compose / single_machine）
        get_storage_backend() → StorageBackend（meta_db + vector_store + git_provider）
        get_version()        → 语义化版本字符串（如 "1.4.0"）
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from enum import Enum
from functools import lru_cache
from pathlib import Path

# 当前 TeamHarness 服务端版本（语义化版本，对应技术方案 9 里程碑）
# 升级流程与迁移脚本以此版本为基准判断是否需要执行迁移
CURRENT_VERSION = "1.0.0"


class DeployMode(str, Enum):
    """部署模式枚举。

    - ALL_IN_ONE：单二进制（内嵌 SQLite + 向量后端 + libgit2），5 人团队无需外部依赖。
    - DOCKER_COMPOSE：PG + Qdrant + 三服务 + Gitea 一键部署。
    - SINGLE_MACHINE：单机裸进程模式（PG 单实例或 SQLite，无容器编排）。
    """

    ALL_IN_ONE = "all-in-one"
    DOCKER_COMPOSE = "docker-compose"
    SINGLE_MACHINE = "single-machine"


class StorageKind(str, Enum):
    """存储后端类型。

    对应技术方案 4 Provider 抽象。每种存储位置都允许替换实现，
    DeployConfig 在启动时根据环境变量决定具体后端。
    """

    # 元数据 DB
    SQLITE = "sqlite"
    POSTGRES = "postgres"
    # 向量库
    SQLITE_VEC = "sqlite-vec"  # All-in-One 模式内嵌向量后端（sqlite-vec 扩展）
    PGVECTOR = "pgvector"      # PG 扩展向量后端
    QDRANT = "qdrant"          # 独立向量服务
    # Git Provider
    LIBGIT2 = "libgit2"        # 内嵌 libgit2（pygit2）
    GITLAB = "gitlab"
    GITEA = "gitea"


@dataclass(frozen=True)
class StorageBackend:
    """存储后端组合。

    All-in-One 模式：sqlite + sqlite-vec + libgit2
    docker-compose 模式：postgres + qdrant + gitea/gitlab
    single-machine 模式：postgres + pgvector + libgit2（或 sqlite + sqlite-vec）
    """

    meta_db: StorageKind
    vector_store: StorageKind
    git_provider: StorageKind

    def as_dict(self) -> dict[str, str]:
        return {
            "meta_db": self.meta_db.value,
            "vector_store": self.vector_store.value,
            "git_provider": self.git_provider.value,
        }


# All-in-One 默认后端：内嵌 SQLite + sqlite-vec + libgit2
ALL_IN_ONE_BACKEND = StorageBackend(
    meta_db=StorageKind.SQLITE,
    vector_store=StorageKind.SQLITE_VEC,
    git_provider=StorageKind.LIBGIT2,
)

# docker-compose 默认后端：PG + Qdrant + Gitea
DOCKER_COMPOSE_BACKEND = StorageBackend(
    meta_db=StorageKind.POSTGRES,
    vector_store=StorageKind.QDRANT,
    git_provider=StorageKind.GITEA,
)

# 单机裸进程默认后端：PG + PGVector + libgit2（缺陷修复 7.1）
SINGLE_MACHINE_BACKEND = StorageBackend(
    meta_db=StorageKind.POSTGRES,
    vector_store=StorageKind.PGVECTOR,
    git_provider=StorageKind.LIBGIT2,
)


# ---------------------------------------------------------------------------
# 版本号语义化解析（对应技术方案 SubTask 3.4）
# ---------------------------------------------------------------------------

_SEMVER_RE = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<prerelease>[0-9A-Za-z.-]+))?(?:\+(?P<build>[0-9A-Za-z.-]+))?$"
)


class DeployConfig:
    """部署配置（公共 API 契约提供方）。

    通过环境变量驱动，无需调用方传参即可探测当前部署形态：
    - TEAMHARNESS_DEPLOY_MODE：显式指定模式（all-in-one / docker-compose / single-machine）
    - TEAMHARNESS_VERSION：覆盖版本号（默认取 CURRENT_VERSION）
    - TEAMHARNESS_IN_DOCKER：docker-compose 容器内自动置为 "true"
    - TEAMHARNESS_ALL_IN_ONE：All-in-One 单二进制启动时置为 "true"
    - TEAMHARNESS_META_DB / TEAMHARNESS_VECTOR_STORE / TEAMHARNESS_GIT_PROVIDER：
      覆盖具体后端（缺省时按模式默认）
    """

    def __init__(
        self,
        *,
        mode: DeployMode | None = None,
        version: str | None = None,
        backend: StorageBackend | None = None,
        env: dict[str, str] | None = None,
    ) -> None:
        # 显式参数优先；否则在调用时按 env 探测
        self._explicit_mode = mode
        self._explicit_version = version
        self._explicit_backend = backend
        # env 用真实 os.environ 副本，便于测试注入
        self._env = env if env is not None else dict(os.environ)

    # ---- 公共 API 契约 ----

    def get_mode(self) -> DeployMode:
        """返回当前部署模式。"""
        if self._explicit_mode is not None:
            return self._explicit_mode
        # 优先级：显式 TEAMHARNESS_DEPLOY_MODE > 容器探测 > All-in-One 探测 > 默认单机
        raw = self._env.get("TEAMHARNESS_DEPLOY_MODE", "").strip().lower()
        if raw:
            return _resolve_mode(raw)
        if self._env.get("TEAMHARNESS_IN_DOCKER", "").strip().lower() in ("1", "true", "yes"):
            return DeployMode.DOCKER_COMPOSE
        if self._env.get("TEAMHARNESS_ALL_IN_ONE", "").strip().lower() in ("1", "true", "yes"):
            return DeployMode.ALL_IN_ONE
        return DeployMode.SINGLE_MACHINE

    def get_storage_backend(self) -> StorageBackend:
        """返回当前存储后端组合。

        允许通过环境变量按需覆盖，未覆盖时使用模式默认组合。
        """
        if self._explicit_backend is not None:
            return self._explicit_backend

        meta_db = self._env.get("TEAMHARNESS_META_DB", "").strip().lower()
        vector_store = self._env.get("TEAMHARNESS_VECTOR_STORE", "").strip().lower()
        git_provider = self._env.get("TEAMHARNESS_GIT_PROVIDER", "").strip().lower()

        default = _default_backend_for(self.get_mode())
        return StorageBackend(
            meta_db=StorageKind(meta_db) if meta_db else default.meta_db,
            vector_store=StorageKind(vector_store) if vector_store else default.vector_store,
            git_provider=StorageKind(git_provider) if git_provider else default.git_provider,
        )

    def get_version(self) -> str:
        """返回当前服务端版本（语义化版本字符串）。"""
        if self._explicit_version is not None:
            return self._explicit_version
        return self._env.get("TEAMHARNESS_VERSION", "").strip() or CURRENT_VERSION

    # ---- 辅助信息（非契约，但供域内其他模块复用） ----

    def is_all_in_one(self) -> bool:
        return self.get_mode() == DeployMode.ALL_IN_ONE

    def is_docker_compose(self) -> bool:
        return self.get_mode() == DeployMode.DOCKER_COMPOSE

    def as_dict(self) -> dict[str, object]:
        """整体配置快照，便于 /v1/system/info 等接口暴露。"""
        return {
            "mode": self.get_mode().value,
            "version": self.get_version(),
            "storage_backend": self.get_storage_backend().as_dict(),
        }


def _resolve_mode(raw: str) -> DeployMode:
    """容错解析模式字符串（兼容 kebab-case / snake_case / 空格）。"""
    normalized = raw.replace("_", "-").replace(" ", "-")
    for m in DeployMode:
        if m.value == normalized:
            return m
    raise ValueError(f"未知部署模式：{raw!r}，可选值：{[m.value for m in DeployMode]}")


def _default_backend_for(mode: DeployMode) -> StorageBackend:
    """按部署模式返回默认存储后端组合。"""
    if mode == DeployMode.ALL_IN_ONE:
        return ALL_IN_ONE_BACKEND
    if mode == DeployMode.DOCKER_COMPOSE:
        return DOCKER_COMPOSE_BACKEND
    return SINGLE_MACHINE_BACKEND


# ---------------------------------------------------------------------------
# 单例访问
# ---------------------------------------------------------------------------

# 全局实例缓存：测试通过 reset_deploy_config() 重置后才会重新探测
@lru_cache(maxsize=1)
def _cached_config() -> DeployConfig:
    return DeployConfig()


def get_deploy_config() -> DeployConfig:
    """获取全局 DeployConfig 单例。

    生产环境从环境变量探测；测试可调用 reset_deploy_config() 清缓存。
    """
    return _cached_config()


def reset_deploy_config() -> None:
    """清空 DeployConfig 单例缓存（测试用）。"""
    _cached_config.cache_clear()


def parse_semver(version: str) -> tuple[int, int, int]:
    """解析语义化版本号为 (major, minor, patch) 元组。

    非法版本抛 ValueError。用于 API 语义化版本与迁移脚本判断版本范围。
    """
    m = _SEMVER_RE.match(version.strip())
    if not m:
        raise ValueError(f"非法语义化版本：{version!r}")
    return int(m.group("major")), int(m.group("minor")), int(m.group("patch"))


def data_dir() -> Path:
    """All-in-One / 单机模式默认数据目录。

    优先取 TEAMHARNESS_DATA_DIR，否则按 OS 选 ~/.teamharness/data。
    """
    env_dir = os.environ.get("TEAMHARNESS_DATA_DIR", "").strip()
    if env_dir:
        return Path(env_dir)
    home = os.environ.get("USERPROFILE") or os.environ.get("HOME") or "."
    return Path(home) / ".teamharness" / "data"
