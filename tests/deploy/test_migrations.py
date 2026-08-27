"""迁移脚本框架测试（SubTask 3.7）。

覆盖：
- Migration / MigrationKind / MigrationContext 数据结构
- MigrationRegistry 注册 / 查询 / 链式查找
- migrate 主流程（dry-run / apply / 幂等 / 跨版本 / 失败断点）
- register_migration 装饰器
- pre_upgrade_check / post_upgrade_check
- CLI main() 入口
- 边界：版本不连续 / 重复注册 / 非法 breaking 标记
"""

from __future__ import annotations

import pytest

from server.deploy.migrations import (
    REGISTRY,
    Migration,
    MigrationContext,
    MigrationKind,
    MigrationRegistry,
    MigrationResult,
    migrate,
    post_upgrade_check,
    pre_upgrade_check,
    register_migration,
)


# ---------------------------------------------------------------------------
# Migration dataclass
# ---------------------------------------------------------------------------


class TestMigrationDataclass:
    def test_正常构造(self) -> None:
        m = Migration(
            from_version="1.0.0",
            to_version="1.1.0",
            kind=MigrationKind.DATA,
            up_fn=lambda ctx: None,
            description="测试迁移",
        )
        assert m.from_version == "1.0.0"
        assert m.to_version == "1.1.0"
        assert m.kind == MigrationKind.DATA
        assert m.breaking is False

    def test_版本倒退抛ValueError(self) -> None:
        with pytest.raises(ValueError, match="版本必须递增"):
            Migration(
                from_version="1.1.0",
                to_version="1.0.0",
                kind=MigrationKind.DATA,
                up_fn=lambda ctx: None,
            )

    def test_breaking但major未变抛ValueError(self) -> None:
        with pytest.raises(ValueError, match="breaking=True 但 major 未变"):
            Migration(
                from_version="1.0.0",
                to_version="1.1.0",
                kind=MigrationKind.DATA,
                up_fn=lambda ctx: None,
                breaking=True,
            )

    def test_breaking_major版本合法(self) -> None:
        m = Migration(
            from_version="1.5.0",
            to_version="2.0.0",
            kind=MigrationKind.API,
            up_fn=lambda ctx: None,
            breaking=True,
        )
        assert m.breaking is True


# ---------------------------------------------------------------------------
# MigrationRegistry
# ---------------------------------------------------------------------------


class TestMigrationRegistry:
    def test_注册与查询(self) -> None:
        reg = MigrationRegistry()
        m = Migration(
            from_version="1.0.0",
            to_version="1.1.0",
            kind=MigrationKind.SCHEMA,
            up_fn=lambda ctx: None,
        )
        reg.register(m)
        assert reg.get("1.0.0") is m

    def test_重复注册抛ValueError(self) -> None:
        reg = MigrationRegistry()
        reg.register(
            Migration(
                from_version="1.0.0",
                to_version="1.1.0",
                kind=MigrationKind.SCHEMA,
                up_fn=lambda ctx: None,
            )
        )
        with pytest.raises(ValueError, match="重复注册"):
            reg.register(
                Migration(
                    from_version="1.0.0",
                    to_version="1.1.0",
                    kind=MigrationKind.DATA,
                    up_fn=lambda ctx: None,
                )
            )

    def test_list_all按版本排序(self) -> None:
        reg = MigrationRegistry()
        # 故意乱序注册
        reg.register(
            Migration("1.1.0", "1.2.0", MigrationKind.DATA, up_fn=lambda ctx: None)
        )
        reg.register(
            Migration("1.0.0", "1.1.0", MigrationKind.DATA, up_fn=lambda ctx: None)
        )
        reg.register(
            Migration("2.0.0", "2.1.0", MigrationKind.DATA, up_fn=lambda ctx: None)
        )
        all_migrations = reg.list_all()
        assert [m.from_version for m in all_migrations] == [
            "1.0.0",
            "1.1.0",
            "2.0.0",
        ]

    def test_chain正常串联(self) -> None:
        reg = MigrationRegistry()
        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, lambda ctx: None))
        reg.register(Migration("1.1.0", "1.2.0", MigrationKind.DATA, lambda ctx: None))
        reg.register(Migration("1.2.0", "2.0.0", MigrationKind.API, lambda ctx: None, breaking=True))
        chain = reg.chain("1.0.0", "2.0.0")
        assert [m.to_version for m in chain] == ["1.1.0", "1.2.0", "2.0.0"]

    def test_chain缺中间迁移抛ValueError(self) -> None:
        reg = MigrationRegistry()
        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, lambda ctx: None))
        # 缺 1.1.0 → 1.2.0
        reg.register(Migration("1.2.0", "1.3.0", MigrationKind.DATA, lambda ctx: None))
        with pytest.raises(ValueError, match="找不到从 1.1.0 出发的迁移"):
            reg.chain("1.0.0", "1.3.0")

    def test_chain目标等于源返回空(self) -> None:
        reg = MigrationRegistry()
        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, lambda ctx: None))
        assert reg.chain("1.0.0", "1.0.0") == []


