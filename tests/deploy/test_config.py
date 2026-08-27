"""DeployConfig 公共 API 契约测试（SubTask 3.7 + 公共 API 契约）。

覆盖：
- get_mode() 在三种模式下的探测正确性
- get_storage_backend() 默认组合与显式覆盖
- get_version() 默认值与环境变量覆盖
- 单例缓存与重置
- semver 解析

测试铁律：正常 + 边界 + 异常输入全覆盖。
"""

from __future__ import annotations

import pytest

from server.deploy.config import (
    CURRENT_VERSION,
    ALL_IN_ONE_BACKEND,
    DOCKER_COMPOSE_BACKEND,
    SINGLE_MACHINE_BACKEND,
    DeployConfig,
    DeployMode,
    StorageBackend,
    StorageKind,
    data_dir,
    get_deploy_config,
    parse_semver,
    reset_deploy_config,
)


# ---------------------------------------------------------------------------
# get_mode()
# ---------------------------------------------------------------------------


class TestGetMode:
    """部署模式探测。"""

    def test_explicit_all_in_one(self) -> None:
        cfg = DeployConfig(mode=DeployMode.ALL_IN_ONE)
        assert cfg.get_mode() == DeployMode.ALL_IN_ONE

    def test_explicit_docker_compose(self) -> None:
        cfg = DeployConfig(mode=DeployMode.DOCKER_COMPOSE)
        assert cfg.get_mode() == DeployMode.DOCKER_COMPOSE

    def test_explicit_single_machine(self) -> None:
        cfg = DeployConfig(mode=DeployMode.SINGLE_MACHINE)
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_env_teamharness_deploy_mode_all_in_one(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "all-in-one"})
        assert cfg.get_mode() == DeployMode.ALL_IN_ONE

    def test_env_teamharness_deploy_mode_docker_compose(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "docker-compose"})
        assert cfg.get_mode() == DeployMode.DOCKER_COMPOSE

    def test_env_teamharness_deploy_mode_single_machine(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "single-machine"})
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_env_teamharness_deploy_mode_snake_case兼容(self) -> None:
        """snake_case 输入也应被解析（容错）。"""
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "all_in_one"})
        assert cfg.get_mode() == DeployMode.ALL_IN_ONE

        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "single_machine"})
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_env_teamharness_deploy_mode_空格兼容(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "single machine"})
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_env_in_docker_true(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_IN_DOCKER": "true"})
        assert cfg.get_mode() == DeployMode.DOCKER_COMPOSE

    def test_env_all_in_one_true(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_ALL_IN_ONE": "1"})
        assert cfg.get_mode() == DeployMode.ALL_IN_ONE

    def test_env_in_docker优先级高于_all_in_one(self) -> None:
        """同时设置时 IN_DOCKER 优先（容器内更可靠）。"""
        cfg = DeployConfig(
            env={"TEAMHARNESS_IN_DOCKER": "true", "TEAMHARNESS_ALL_IN_ONE": "true"}
        )
        assert cfg.get_mode() == DeployMode.DOCKER_COMPOSE

    def test_env_teamharness_deploy_mode优先级最高(self) -> None:
        cfg = DeployConfig(
            env={
                "TEAMHARNESS_DEPLOY_MODE": "single-machine",
                "TEAMHARNESS_IN_DOCKER": "true",
            }
        )
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_无环境变量默认single_machine(self) -> None:
        cfg = DeployConfig(env={})
        assert cfg.get_mode() == DeployMode.SINGLE_MACHINE

    def test_未知模式抛ValueError(self) -> None:
        # DeployConfig 构造是延迟探测设计，只在 get_mode() 调用时才解析
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "cloud-native"})
        with pytest.raises(ValueError, match="未知部署模式"):
            cfg.get_mode()

    def test_大小写不敏感(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_DEPLOY_MODE": "ALL-IN-ONE"})
        assert cfg.get_mode() == DeployMode.ALL_IN_ONE

    def test_is_all_in_one_helper(self) -> None:
        assert DeployConfig(mode=DeployMode.ALL_IN_ONE).is_all_in_one() is True
        assert DeployConfig(mode=DeployMode.DOCKER_COMPOSE).is_all_in_one() is False

    def test_is_docker_compose_helper(self) -> None:
        assert DeployConfig(mode=DeployMode.DOCKER_COMPOSE).is_docker_compose() is True
        assert DeployConfig(mode=DeployMode.ALL_IN_ONE).is_docker_compose() is False


# ---------------------------------------------------------------------------
# get_storage_backend()
# ---------------------------------------------------------------------------


class TestGetStorageBackend:
    """存储后端组合。"""

    def test_all_in_one默认后端(self) -> None:
        cfg = DeployConfig(mode=DeployMode.ALL_IN_ONE)
        backend = cfg.get_storage_backend()
        assert backend == ALL_IN_ONE_BACKEND
        assert backend.meta_db == StorageKind.SQLITE
        assert backend.vector_store == StorageKind.SQLITE_VEC
        assert backend.git_provider == StorageKind.LIBGIT2

    def test_docker_compose默认后端(self) -> None:
        cfg = DeployConfig(mode=DeployMode.DOCKER_COMPOSE)
        backend = cfg.get_storage_backend()
        assert backend == DOCKER_COMPOSE_BACKEND
        assert backend.meta_db == StorageKind.POSTGRES
        assert backend.vector_store == StorageKind.QDRANT
        assert backend.git_provider == StorageKind.GITEA

    def test_single_machine默认后端(self) -> None:
        cfg = DeployConfig(mode=DeployMode.SINGLE_MACHINE)
        backend = cfg.get_storage_backend()
        assert backend == SINGLE_MACHINE_BACKEND
        assert backend.meta_db == StorageKind.POSTGRES
        assert backend.vector_store == StorageKind.PGVECTOR
        assert backend.git_provider == StorageKind.LIBGIT2

    def test_显式backend覆盖(self) -> None:
        custom = StorageBackend(
            meta_db=StorageKind.SQLITE,
            vector_store=StorageKind.PGVECTOR,  # 不寻常但允许
            git_provider=StorageKind.GITLAB,
        )
        cfg = DeployConfig(mode=DeployMode.ALL_IN_ONE, backend=custom)
        assert cfg.get_storage_backend() == custom

    def test_环境变量按字段覆盖(self) -> None:
        """环境变量逐字段覆盖默认组合。"""
        cfg = DeployConfig(
            mode=DeployMode.DOCKER_COMPOSE,
            env={"TEAMHARNESS_VECTOR_STORE": "pgvector"},
        )
        backend = cfg.get_storage_backend()
        assert backend.meta_db == StorageKind.POSTGRES  # 默认
        assert backend.vector_store == StorageKind.PGVECTOR  # 覆盖
        assert backend.git_provider == StorageKind.GITEA  # 默认

    def test_as_dict格式(self) -> None:
        cfg = DeployConfig(mode=DeployMode.ALL_IN_ONE)
        d = cfg.get_storage_backend().as_dict()
        assert d == {
            "meta_db": "sqlite",
            "vector_store": "sqlite-vec",
            "git_provider": "libgit2",
        }


# ---------------------------------------------------------------------------
# get_version()
# ---------------------------------------------------------------------------


class TestGetVersion:
    """版本号。"""

    def test_默认版本(self) -> None:
        cfg = DeployConfig(env={})
        assert cfg.get_version() == CURRENT_VERSION

    def test_显式版本(self) -> None:
        cfg = DeployConfig(version="2.0.0")
        assert cfg.get_version() == "2.0.0"

    def test_环境变量覆盖(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_VERSION": "1.5.0"})
        assert cfg.get_version() == "1.5.0"

    def test_空环境变量用默认(self) -> None:
        cfg = DeployConfig(env={"TEAMHARNESS_VERSION": "  "})
        assert cfg.get_version() == CURRENT_VERSION

    def test_显式优先于环境变量(self) -> None:
        cfg = DeployConfig(version="3.0.0", env={"TEAMHARNESS_VERSION": "1.5.0"})
        assert cfg.get_version() == "3.0.0"


# ---------------------------------------------------------------------------
# as_dict / 单例
# ---------------------------------------------------------------------------


class TestAsDictAndSingleton:
    def test_as_dict包含三个核心字段(self) -> None:
        cfg = DeployConfig(mode=DeployMode.ALL_IN_ONE, version="1.2.3")
        d = cfg.as_dict()
        assert d["mode"] == "all-in-one"
        assert d["version"] == "1.2.3"
        assert "storage_backend" in d
        assert d["storage_backend"]["meta_db"] == "sqlite"

    def test_单例缓存返回同一实例(self) -> None:
        reset_deploy_config()
        c1 = get_deploy_config()
        c2 = get_deploy_config()
        assert c1 is c2

    def test_reset后单例重置(self) -> None:
        reset_deploy_config()
        c1 = get_deploy_config()
        reset_deploy_config()
        c2 = get_deploy_config()
        assert c1 is not c2


# ---------------------------------------------------------------------------
# parse_semver()
# ---------------------------------------------------------------------------


class TestParseSemver:
    @pytest.mark.parametrize(
        "version,expected",
        [
            ("1.0.0", (1, 0, 0)),
            ("0.0.1", (0, 0, 1)),
            ("10.20.30", (10, 20, 30)),
            ("1.0.0-alpha", (1, 0, 0)),
            ("1.0.0+build123", (1, 0, 0)),
            ("2.5.3-beta.1+exp.sha.5114f85", (2, 5, 3)),
            ("  1.2.3  ", (1, 2, 3)),  # 含空格
        ],
    )
    def test_合法版本(self, version: str, expected: tuple) -> None:
        assert parse_semver(version) == expected

    @pytest.mark.parametrize(
        "version",
        [
            "",
            "1",
            "1.0",
            "1.0.0.0",
            "v1.0.0",
            "1.0.0-",
            "01.0.0",  # 前导 0 非法
            "1.0.x",
            "abc",
        ],
    )
    def test_非法版本抛ValueError(self, version: str) -> None:
        with pytest.raises(ValueError, match="非法语义化版本"):
            parse_semver(version)


# ---------------------------------------------------------------------------
# data_dir()
# ---------------------------------------------------------------------------


class TestDataDir:
    def test_环境变量优先(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        monkeypatch.setenv("TEAMHARNESS_DATA_DIR", str(tmp_path))
        assert data_dir() == tmp_path

    def test_默认走home目录(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TEAMHARNESS_DATA_DIR", raising=False)
        monkeypatch.setenv("HOME", "/tmp/fake-home")
        result = data_dir()
        assert ".teamharness" in str(result)
        assert "data" in str(result)
