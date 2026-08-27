"""frontmatter schema_version 兼容解析测试（SubTask 3.7）。

覆盖：
- 旧版本（缺失 schema_version）解析为 v1
- 单块 / 双区 frontmatter 解析
- 迁移函数链应用
- 高于目标版本不降级
- 迁移函数幂等
- 异常输入（非 dict / 非法版本）
"""

from __future__ import annotations

import pytest

from server.deploy.schema_version import (
    SCHEMA_VERSION_CURRENT,
    SchemaMigrator,
    parse_asset_frontmatter,
    register_migration,
    validate_compatibility,
)


# ---------------------------------------------------------------------------
# parse_asset_frontmatter
# ---------------------------------------------------------------------------


class TestParseAssetFrontmatter:
    def test_单块frontmatter无schema_version视为v1(self) -> None:
        content = (
            "---\n"
            "id: rule-test\n"
            "type: rule\n"
            "owner: alice\n"
            "scope: team\n"
            "tags: [backend]\n"
            "---\n"
            "# 规则正文\n"
        )
        fm, body, trace = parse_asset_frontmatter(content)
        assert fm["id"] == "rule-test"
        assert fm["type"] == "rule"
        assert fm["owner"] == "alice"
        assert fm["scope"] == "team"
        assert fm["tags"] == ["backend"]
        assert "规则正文" in body
        # 缺失 schema_version 视为 1
        assert trace.original_version == 1
        assert trace.final_version == SCHEMA_VERSION_CURRENT

    def test_显式schema_version_1(self) -> None:
        content = (
            "---\n"
            "id: rule-test\n"
            "schema_version: 1\n"
            "---\n"
            "body\n"
        )
        fm, _, trace = parse_asset_frontmatter(content)
        assert trace.original_version == 1
        assert fm["schema_version"] == SCHEMA_VERSION_CURRENT

    def test_双区frontmatter取teamharness区(self) -> None:
        content = (
            "---\n"
            "coding: trae\n"
            "description: trae专用字段\n"
            "---\n"
            "\n"
            "---\n"
            "teamharness:\n"
            "  id: rule-test\n"
            "  type: rule\n"
            "  schema_version: 1\n"
            "---\n"
            "正文\n"
        )
        fm, body, _ = parse_asset_frontmatter(content)
        assert fm["id"] == "rule-test"
        assert fm["type"] == "rule"
        assert "coding" not in fm  # coding 区字段不混入 teamharness 区
        assert "正文" in body

    def test_空内容返回空dict(self) -> None:
        fm, body, trace = parse_asset_frontmatter("")
        assert fm == {}
        assert body == ""
        assert trace.original_version == 1

    def test_无frontmatter返回原文(self) -> None:
        content = "# 纯正文，无 frontmatter\n"
        fm, body, _ = parse_asset_frontmatter(content)
        assert fm == {}
        assert body == content

    def test_YAML解析为非dict按空处理(self) -> None:
        # YAML 解析为字符串而非 dict
        content = "---\njust a string\n---\nbody\n"
        fm, body, _ = parse_asset_frontmatter(content)
        assert fm == {}


# ---------------------------------------------------------------------------
# SchemaMigrator
# ---------------------------------------------------------------------------


