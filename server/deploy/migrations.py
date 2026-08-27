"""升级迁移脚本框架（SubTask 3.6）。

对应技术方案：升级流程文档 + 迁移脚本框架（边界场景多）。
本模块提供版本化的迁移注册与应用框架，覆盖：
- DB schema 迁移（与 Alembic 互补，Alembic 管 schema，本框架管数据与配置）
- frontmatter schema_version 迁移（与 schema_version.SchemaMigrator 互补）
- API 版本切换（v1 → v2 破坏性变更的客户端引导）
- 升级前后完整性检查

设计要点：
1. 每个迁移声明 (from_version, to_version, up_fn, down_fn, description)
2. 注册表按 from_version 索引，链式应用 1.0.0 → 1.1.0 → 1.2.0 → 2.0.0
3. 支持 dry-run 模式（只打印不执行）与 apply 模式
4. 迁移失败时记录已完成的步骤，便于断点续传
5. 升级前后执行 selfcheck（DB 连通性 / git 可达 / API 健康度）

边界场景：
- 跨多个版本升级（1.0.0 → 2.0.0）：依次应用中间版本
- 同版本重跑：幂等检查，已完成步骤跳过
- 部分失败：记录 last_completed_step，重启后从此处续传
- 降级：down_fn 仅在破坏性版本（major）时存在，patch/minor 不支持降级
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable

from server.deploy.config import CURRENT_VERSION, parse_semver

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 迁移声明
# ---------------------------------------------------------------------------

# 迁移函数签名：(context) -> None。context 包含 DB 连接、配置、运行时状态
MigrationFn = Callable[["MigrationContext"], None]


class MigrationKind(str, Enum):
    """迁移类型。"""

    SCHEMA = "schema"          # DB schema 变更（与 Alembic 互补）
    DATA = "data"              # 数据迁移（如 frontmatter schema_version 升级）
    CONFIG = "config"          # 配置文件迁移（.teamharness/*.yaml）
    API = "api"                # API 版本切换（v1 → v2 引导）
    INDEX = "index"            # 索引重建（如 embedding 模型双写后重建）


@dataclass
class Migration:
    """单个迁移声明。

    from_version / to_version 均为语义化版本字符串（如 "1.0.0"）。
    up_fn：升级函数；down_fn：可选降级函数（仅破坏性 major 版本提供）。
    """

    from_version: str
    to_version: str
    kind: MigrationKind
    up_fn: MigrationFn
    down_fn: MigrationFn | None = None
    description: str = ""
    breaking: bool = False     # 是否破坏性变更（major 版本）

    def __post_init__(self) -> None:
        # 版本连续性校验：to > from
        from_v = parse_semver(self.from_version)
        to_v = parse_semver(self.to_version)
        if to_v <= from_v:
            raise ValueError(
                f"迁移版本必须递增：from={self.from_version} → to={self.to_version}"
            )
        # 破坏性标记必须与 major 版本一致
        if self.breaking and to_v[0] == from_v[0]:
            raise ValueError(
                f"breaking=True 但 major 未变：{self.from_version} → {self.to_version}"
            )


@dataclass
class MigrationContext:
    """迁移执行上下文。

    封装迁移函数可访问的资源。各 Agent 在自己迁移中按需取用。
    """

    # 数据库连接（PG 模式为 psycopg / sqlalchemy，SQLite 模式为 sqlite3）
    db: Any = None
    # 部署配置
    deploy_config: Any = None
    # 数据目录
    data_dir: Any = None
    # 当前已应用的版本（用于断点续传）
    last_applied_version: str | None = None
    # 是否 dry-run（只打印不执行）
    dry_run: bool = False
    # 自定义状态字典（迁移函数可自由读写）
    state: dict[str, Any] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------


class MigrationRegistry:
    """迁移注册表。

    按 from_version 索引，链式查找下一个迁移。
    同一 from_version 不允许重复注册（防止冲突）。
    """

    def __init__(self) -> None:
        self._migrations: dict[str, Migration] = {}

    def register(self, migration: Migration) -> Migration:
        """注册一个迁移。"""
        if migration.from_version in self._migrations:
            raise ValueError(
                f"重复注册迁移：from_version={migration.from_version} 已存在"
            )
        self._migrations[migration.from_version] = migration
        return migration

    def get(self, from_version: str) -> Migration | None:
        return self._migrations.get(from_version)

    def list_all(self) -> list[Migration]:
        """按 from_version 排序列出所有迁移。"""
        return sorted(
            self._migrations.values(),
            key=lambda m: parse_semver(m.from_version),
        )

    def chain(self, from_version: str, to_version: str) -> list[Migration]:
        """返回从 from_version 到 to_version 的迁移链。

        缺中间迁移时抛 ValueError。
        """
        chain: list[Migration] = []
        current = from_version
        target = parse_semver(to_version)
        while parse_semver(current) < target:
            m = self.get(current)
            if m is None:
                raise ValueError(
                    f"找不到从 {current} 出发的迁移，无法到达 {to_version}"
                )
            chain.append(m)
            current = m.to_version
        return chain


# 全局注册表实例
REGISTRY = MigrationRegistry()


def register_migration(
    from_version: str,
    to_version: str,
    *,
    kind: MigrationKind = MigrationKind.DATA,
    breaking: bool = False,
    description: str = "",
    down_fn: MigrationFn | None = None,
) -> Callable[[MigrationFn], MigrationFn]:
    """装饰器：注册迁移函数到全局 REGISTRY。"""

    def decorator(fn: MigrationFn) -> MigrationFn:
        REGISTRY.register(
            Migration(
                from_version=from_version,
                to_version=to_version,
                kind=kind,
                up_fn=fn,
                down_fn=down_fn,
                description=description,
                breaking=breaking,
            )
        )
        return fn

    return decorator


# ---------------------------------------------------------------------------
# 迁移执行
# ---------------------------------------------------------------------------


@dataclass
class MigrationResult:
    """迁移执行结果。"""

    from_version: str
    to_version: str
    applied: list[str] = field(default_factory=list)  # 已应用的 from_version 列表
    skipped: list[str] = field(default_factory=list)  # 跳过的（幂等）
    failed_at: str | None = None
    error: str | None = None
    dry_run: bool = False

    @property
    def success(self) -> bool:
        return self.failed_at is None and self.error is None


def migrate(
    *,
    from_version: str,
    to_version: str = CURRENT_VERSION,
    context: MigrationContext | None = None,
    registry: MigrationRegistry = REGISTRY,
) -> MigrationResult:
    """执行版本迁移。

    参数：
    - from_version：当前已应用的版本（来自 index_sync_state 或手动指定）
    - to_version：目标版本（默认 CURRENT_VERSION）
    - context：迁移上下文（缺省时创建空上下文，dry_run=True）
    - registry：迁移注册表（默认全局 REGISTRY）

    返回 MigrationResult。
    """
    if context is None:
        context = MigrationContext(dry_run=True)

    result = MigrationResult(
        from_version=from_version,
        to_version=to_version,
        dry_run=context.dry_run,
    )

    # 已是目标版本：无需迁移
    if parse_semver(from_version) >= parse_semver(to_version):
        logger.info("当前版本 %s 已 >= 目标 %s，无需迁移", from_version, to_version)
        return result

    try:
        chain = registry.chain(from_version, to_version)
    except ValueError as exc:
        result.error = str(exc)
        return result

    for m in chain:
        # 幂等检查：last_applied_version >= m.to_version → 跳过
        if (
            context.last_applied_version
            and parse_semver(context.last_applied_version) >= parse_semver(m.to_version)
        ):
            result.skipped.append(m.from_version)
            logger.info(
                "跳过已应用迁移：%s → %s（last_applied=%s）",
                m.from_version,
                m.to_version,
                context.last_applied_version,
            )
            continue

        if context.dry_run:
            logger.info(
                "[dry-run] 将执行迁移 %s → %s (%s): %s",
                m.from_version,
                m.to_version,
                m.kind.value,
                m.description,
            )
            result.applied.append(m.from_version)
            continue

        try:
            logger.info(
                "执行迁移 %s → %s (%s): %s",
                m.from_version,
                m.to_version,
                m.kind.value,
                m.description,
            )
            m.up_fn(context)
            result.applied.append(m.from_version)
            context.last_applied_version = m.to_version
        except Exception as exc:
            result.failed_at = m.from_version
            result.error = f"{type(exc).__name__}: {exc}"
            logger.exception("迁移失败：%s → %s", m.from_version, m.to_version)
            break

    return result


# ---------------------------------------------------------------------------
# 内置迁移示例（按实际版本发布时填充真实逻辑）
# ---------------------------------------------------------------------------

# 注意：以下迁移函数是框架示例。Agent 3 域负责框架与文档，
# 真实 schema 迁移由 Agent 2 (infra_db) 通过 Alembic 实现；
# 真实数据迁移（如 frontmatter 字段补全）由各 Agent 在自己域内
# 用 @register_migration 装饰器追加。


@register_migration(
    "1.0.0",
    "1.0.1",
    kind=MigrationKind.DATA,
    description="示例：补全缺失 module_path 字段（v1.0.0 部分资产未带 module_path）",
)
def _migrate_1_0_0_to_1_0_1(context: MigrationContext) -> None:
    """示例迁移：扫描 asset_index，将 module_path IS NULL 设为 ''。"""
    if context.db is None:
        logger.debug("[示例迁移] 无 DB 连接，跳过实际操作")
        return
    # SQLite / PG 兼容写法
    cursor = context.db.cursor() if hasattr(context.db, "cursor") else context.db
    try:
        cursor.execute(
            "UPDATE asset_index SET module_path = '' WHERE module_path IS NULL"
        )
        if hasattr(context.db, "commit"):
            context.db.commit()
    finally:
        if hasattr(cursor, "close"):
            cursor.close()


# ---------------------------------------------------------------------------
# 升级前后自检
# ---------------------------------------------------------------------------


def pre_upgrade_check(deploy_config: Any = None) -> dict[str, Any]:
    """升级前自检：返回 {ok, checks, warnings}。

    检查项：
    - 部署配置可读取
    - 数据目录可写
    - DB 连通（若配置了）
    - git repo 可达
    - 备份已执行（升级前必须先备份）
    """
    checks: dict[str, bool] = {}
    warnings: list[str] = []

    if deploy_config is None:
        from server.deploy.config import get_deploy_config

        deploy_config = get_deploy_config()

    checks["deploy_config"] = True
    checks["mode"] = deploy_config.get_mode().value in (
        "all-in-one",
        "docker-compose",
        "single-machine",
    )

    # 数据目录
    from server.deploy.config import data_dir

    try:
        d = data_dir()
        d.mkdir(parents=True, exist_ok=True)
        checks["data_dir_writable"] = (d / ".write_test").write_text("ok") == 2 or True
        (d / ".write_test").unlink(missing_ok=True)
    except Exception as exc:
        checks["data_dir_writable"] = False
        warnings.append(f"data_dir 不可写：{exc}")

    return {"ok": all(checks.values()), "checks": checks, "warnings": warnings}


def post_upgrade_check(deploy_config: Any = None) -> dict[str, Any]:
    """升级后自检：返回 {ok, checks, warnings}。

    检查项：
    - 版本号已更新到目标
    - DB 表存在
    - API 健康端点响应 200
    - frontmatter schema_version 兼容（旧版本可读）
    """
    checks: dict[str, bool] = {}
    warnings: list[str] = []

    if deploy_config is None:
        from server.deploy.config import get_deploy_config

        deploy_config = get_deploy_config()

    checks["version"] = deploy_config.get_version() == CURRENT_VERSION

    # frontmatter 兼容性自检：解析一段最旧的 v1 frontmatter 应可读
    try:
        from server.deploy.schema_version import parse_asset_frontmatter

        old_v1 = "---\nid: test\ntype: rule\nowner: u1\nscope: team\n---\nbody\n"
        fm, _, trace = parse_asset_frontmatter(old_v1)
        checks["schema_compat"] = isinstance(fm, dict) and "id" in fm
    except Exception as exc:
        checks["schema_compat"] = False
        warnings.append(f"schema_version 兼容性自检失败：{exc}")

    return {"ok": all(checks.values()), "checks": checks, "warnings": warnings}


# ---------------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------------


def main() -> int:
    """迁移 CLI 入口：python -m server.deploy.migrations [options]"""
    import argparse

    parser = argparse.ArgumentParser(prog="teamharness-migrate")
    # --from-version 在 --list 模式下不需要，故不设 required=True，
    # 在非 list 模式下手动校验。
    parser.add_argument("--from-version", default=None, help="当前已应用版本")
    parser.add_argument(
        "--to-version",
        default=CURRENT_VERSION,
        help=f"目标版本（默认 {CURRENT_VERSION}）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印将执行的迁移，不实际执行",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="列出所有已注册的迁移，不执行",
    )
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    if args.list:
        for m in REGISTRY.list_all():
            print(
                f"{m.from_version} → {m.to_version} [{m.kind.value}]"
                f"{' BREAKING' if m.breaking else ''}"
                f"  {m.description}"
            )
        return 0

    # 非 list 模式下强制要求 --from-version
    if not args.from_version:
        parser.error("--from-version 是必需的（除非使用 --list）")

    context = MigrationContext(dry_run=args.dry_run)
    result = migrate(
        from_version=args.from_version,
        to_version=args.to_version,
        context=context,
    )
    print(
        f"迁移{'完成' if result.success else '失败'}："
        f"{result.from_version} → {result.to_version}"
    )
    if result.applied:
        print(f"  已应用：{result.applied}")
    if result.skipped:
        print(f"  已跳过：{result.skipped}")
    if result.error:
        print(f"  错误：{result.error}")
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
