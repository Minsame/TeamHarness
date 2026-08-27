"""Task 7 测试：节点发现（SeedDiscovery / MDnsDiscovery / CompositeDiscovery）。"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx

from server.transport.discovery import (
    CompositeDiscovery,
    MDnsDiscovery,
    MDNS_SERVICE_TYPE,
    PeerDiscovery,
    SeedDiscovery,
)
from server.transport.types import PeerInfo


# ----------------------------------------------------------------------
# SeedDiscovery
# ----------------------------------------------------------------------


class TestSeedDiscovery:
    """SeedDiscovery 种子节点发现。"""

    @patch("server.transport.discovery.httpx.get")
    def test_all_seeds_online(self, mock_get):
        """所有种子在线 → 返回全部 PeerInfo。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "peer_id": "alice",
            "agent_id": "agent-1",
            "capabilities": ["ask", "answer"],
        }
        mock_get.return_value = mock_resp

        discovery = SeedDiscovery(seeds=["host1:8080", "host2:8080"])
        peers = discovery.discover()

        assert len(peers) == 2
        assert all(p.online for p in peers)
        assert peers[0].peer_id == "alice"
        assert peers[0].agent_id == "agent-1"
        assert peers[0].capabilities == ["ask", "answer"]
        assert peers[0].endpoint == "host1:8080"
        assert peers[0].last_seen != ""

    @patch("server.transport.discovery.httpx.get")
    def test_partial_seeds_offline(self, mock_get):
        """部分种子返回非 200 → 只返回在线的。"""
        ok_resp = MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {"peer_id": "alice"}

        err_resp = MagicMock()
        err_resp.status_code = 500

        mock_get.side_effect = [ok_resp, err_resp]

        discovery = SeedDiscovery(seeds=["host1:8080", "host2:8080"])
        peers = discovery.discover()

        assert len(peers) == 1
        assert peers[0].peer_id == "alice"

    @patch("server.transport.discovery.httpx.get")
    def test_all_seeds_unreachable(self, mock_get):
        """所有种子不可达 → 返回空列表。"""
        mock_get.side_effect = httpx.ConnectError("connection refused")

        discovery = SeedDiscovery(seeds=["host1:8080", "host2:8080"])
        peers = discovery.discover()

        assert peers == []

    @patch("server.transport.discovery.httpx.get")
    def test_timeout_error_skipped(self, mock_get):
        """请求超时 → 跳过该种子。"""
        mock_get.side_effect = httpx.ReadTimeout("read timeout")

        discovery = SeedDiscovery(seeds=["host1:8080"])
        peers = discovery.discover()

        assert peers == []

    @patch("server.transport.discovery.httpx.get")
    def test_non_json_response_skipped(self, mock_get):
        """响应非 JSON → 跳过该种子。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.side_effect = ValueError("not json")
        mock_get.return_value = mock_resp

        discovery = SeedDiscovery(seeds=["host1:8080"])
        peers = discovery.discover()

        assert peers == []

    @patch("server.transport.discovery.httpx.get")
    def test_peer_id_fallback_to_seed(self, mock_get):
        """响应缺少 peer_id → 用 seed 作为 peer_id。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"agent_id": "agent-1"}
        mock_get.return_value = mock_resp

        discovery = SeedDiscovery(seeds=["host1:8080"])
        peers = discovery.discover()

        assert len(peers) == 1
        assert peers[0].peer_id == "host1:8080"
        assert peers[0].agent_id == "agent-1"

    @patch("server.transport.discovery.httpx.get")
    def test_timeout_passed_to_httpx(self, mock_get):
        """timeout 参数传递给 httpx.get。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"peer_id": "alice"}
        mock_get.return_value = mock_resp

        discovery = SeedDiscovery(seeds=["host1:8080"], timeout=10)
        discovery.discover()

        _, kwargs = mock_get.call_args
        assert kwargs["timeout"] == 10

    @patch("server.transport.discovery.httpx.get")
    def test_empty_capabilities_default(self, mock_get):
        """响应缺少 capabilities → 默认空列表。"""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {"peer_id": "alice"}
        mock_get.return_value = mock_resp

        discovery = SeedDiscovery(seeds=["host1:8080"])
        peers = discovery.discover()

        assert len(peers) == 1
        assert peers[0].capabilities == []

    def test_empty_seeds(self):
        """空种子列表 → 返回空列表。"""
        discovery = SeedDiscovery(seeds=[])
        assert discovery.discover() == []

    def test_register_self_is_noop(self):
        """种子模式下 register_self 是 no-op。"""
        discovery = SeedDiscovery(seeds=["host1:8080"])
        result = discovery.register_self(peer_id="alice", port=8080)
        assert result is None

    def test_is_peer_discovery(self):
        """SeedDiscovery 满足 PeerDiscovery Protocol。"""
        assert isinstance(SeedDiscovery(seeds=[]), PeerDiscovery)


# ----------------------------------------------------------------------
# MDnsDiscovery（Stub 降级，zeroconf 未安装）
# ----------------------------------------------------------------------


class TestMDnsDiscoveryStub:
    """MDnsDiscovery Stub 降级（zeroconf 未安装时）。"""

    def test_stub_construction_no_args(self):
        """Stub 无参构造不抛异常。"""
        discovery = MDnsDiscovery()
        assert discovery is not None

    def test_stub_construction_with_kwargs(self):
        """Stub 接受关键字参数（构造签名兼容）。"""
        discovery = MDnsDiscovery(scan_timeout=2.0)
        assert discovery is not None

    def test_stub_construction_with_positional_and_kwargs(self):
        """Stub 接受位置参数 + 关键字参数（构造签名兼容）。"""
        discovery = MDnsDiscovery(1.0, "extra", key="value")
        assert discovery is not None

    def test_stub_discover_returns_empty(self):
        """Stub discover 返回空列表。"""
        discovery = MDnsDiscovery()
        assert discovery.discover() == []

    def test_stub_register_self_noop(self):
        """Stub register_self 是 no-op。"""
        discovery = MDnsDiscovery()
        result = discovery.register_self(peer_id="alice", port=8080)
        assert result is None

    def test_stub_is_peer_discovery(self):
        """Stub 满足 PeerDiscovery Protocol。"""
        assert isinstance(MDnsDiscovery(), PeerDiscovery)


# ----------------------------------------------------------------------
# CompositeDiscovery
# ----------------------------------------------------------------------


class TestCompositeDiscovery:
    """CompositeDiscovery 组合发现。"""

    def test_merge_multiple_sources(self):
        """合并多个来源的 PeerInfo。"""
        d1 = _StaticDiscovery([PeerInfo(peer_id="alice", online=True)])
        d2 = _StaticDiscovery([PeerInfo(peer_id="bob", online=True)])

        composite = CompositeDiscovery(discoveries=[d1, d2])
        peers = composite.discover()

        peer_ids = {p.peer_id for p in peers}
        assert peer_ids == {"alice", "bob"}

    def test_dedup_by_peer_id(self):
        """按 peer_id 去重（后出现的覆盖先出现的）。"""
        d1 = _StaticDiscovery(
            [PeerInfo(peer_id="alice", endpoint="old:8080", online=False)]
        )
        d2 = _StaticDiscovery(
            [PeerInfo(peer_id="alice", endpoint="new:8080", online=True)]
        )

        composite = CompositeDiscovery(discoveries=[d1, d2])
        peers = composite.discover()

        assert len(peers) == 1
        assert peers[0].endpoint == "new:8080"
        assert peers[0].online is True

    def test_register_self_calls_all(self):
        """register_self 调用所有子 discovery。"""
        d1 = _RecordingDiscovery()
        d2 = _RecordingDiscovery()

        composite = CompositeDiscovery(discoveries=[d1, d2])
        composite.register_self(peer_id="alice", port=8080)

        assert d1.registered == [("alice", 8080)]
        assert d2.registered == [("alice", 8080)]

    def test_empty_discoveries_discover(self):
        """空 discoveries 列表 → discover 返回空列表。"""
        composite = CompositeDiscovery(discoveries=[])
        assert composite.discover() == []

    def test_empty_discoveries_register_self(self):
        """空 discoveries 列表 → register_self 不抛异常。"""
        composite = CompositeDiscovery(discoveries=[])
        composite.register_self(peer_id="alice", port=8080)  # should not raise

    def test_is_peer_discovery(self):
        """CompositeDiscovery 满足 PeerDiscovery Protocol。"""
        assert isinstance(CompositeDiscovery(discoveries=[]), PeerDiscovery)

    def test_mixed_seed_and_mdns(self):
        """混合 SeedDiscovery + MDnsDiscovery（Stub）→ 仅种子返回结果。"""
        seed = _StaticDiscovery([PeerInfo(peer_id="alice", online=True)])
        mdns = MDnsDiscovery()  # Stub，discover 返回空

        composite = CompositeDiscovery(discoveries=[seed, mdns])
        peers = composite.discover()

        assert len(peers) == 1
        assert peers[0].peer_id == "alice"


# ----------------------------------------------------------------------
# 辅助类
# ----------------------------------------------------------------------


class _StaticDiscovery:
    """返回固定 PeerInfo 列表的测试用 discovery。"""

    def __init__(self, peers: list[PeerInfo]) -> None:
        self._peers = peers

    def discover(self) -> list[PeerInfo]:
        return list(self._peers)

    def register_self(self, peer_id: str, port: int) -> None:
        pass


class _RecordingDiscovery:
    """记录 register_self 调用的测试用 discovery。"""

    def __init__(self) -> None:
        self.registered: list[tuple[str, int]] = []

    def discover(self) -> list[PeerInfo]:
        return []

    def register_self(self, peer_id: str, port: int) -> None:
        self.registered.append((peer_id, port))


# ----------------------------------------------------------------------
# 常量
# ----------------------------------------------------------------------


class TestServiceTypeConstant:
    """mDNS 服务名常量。"""

    def test_service_type_value(self):
        assert MDNS_SERVICE_TYPE == "_teamharness._tcp.local."