class TestSchemaMigrator:
    def test_目标版本已是当前版本无迁移(self) -> None:
        migrator = SchemaMigrator(target_version=SCHEMA_VERSION_CURRENT)
        data = {"id": "x", "schema_version": SCHEMA_VERSION_CURRENT}
        result, trace = migrator.migrate(data)
        assert result == data
        assert trace.migrated is False
        assert trace.steps == []

    def test_缺失schema_version视为1(self) -> None:
        migrator = SchemaMigrator(target_version=SCHEMA_VERSION_CURRENT)
        data = {"id": "x"}
        result, trace = migrator.migrate(data)
        assert trace.original_version == 1
        # 若 SCHEMA_VERSION_CURRENT == 1 则无迁移；否则应用链
        if SCHEMA_VERSION_CURRENT > 1:
            assert trace.migrated is True
            assert result["schema_version"] == SCHEMA_VERSION_CURRENT

    def test_迁移不修改输入dict(self) -> None:
        """迁移函数应深拷贝，不污染输入。"""
        migrator = SchemaMigrator(target_version=3)
        data = {"id": "x", "schema_version": 1}
        original = dict(data)
        migrator.migrate(data)
        assert data == original  # 输入未被修改

    def test_高于目标版本不降级(self) -> None:
        """向前兼容：v3 数据在目标 v1 下保留原字段。"""
        migrator = SchemaMigrator(target_version=1)
        data = {"id": "x", "schema_version": 5, "future_field": "value"}
        result, trace = migrator.migrate(data)
        assert trace.original_version == 5
        assert trace.final_version == 5
        assert result["future_field"] == "value"  # 不删除未来字段
        assert any("不降级" in s for s in trace.steps)

    def test_非dict输入抛TypeError(self) -> None:
        migrator = SchemaMigrator()
        with pytest.raises(TypeError, match="必须是 dict"):
            migrator.migrate(["not", "a", "dict"])  # type: ignore[arg-type]

    def test_目标版本小于1抛ValueError(self) -> None:
        with pytest.raises(ValueError, match="target_version 必须 ≥ 1"):
            SchemaMigrator(target_version=0)


# ---------------------------------------------------------------------------
# 迁移函数链
# ---------------------------------------------------------------------------


class TestMigrationChain:
    def test_v1到v3链式应用(self) -> None:
        """v1 → v2 → v3 链式迁移，新增字段依次补全。"""
        migrator = SchemaMigrator(target_version=3)
        data = {"id": "x", "type": "rule", "schema_version": 1}
        result, trace = migrator.migrate(data)
        assert result["schema_version"] == 3
        # v2 新增字段
        assert "module_path" in result
        assert "category" in result
        # v3 新增字段
        assert "distillation_metadata" in result
        assert isinstance(result["distillation_metadata"], dict)
        # 轨迹包含两步
        assert len(trace.steps) == 2
        assert trace.fields_added  # 至少有新增字段

    def test_迁移函数幂等(self) -> None:
        """重复迁移同份数据结果一致。"""
        migrator = SchemaMigrator(target_version=3)
        data = {"id": "x", "schema_version": 1}
        r1, _ = migrator.migrate(data)
        r2, _ = migrator.migrate(r1)
        assert r1 == r2

    def test_v2到v3单步迁移(self) -> None:
        migrator = SchemaMigrator(target_version=3)
        data = {
            "id": "x",
            "schema_version": 2,
            "module_path": "modules/backend",
            "category": "rule-backend",
        }
        result, trace = migrator.migrate(data)
        assert trace.original_version == 2
        assert trace.final_version == 3
        assert result["schema_version"] == 3
        # v2 已有字段保留
        assert result["module_path"] == "modules/backend"


# ---------------------------------------------------------------------------
# register_migration 装饰器
# ---------------------------------------------------------------------------


class TestRegisterMigration:
    def test_非连续版本抛ValueError(self) -> None:
        from server.deploy import schema_version as sv

        # 临时保存注册表，测试后恢复
        original_registry = sv._REGISTRY.copy()
        try:
            with pytest.raises(ValueError, match="迁移必须连续"):

                @register_migration(10, 12, "跳跃版本")
                def _bad(data):  # type: ignore[no-untyped-def]
                    return data
        finally:
            sv._REGISTRY.clear()
            sv._REGISTRY.update(original_registry)


# ---------------------------------------------------------------------------
# validate_compatibility
# ---------------------------------------------------------------------------


class TestValidateCompatibility:
    def test_合法数据无违规(self) -> None:
        issues = validate_compatibility({"id": "x", "schema_version": 1})
        assert issues == []

    def test_缺失schema_version无违规(self) -> None:
        issues = validate_compatibility({"id": "x"})
        assert issues == []

    def test_非法schema_version抛违规(self) -> None:
        issues = validate_compatibility({"schema_version": "abc"})
        assert len(issues) == 1
        assert "schema_version" in issues[0]

    def test_负数schema_version抛违规(self) -> None:
        issues = validate_compatibility({"schema_version": -1})
        assert len(issues) == 1
