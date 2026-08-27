"""节点发现：mDNS + 种子混合发现。

对应 Task 7：
- PeerDiscovery Protocol：统一发现接口
- SeedDiscovery：从静态配置的种子节点列表发现 peer（HTTP 探测）
- MDnsDiscovery：通过 zeroconf 在局域网广播和发现 peer（zeroconf 未安装时降级为 Stub）
- CompositeDiscovery：组合多个发现源，按 peer_id 去重

设计要点：
- SeedDiscovery 用 httpx（已是项目依赖）对每个种子发起 HTTP GET /peer/info
- MDnsDiscovery 的 zeroconf 为可选依赖，未安装时降级为 no-op Stub
- CompositeDiscovery 合并多个来源的 PeerInfo，按 peer_id 去重（后出现的覆盖先出现的）
"""

from __future__ import annotations

import logging
import socket
import time
from datetime import datetime, timezone
from typing import Protocol, runtime_checkable

import httpx

from server.transport.types import PeerInfo

logger = logging.getLogger(__name__)

# mDNS 服务名常量
MDNS_SERVICE_TYPE = "_teamharness._tcp.local."

# SeedDiscovery 探测的 HTTP 端点路径
SEED_PEER_INFO_PATH = "/peer/info"


@runtime_checkable
class PeerDiscovery(Protocol):
    """节点发现接口。"""

    def discover(self) -> list[PeerInfo]:
        """发现可用 peer。"""
        ...

    def register_self(self, peer_id: str, port: int) -> None:
        """注册自己到发现网络。"""
        ...


class SeedDiscovery:
    """种子节点发现：从静态配置的种子列表发现 peer。

    对每个种子（host:port 格式）发起 HTTP GET /peer/info 探测：
    - 响应 200 且为 JSON → 解析为 PeerInfo，标记 online=True
    - 不可达 / 非 200 / JSON 解析失败 → 跳过该种子

    种子模式下 register_self 为 no-op（种子是静态配置，无需主动注册）。
    """

    def __init__(self, seeds: list[str], timeout: int = 5) -> None:
        self._seeds = list(seeds)
        self._timeout = timeout

    def discover(self) -> list[PeerInfo]:
        """探测所有种子节点，返回在线的 PeerInfo 列表。"""
        peers: list[PeerInfo] = []
        for seed in self._seeds:
            peer = self._probe_seed(seed)
            if peer is not None:
                peers.append(peer)
        return peers

    def register_self(self, peer_id: str, port: int) -> None:
        """种子模式下不需要主动注册（种子是静态配置）。"""
        return None

    def _probe_seed(self, seed: str) -> PeerInfo | None:
        """探测单个种子节点，失败返回 None。"""
        url = f"http://{seed}{SEED_PEER_INFO_PATH}"
        try:
            resp = httpx.get(url, timeout=self._timeout)
        except httpx.HTTPError as exc:
            logger.debug("seed %s 探测失败：%s", seed, exc)
            return None
        if resp.status_code != 200:
            logger.debug("seed %s 返回非 200：%s", seed, resp.status_code)
            return None
        try:
            data = resp.json()
        except ValueError as exc:
            logger.debug("seed %s 响应非 JSON：%s", seed, exc)
            return None
        peer_id = data.get("peer_id") or seed
        return PeerInfo(
            peer_id=peer_id,
            agent_id=data.get("agent_id", ""),
            endpoint=seed,
            online=True,
            last_seen=datetime.now(timezone.utc).isoformat(),
            capabilities=list(data.get("capabilities", [])),
        )


# ----------------------------------------------------------------------
# mDNS 发现（zeroconf 为可选依赖）
# ----------------------------------------------------------------------

try:
    from zeroconf import ServiceBrowser, ServiceInfo, Zeroconf

    _ZEROCONF_AVAILABLE = True
except ImportError:
    _ZEROCONF_AVAILABLE = False