# ---------------------------------------------------------------------------
# migrate 主流程
# ---------------------------------------------------------------------------


class TestMigrate:
    def test_已是目标版本无迁移(self) -> None:
        reg = MigrationRegistry()
        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, lambda ctx: None))
        result = migrate(
            from_version="1.1.0",
            to_version="1.1.0",
            registry=reg,
        )
        assert result.success is True
        assert result.applied == []

    def test_目标版本低于源无迁移(self) -> None:
        reg = MigrationRegistry()
        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, lambda ctx: None))
        result = migrate(
            from_version="1.1.0",
            to_version="1.0.0",
            registry=reg,
        )
        assert result.success is True
        assert result.applied == []

    def test_dry_run只打印不执行(self) -> None:
        calls: list[str] = []
        reg = MigrationRegistry()

        def up(ctx: MigrationContext) -> None:
            calls.append(ctx.state.get("name", "unknown"))

        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, up))
        ctx = MigrationContext(dry_run=True, state={"name": "test1"})
        result = migrate(
            from_version="1.0.0",
            to_version="1.1.0",
            context=ctx,
            registry=reg,
        )
        assert result.success is True
        assert result.applied == ["1.0.0"]
        assert calls == []  # dry-run 不执行 up_fn

    def test_apply模式执行up_fn(self) -> None:
        calls: list[str] = []
        reg = MigrationRegistry()

        def up(ctx: MigrationContext) -> None:
            calls.append(f"{ctx.state.get('name')} applied")

        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, up))
        ctx = MigrationContext(dry_run=False, state={"name": "test2"})
        result = migrate(
            from_version="1.0.0",
            to_version="1.1.0",
            context=ctx,
            registry=reg,
        )
        assert result.success is True
        assert calls == ["test2 applied"]

    def test_链式应用多步迁移(self) -> None:
        calls: list[str] = []
        reg = MigrationRegistry()

        def make_up(name: str):
            def up(ctx: MigrationContext) -> None:
                calls.append(name)

            return up

        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, make_up("step1")))
        reg.register(Migration("1.1.0", "1.2.0", MigrationKind.DATA, make_up("step2")))
        reg.register(Migration("1.2.0", "1.3.0", MigrationKind.DATA, make_up("step3")))

        result = migrate(
            from_version="1.0.0",
            to_version="1.3.0",
            context=MigrationContext(),
            registry=reg,
        )
        assert result.success is True
        assert calls == ["step1", "step2", "step3"]
        assert result.applied == ["1.0.0", "1.1.0", "1.2.0"]

    def test_断点续传跳过已应用(self) -> None:
        calls: list[str] = []
        reg = MigrationRegistry()

        def make_up(name: str):
            def up(ctx: MigrationContext) -> None:
                calls.append(name)

            return up

        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, make_up("step1")))
        reg.register(Migration("1.1.0", "1.2.0", MigrationKind.DATA, make_up("step2")))
        # last_applied_version=1.1.0 → 跳过 step1，从 step2 开始
        ctx = MigrationContext(last_applied_version="1.1.0")
        result = migrate(
            from_version="1.0.0",
            to_version="1.2.0",
            context=ctx,
            registry=reg,
        )
        assert result.success is True
        assert calls == ["step2"]  # step1 被跳过
        assert "1.0.0" in result.skipped

    def test_部分失败断点记录(self) -> None:
        reg = MigrationRegistry()

        def up_ok(ctx: MigrationContext) -> None:
            pass

        def up_fail(ctx: MigrationContext) -> None:
            raise RuntimeError("故意失败")

        reg.register(Migration("1.0.0", "1.1.0", MigrationKind.DATA, up_ok))
        reg.register(Migration("1.1.0", "1.2.0", MigrationKind.DATA, up_fail))
        reg.register(Migration("1.2.0", "1.3.0", MigrationKind.DATA, up_ok))

        result = migrate(
            from_version="1.0.0",
            to_version="1.3.0",
            context=MigrationContext(),
            registry=reg,
        )
        assert result.success is False
        assert result.failed_at == "1.1.0"
        assert "故意失败" in (result.error or "")
        # step1 已应用，step3 未执行
        assert result.applied == ["1.0.0"]

    def test_无迁移链报错(self) -> None:
        reg = MigrationRegistry()
        # 注册表为空
        result = migrate(
            from_version="1.0.0",
            to_version="1.1.0",
            registry=reg,
        )
        assert result.success is False
        assert "找不到" in (result.error or "")


