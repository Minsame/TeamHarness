"""客户端配置管理测试（SubTask 6.11 一部分）。

覆盖：
- ClientConfig 默认值 + 路径解析
- load_client_config 优先级（参数 > env > 文件 > 默认）
- save_client_config（api_key 不落盘）
- is_offline_mode 判定
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from server.client.config import (
    ClientConfig,
    DEFAULT_ASYNC_COMM,
    DEFAULT_DISCOVERY,
    DEFAULT_NETWORK_CHECK_INTERVAL,
    DEFAULT_TARGET_BRANCH,
    DEFAULT_TOPOLOGY,
    load_client_config,
    save_client_config,
    TEAMHARNESS_DIR,
)
from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
    VALID_TOPOLOGIES,
)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


def test_default_config_values():
    cfg = ClientConfig()
    assert cfg.server_url == ""
    assert cfg.api_key == ""
    assert cfg.sync_strategy == "manual"
    assert cfg.target_branch == DEFAULT_TARGET_BRANCH
    assert cfg.network_check_interval_seconds == DEFAULT_NETWORK_CHECK_INTERVAL
    assert cfg.offline_recall_local_only is True
    # 拓扑配置默认值
    assert cfg.topology == DEFAULT_TOPOLOGY == TOPOLOGY_CENTRAL == "central"
    assert cfg.peers == []
    assert cfg.discovery == DEFAULT_DISCOVERY == "seed"
    assert cfg.async_comm == {}


def test_default_config_mutable_defaults_are_independent():
    """mutable default 字段（peers/async_comm/extras）在不同实例间互不影响。"""
    a = ClientConfig()
    b = ClientConfig()
    a.peers.append("host:1")
    a.async_comm["snapshot_policy"] = "never"
    assert b.peers == []
    assert b.async_comm == {}


def test_resolve_async_comm_config_returns_defaults_when_empty():
    cfg = ClientConfig()
    merged = cfg.resolve_async_comm_config()
    assert merged == DEFAULT_ASYNC_COMM
    assert merged["snapshot_policy"] == "on_demand"
    assert merged["snapshot_ttl_days"] == 30
    assert merged["conflict_threshold"] == 0.3
    assert merged["auto_confirm_threshold"] == 0.8


def test_resolve_async_comm_config_user_overrides_defaults():
    cfg = ClientConfig(
        async_comm={"snapshot_policy": "never", "snapshot_ttl_days": 7}
    )
    merged = cfg.resolve_async_comm_config()
    # 用户值覆盖默认值
    assert merged["snapshot_policy"] == "never"
    assert merged["snapshot_ttl_days"] == 7
    # 未覆盖的键仍为默认值
    assert merged["conflict_threshold"] == 0.3
    assert merged["auto_confirm_threshold"] == 0.8


def test_resolve_async_comm_config_does_not_mutate_self():
    """resolve_async_comm_config 不应修改 self.async_comm。"""
    cfg = ClientConfig(async_comm={"snapshot_policy": "never"})
    merged = cfg.resolve_async_comm_config()
    assert "conflict_threshold" in merged
    assert "conflict_threshold" not in cfg.async_comm
    assert cfg.async_comm == {"snapshot_policy": "never"}


def test_resolve_paths_with_explicit_repo_root(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    assert cfg.resolve_repo_root() == tmp_path
    assert cfg.resolve_mapping_path() == tmp_path / TEAMHARNESS_DIR / "mapping.yaml"
    assert cfg.resolve_manifest_path() == tmp_path / TEAMHARNESS_DIR / "manifest.json"
    assert cfg.resolve_private_dir() == tmp_path / TEAMHARNESS_DIR / "private"
    assert cfg.resolve_teamharness_dir() == tmp_path / TEAMHARNESS_DIR


def test_resolve_personal_branch_with_member_id():
    cfg = ClientConfig(member_id="alice")
    assert cfg.resolve_personal_branch() == "members/alice"


def test_resolve_personal_branch_explicit_wins():
    cfg = ClientConfig(member_id="alice", personal_branch="feature/x")
    assert cfg.resolve_personal_branch() == "feature/x"


def test_resolve_personal_branch_no_member():
    cfg = ClientConfig()
    assert cfg.resolve_personal_branch() == "members/default"


def test_is_offline_mode_no_server_url():
    cfg = ClientConfig()
    assert cfg.is_offline_mode() is True


def test_is_offline_mode_with_server_url():
    cfg = ClientConfig(server_url="https://th.example.com")
    assert cfg.is_offline_mode() is False


def test_is_offline_mode_explicit_extras():
    cfg = ClientConfig(server_url="https://th.example.com", extras={"offline": True})
    assert cfg.is_offline_mode() is True


def test_is_auto_sync():
    assert ClientConfig(sync_strategy="auto").is_auto_sync() is True
    assert ClientConfig(sync_strategy="manual").is_auto_sync() is False


# ---------------------------------------------------------------------------
# load_client_config 优先级
# ---------------------------------------------------------------------------


def test_load_config_uses_defaults_when_nothing_set(tmp_path: Path, monkeypatch):
    # 清理环境变量
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.repo_root == str(tmp_path)
    assert cfg.server_url == ""
    assert cfg.api_key == ""
    assert cfg.sync_strategy == "manual"


def test_load_config_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_SERVER_URL", "https://th.example.com")
    monkeypatch.setenv("TEAMHARNESS_API_KEY", "sk-xxx")
    monkeypatch.setenv("TEAMHARNESS_MEMBER_ID", "alice")
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.server_url == "https://th.example.com"
    assert cfg.api_key == "sk-xxx"
    assert cfg.member_id == "alice"


def test_load_config_from_file(tmp_path: Path, monkeypatch):
    # 清理 env，避免干扰
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "server_url": "https://file.example.com",
                "member_id": "bob",
                "sync_strategy": "auto",
                "target_branch": "develop",
            }
        ),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.server_url == "https://file.example.com"
    assert cfg.member_id == "bob"
    assert cfg.sync_strategy == "auto"
    assert cfg.target_branch == "develop"


def test_load_config_param_overrides_env_and_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_SERVER_URL", "https://env.example.com")
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump({"server_url": "https://file.example.com"}),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path, server_url="https://param.example.com")
    assert cfg.server_url == "https://param.example.com"


def test_load_config_overrides_kwargs(tmp_path: Path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_client_config(repo_root=tmp_path, sync_strategy="auto", request_timeout_seconds=42)
    assert cfg.sync_strategy == "auto"
    assert cfg.request_timeout_seconds == 42


def test_load_config_invalid_env_int_falls_back(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_NETWORK_CHECK_INTERVAL", "not-a-number")
    cfg = load_client_config(repo_root=tmp_path)
    # 回退到默认值
    assert cfg.network_check_interval_seconds == DEFAULT_NETWORK_CHECK_INTERVAL


# ---------------------------------------------------------------------------
# save_client_config
# ---------------------------------------------------------------------------


def test_save_config_writes_yaml(tmp_path: Path):
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        member_id="alice",
        api_key="sk-secret",
    )
    path = save_client_config(cfg)
    assert path.is_file()
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["server_url"] == "https://th.example.com"
    assert data["member_id"] == "alice"
    # api_key 不落盘
    assert "api_key" not in data


def test_save_config_creates_parent_dir(tmp_path: Path):
    cfg = ClientConfig(repo_root=str(tmp_path))
    path = save_client_config(cfg)
    assert path.is_file()
    assert path.parent == tmp_path / TEAMHARNESS_DIR


def test_save_config_round_trip(tmp_path: Path, monkeypatch):
    # 清理 env 避免干扰
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        server_url="https://th.example.com",
        member_id="alice",
        sync_strategy="auto",
    )
    save_client_config(cfg)
    loaded = load_client_config(repo_root=tmp_path)
    assert loaded.server_url == "https://th.example.com"
    assert loaded.member_id == "alice"
    assert loaded.sync_strategy == "auto"


def test_save_config_round_trip_topology_fields(tmp_path: Path, monkeypatch):
    """save/load 往返应保持 topology/peers/discovery/async_comm 字段。"""
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        topology=TOPOLOGY_HYBRID,
        peers=["10.0.0.1:7000", "10.0.0.2:7000"],
        discovery="mdns",
        async_comm={"snapshot_policy": "never", "snapshot_ttl_days": 14},
    )
    save_client_config(cfg)
    loaded = load_client_config(repo_root=tmp_path)
    assert loaded.topology == TOPOLOGY_HYBRID
    assert loaded.peers == ["10.0.0.1:7000", "10.0.0.2:7000"]
    assert loaded.discovery == "mdns"
    assert loaded.async_comm == {"snapshot_policy": "never", "snapshot_ttl_days": 14}


def test_save_config_serializes_new_fields(tmp_path: Path):
    """save_client_config 应将新字段写入 yaml。"""
    cfg = ClientConfig(
        repo_root=str(tmp_path),
        topology=TOPOLOGY_P2P,
        peers=["peer.example:9000"],
        discovery="composite",
        async_comm={"conflict_threshold": 0.5},
    )
    path = save_client_config(cfg)
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert data["topology"] == "p2p"
    assert data["peers"] == ["peer.example:9000"]
    assert data["discovery"] == "composite"
    assert data["async_comm"] == {"conflict_threshold": 0.5}


# ---------------------------------------------------------------------------
# 拓扑配置加载（env / 文件）
# ---------------------------------------------------------------------------


def test_load_config_topology_from_env(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_TOPOLOGY", TOPOLOGY_P2P)
    monkeypatch.setenv("TEAMHARNESS_DISCOVERY", "mdns")
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_P2P
    assert cfg.discovery == "mdns"


def test_load_config_peers_from_env_comma_separated(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_PEERS", "10.0.0.1:7000, 10.0.0.2:7000 ,10.0.0.3:7000")
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.peers == ["10.0.0.1:7000", "10.0.0.2:7000", "10.0.0.3:7000"]


def test_load_config_peers_from_env_empty(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_PEERS", "")
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.peers == []


def test_load_config_topology_from_file(tmp_path: Path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "topology": TOPOLOGY_HYBRID,
                "peers": ["seed1.example:7000", "seed2.example:7000"],
                "discovery": "composite",
                "async_comm": {
                    "snapshot_policy": "on_commit",
                    "auto_confirm_threshold": 0.9,
                },
            }
        ),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_HYBRID
    assert cfg.peers == ["seed1.example:7000", "seed2.example:7000"]
    assert cfg.discovery == "composite"
    assert cfg.async_comm == {
        "snapshot_policy": "on_commit",
        "auto_confirm_threshold": 0.9,
    }


def test_load_config_param_overrides_env_topology(tmp_path: Path, monkeypatch):
    """参数（通过 overrides）优先于 env。"""
    monkeypatch.setenv("TEAMHARNESS_TOPOLOGY", TOPOLOGY_P2P)
    cfg = load_client_config(repo_root=tmp_path, topology=TOPOLOGY_HYBRID)
    assert cfg.topology == TOPOLOGY_HYBRID


def test_load_config_env_overrides_file_topology(tmp_path: Path, monkeypatch):
    """env 优先于文件。"""
    monkeypatch.setenv("TEAMHARNESS_TOPOLOGY", TOPOLOGY_P2P)
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump({"topology": TOPOLOGY_HYBRID}),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_P2P


def test_load_config_peers_env_overrides_file(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TEAMHARNESS_PEERS", "env.host:7000")
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump({"peers": ["file.host:7000"]}),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.peers == ["env.host:7000"]


# ---------------------------------------------------------------------------
# 拓扑校验：无效值回退到 central
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("invalid_value", ["invalid", "CENTRAL", "P2P", "xyz", "hybrid "])
def test_load_config_invalid_topology_falls_back_to_central(
    tmp_path: Path, monkeypatch, invalid_value
):
    """topology 值不在 VALID_TOPOLOGIES 中时回退到 central。

    覆盖：拼写错误、大小写错误、前后空白、未知值。
    """
    monkeypatch.setenv("TEAMHARNESS_TOPOLOGY", str(invalid_value))
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_CENTRAL


def test_load_config_invalid_topology_in_file_falls_back(tmp_path: Path, monkeypatch):
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    th_dir = tmp_path / TEAMHARNESS_DIR
    th_dir.mkdir(parents=True)
    (th_dir / "config.yaml").write_text(
        yaml.safe_dump({"topology": "bogus"}),
        encoding="utf-8",
    )
    cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_CENTRAL


def test_load_config_invalid_topology_via_overrides_falls_back(tmp_path: Path, monkeypatch):
    """overrides 传入非法 topology 也应被校验回退。"""
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    cfg = load_client_config(repo_root=tmp_path, topology="not-a-topology")
    assert cfg.topology == TOPOLOGY_CENTRAL


def test_load_config_valid_topologies_accepted(tmp_path: Path, monkeypatch):
    """三种合法拓扑均应被接受。"""
    for k in list(os.environ):
        if k.startswith("TEAMHARNESS_"):
            monkeypatch.delenv(k, raising=False)
    for topo in VALID_TOPOLOGIES:
        cfg = load_client_config(repo_root=tmp_path, topology=topo)
        assert cfg.topology == topo


def test_load_config_invalid_topology_logs_warning(tmp_path: Path, monkeypatch, caplog):
    """无效 topology 回退时应记录 warning 日志。"""
    monkeypatch.setenv("TEAMHARNESS_TOPOLOGY", "bogus")
    with caplog.at_level("WARNING", logger="server.client.config"):
        cfg = load_client_config(repo_root=tmp_path)
    assert cfg.topology == TOPOLOGY_CENTRAL
    assert any("fallback" in rec.message.lower() for rec in caplog.records)