if _ZEROCONF_AVAILABLE:

    class _MDnsServiceListener:
        """zeroconf ServiceBrowser 监听器：收集发现的服务。"""

        def __init__(self) -> None:
            self._services: list[ServiceInfo] = []

        def add_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
            info = zeroconf.get_service_info(service_type, name)
            if info is not None:
                self._services.append(info)

        def remove_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
            pass

        def update_service(self, zeroconf: Zeroconf, service_type: str, name: str) -> None:
            info = zeroconf.get_service_info(service_type, name)
            if info is not None:
                self._services.append(info)

        def to_peer_infos(self) -> list[PeerInfo]:
            """将收集的 ServiceInfo 转为 PeerInfo 列表。"""
            peers: list[PeerInfo] = []
            for info in self._services:
                peer_id = ""
                if info.properties:
                    raw = info.properties.get(b"peer_id")
                    if raw is not None:
                        peer_id = raw.decode("utf-8", errors="replace")
                if not peer_id:
                    if info.name.endswith(MDNS_SERVICE_TYPE):
                        peer_id = info.name[: -len(MDNS_SERVICE_TYPE)]
                    else:
                        peer_id = info.name
                endpoint = ""
                if info.addresses:
                    try:
                        endpoint = f"{socket.inet_ntoa(info.addresses[0])}:{info.port}"
                    except OSError:
                        endpoint = f":{info.port}"
                peers.append(
                    PeerInfo(
                        peer_id=peer_id,
                        endpoint=endpoint,
                        online=True,
                        last_seen=datetime.now(timezone.utc).isoformat(),
                    )
                )
            return peers

    class MDnsDiscovery:
        """mDNS 局域网发现（需要 zeroconf 库）。

        服务名：_teamharness._tcp.local.
        - register_self(peer_id, port) → 注册 Zeroconf 服务
        - discover() → 浏览局域网内所有 _teamharness._tcp.local. 服务
        """

        SERVICE_TYPE = MDNS_SERVICE_TYPE

        def __init__(self, scan_timeout: float = 1.0) -> None:
            self._scan_timeout = scan_timeout
            self._zeroconf: Zeroconf | None = None
            self._registered_info: ServiceInfo | None = None

        def discover(self) -> list[PeerInfo]:
            """浏览局域网内的 _teamharness._tcp.local. 服务。"""
            if self._zeroconf is None:
                return []
            listener = _MDnsServiceListener()
            browser = ServiceBrowser(self._zeroconf, self.SERVICE_TYPE, listener)
            time.sleep(self._scan_timeout)
            browser.cancel()
            return listener.to_peer_infos()

        def register_self(self, peer_id: str, port: int) -> None:
            """注册本节点到 mDNS 网络。"""
            if self._zeroconf is None:
                self._zeroconf = Zeroconf()
            info = ServiceInfo(
                type_=self.SERVICE_TYPE,
                name=f"{peer_id}.{self.SERVICE_TYPE}",
                port=port,
                properties={"peer_id": peer_id.encode("utf-8")},
                addresses=[socket.inet_aton("0.0.0.0")],
            )
            self._zeroconf.register_service(info)
            self._registered_info = info

else:

    class MDnsDiscovery:  # type: ignore[no-redef]
        """Stub：zeroconf 未安装时的降级实现（所有方法 no-op）。"""

        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def discover(self) -> list[PeerInfo]:
            return []

        def register_self(self, peer_id: str, port: int) -> None:
            return None


class CompositeDiscovery:
    """组合发现：合并多个 PeerDiscovery 来源。

    - discover() → 合并所有子 discovery 的 PeerInfo，按 peer_id 去重
    - register_self() → 调用所有子 discovery 的 register_self
    """

    def __init__(self, discoveries: list[PeerDiscovery]) -> None:
        self._discoveries = list(discoveries)

    def discover(self) -> list[PeerInfo]:
        """合并所有来源的 PeerInfo，按 peer_id 去重（后出现的覆盖先出现的）。"""
        seen: dict[str, PeerInfo] = {}
        for discovery in self._discoveries:
            for peer in discovery.discover():
                seen[peer.peer_id] = peer
        return list(seen.values())

    def register_self(self, peer_id: str, port: int) -> None:
        """调用所有子 discovery 的 register_self。"""
        for discovery in self._discoveries:
            discovery.register_self(peer_id, port)


__all__ = [
    "CompositeDiscovery",
    "MDNS_SERVICE_TYPE",
    "MDnsDiscovery",
    "PeerDiscovery",
    "SeedDiscovery",
]
