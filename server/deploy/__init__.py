"""deploy 领域包：部署与升级（Agent 3）。

职责对应技术方案 agent3-deploy.md：
- All-in-One 单二进制（内嵌 SQLite + PGVector 替代 + libgit2）
- docker-compose 一键部署（PG + Qdrant + 三服务 + Gitea）
- 单机模式每日 cron 备份（SQLite + git repo → tar.gz）
- API 语义化版本（/v1/ 锁定，/v2/ 破坏性）
- frontmatter schema_version 兼容解析
- 升级流程文档与迁移脚本框架

本包为公共 API 提供方，导出 DeployConfig 供 Agent 10 集成测试调用。
"""

from server.deploy.config import (
    CURRENT_VERSION,
    DeployConfig,
    DeployMode,
    StorageBackend,
    StorageKind,
    get_deploy_config,
)
from server.deploy.all_in_one import AllInOneRuntime, run_all_in_one
from server.deploy.api_versioning import (
    APIVersionPolicy,
    VersionedAPIRouter,
    parse_semver,
)
from server.deploy.schema_version import (
    SCHEMA_VERSION_CURRENT,
    SchemaMigrator,
    parse_asset_frontmatter,
)
from server.deploy.backup import BackupResult, run_backup
from server.deploy.migrations import Migration, MigrationRegistry, migrate

__all__ = [
    "CURRENT_VERSION",
    "DeployConfig",
    "DeployMode",
    "StorageBackend",
    "StorageKind",
    "get_deploy_config",
    "AllInOneRuntime",
    "run_all_in_one",
    "APIVersionPolicy",
    "VersionedAPIRouter",
    "parse_semver",
    "SCHEMA_VERSION_CURRENT",
    "SchemaMigrator",
    "parse_asset_frontmatter",
    "BackupResult",
    "run_backup",
    "Migration",
    "MigrationRegistry",
    "migrate",
]