# ---------------------------------------------------------------------------
# register_migration 装饰器
# ---------------------------------------------------------------------------


class TestRegisterMigrationDecorator:
    def test_装饰器注册到指定registry_默认全局(self) -> None:
        """装饰器注册到全局 REGISTRY。"""
        # 用一个临时 from_version 避免与已有注册冲突
        from server.deploy import migrations as mod

        original = mod.REGISTRY._migrations.copy()
        try:

            @register_migration("99.0.0", "99.1.0", kind=MigrationKind.DATA)
            def _test_migration(ctx: MigrationContext) -> None:
                ctx.state["called"] = True

            m = REGISTRY.get("99.0.0")
            assert m is not None
            assert m.to_version == "99.1.0"

            # 实际执行验证
            ctx = MigrationContext()
            migrate(from_version="99.0.0", to_version="99.1.0", context=ctx)
            assert ctx.state.get("called") is True
        finally:
            mod.REGISTRY._migrations.clear()
            mod.REGISTRY._migrations.update(original)


# ---------------------------------------------------------------------------
# pre_upgrade_check / post_upgrade_check
# ---------------------------------------------------------------------------


class TestPrePostCheck:
    def test_pre_upgrade_check正常通过(self, tmp_path, monkeypatch) -> None:
        monkeypatch.setenv("TEAMHARNESS_DATA_DIR", str(tmp_path))
        from server.deploy.config import reset_deploy_config

        reset_deploy_config()
        result = pre_upgrade_check()
        assert result["ok"] is True
        assert result["checks"]["data_dir_writable"] is True

    def test_post_upgrade_check_version校验(self, monkeypatch) -> None:
        from server.deploy.config import CURRENT_VERSION, DeployConfig, reset_deploy_config

        reset_deploy_config()
        cfg = DeployConfig(version=CURRENT_VERSION, env={})
        result = post_upgrade_check(cfg)
        assert result["checks"]["version"] is True
        # schema_compat 依赖 parse_asset_frontmatter，应通过
        assert result["checks"]["schema_compat"] is True

    def test_post_upgrade_check版本不匹配(self) -> None:
        from server.deploy.config import DeployConfig

        cfg = DeployConfig(version="0.0.0-wrong")
        result = post_upgrade_check(cfg)
        assert result["checks"]["version"] is False


# ---------------------------------------------------------------------------
# CLI main()
# ---------------------------------------------------------------------------


class TestCLI:
    def test_list列出所有迁移(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from server.deploy import migrations as mod
        from server.deploy.migrations import main

        original = mod.REGISTRY._migrations.copy()
        try:
            # 清空原注册表，避免输出过长
            mod.REGISTRY._migrations.clear()
            mod.REGISTRY.register(
                Migration("9.0.0", "9.1.0", MigrationKind.DATA, lambda ctx: None, description="list测试")
            )
            monkeypatch.setattr(
                "sys.argv",
                ["teamharness-migrate", "--list"],
            )
            rc = main()
            assert rc == 0
            captured = capsys.readouterr()
            assert "9.0.0 → 9.1.0" in captured.out
            assert "list测试" in captured.out
        finally:
            mod.REGISTRY._migrations.clear()
            mod.REGISTRY._migrations.update(original)

    def test_dry_run执行成功(
        self, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from server.deploy import migrations as mod
        from server.deploy.migrations import main

        original = mod.REGISTRY._migrations.copy()
        try:
            mod.REGISTRY._migrations.clear()
            mod.REGISTRY.register(
                Migration("8.0.0", "8.1.0", MigrationKind.DATA, lambda ctx: None, description="dry-run测试")
            )
            monkeypatch.setattr(
                "sys.argv",
                [
                    "teamharness-migrate",
                    "--from-version",
                    "8.0.0",
                    "--to-version",
                    "8.1.0",
                    "--dry-run",
                ],
            )
            rc = main()
            assert rc == 0
            captured = capsys.readouterr()
            assert "完成" in captured.out
            assert "8.0.0" in captured.out
        finally:
            mod.REGISTRY._migrations.clear()
            mod.REGISTRY._migrations.update(original)
