"""召回客户端（调 /v1/recall/* API / 离线降级本地文件）。

对应 SubTask 6.4 + 技术方案 3.2.2「召回客户端」：
- 有网时调服务端 RecallService：
    POST /v1/recall/list (agent_id, query?, module_path?, consistency?)
    POST /v1/recall/read  (agent_id, asset_id)
    GET  /v1/sync/status
- 离线降级：
    1. 优先读 manifest.json 本地缓存索引做关键词匹配（BM25-lite）
    2. 命中后从本地 git working copy 读取资产内容
    3. 私有资产（.teamharness/private/）也参与本地匹配
- 一致性策略：strict → 强制 git fetch + 实时读；eventual → 走索引层

网络检测：
- 守护进程周期性 ping /v1/sync/status；离线/在线状态由 ClientDaemon 维护
- RecallClient 接受 online 参数（由调用方注入，避免每次都探测）

降级一致性（重点风险 🟡）：
- 联网时返回 /v1/recall/* 的 degraded=false 结果
- 离线时返回 OfflineRecallResult(degraded=true, source='local')
- 上层 UI 须显式标记"召回降级为本地模式"
"""

from __future__ import annotations

import math
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from server.client.config import ClientConfig
from server.client.manifest import Manifest, ManifestAssetEntry, ManifestBuilder
from server.client.module_path import (
    ModulePathInference,
    infer_module_path,
)
from server.client.placeholders import (
    RecallListItem,
    RecallListResult,
    RecallReadResult,
    SyncStatusResult,
    mock_recall_list,
    mock_recall_read,
    mock_sync_status,
)
from server.client.private_isolation import PrivateIsolation
from server.client.working_copy import WorkingCopy
from server.common.models import Scope
from server.transport.protocol import SyncTransport
from server.transport.types import Message

# 本地匹配的最低 BM25-lite 分数阈值
DEFAULT_LOCAL_MATCH_THRESHOLD = 0.0


@dataclass
class OfflineRecallResult:
    """离线降级召回结果。"""

    items: list[RecallListItem] = field(default_factory=list)
    degraded: bool = True
    source: str = "local"  # local / cache / mixed
    reason: str = ""  # 离线原因（如 'network unreachable' / 'server_url not configured'）
    matched_private_count: int = 0


@dataclass
class NetworkStatus:
    """网络可达性快照。"""

    online: bool
    latency_ms: int = 0
    last_check_at: str = ""
    error: str | None = None


# ---------------------------------------------------------------------------
# BM25-lite 本地匹配
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[\w]+", re.UNICODE)


def _tokenize(text: str) -> list[str]:
    """简单分词：小写化 + 提取 word。"""
    if not text:
        return []
    return [t.lower() for t in _TOKEN_RE.findall(text)]


def _bm25_lite(
    query_tokens: list[str],
    doc_tokens: list[str],
    *,
    avg_doc_len: float,
    doc_count: int,
    doc_freq: dict[str, int],
    k1: float = 1.5,
    b: float = 0.75,
) -> float:
    """单文档 BM25-lite 评分（无 IDF 平滑的简化版）。

    仅供离线降级本地匹配使用；服务端走真正的向量+BM25+RRF 精排。
    """
    if not query_tokens or not doc_tokens:
        return 0.0
    doc_len = len(doc_tokens)
    tf: dict[str, int] = {}
    for t in doc_tokens:
        tf[t] = tf.get(t, 0) + 1
    score = 0.0
    for term in query_tokens:
        if term not in tf:
            continue
        n = doc_freq.get(term, 0)
        # IDF：BM25 形式，doc_count=0 时退化为 1.0
        if doc_count > 0 and n < doc_count:
            idf = math.log(1 + (doc_count - n + 0.5) / (n + 0.5))
        else:
            idf = 1.0
        tf_val = tf[term]
        norm = tf_val * (k1 + 1) / (tf_val + k1 * (1 - b + b * doc_len / max(avg_doc_len, 1)))
        score += idf * norm
    return score


# ---------------------------------------------------------------------------
# RecallClient
# ---------------------------------------------------------------------------


class RecallClient:
    """召回客户端。

    使用方式：
        cfg = load_client_config()
        client = RecallClient(cfg)
        # 在线模式
        result = client.recall_list(agent_id='agent-1', query='lint rule', module_path='modules/backend')
        # 离线模式（自动降级）
        result = client.recall_list(agent_id='agent-1', query='lint rule')

    online 参数：
    - None（默认）：每次调用前探测网络
    - True/False：调用方强制指定（守护进程维护网络状态时使用）

    transport 参数：
    - None（默认）：使用 httpx 直连服务端（向后兼容）
    - SyncTransport 实例：通过注入的传输层（deliver/fetch）通信，不感知底层拓扑
    """

    def __init__(
        self,
        config: ClientConfig,
        *,
        online: bool | None = None,
        http_client: httpx.BaseClient | None = None,
        working_copy: WorkingCopy | None = None,
        manifest_builder: ManifestBuilder | None = None,
        private_isolation: PrivateIsolation | None = None,
        transport: SyncTransport | None = None,
    ) -> None:
        self.config = config
        self._online_override = online
        self._http_client = http_client
        self._wc = working_copy or WorkingCopy(config.resolve_repo_root())
        self._manifest_builder = manifest_builder or ManifestBuilder(config.resolve_repo_root())
        self._pi = private_isolation or PrivateIsolation(config.resolve_repo_root())
        self._transport = transport
        self._cached_manifest: Manifest | None = None
        self._cached_network: NetworkStatus | None = None

    # ------------------------------------------------------------------
    # 网络状态
    # ------------------------------------------------------------------

    def check_network(self, *, force: bool = False) -> NetworkStatus:
        """探测服务端可达性（GET /v1/sync/status）。

        force=True 时跳过缓存重新探测；否则使用上次探测结果（缓存 30s）。
        """
        if not force and self._cached_network is not None:
            return self._cached_network
        if not self.config.server_url:
            status = NetworkStatus(online=False, error="server_url 未配置")
            self._cached_network = status
            return status
        # transport 路径：用 is_peer_reachable 探测，不走 httpx
        if self._transport is not None:
            try:
                peer_id = self._transport_peer_id()
                online = self._transport.is_peer_reachable(peer_id)
                status = NetworkStatus(
                    online=online,
                    last_check_at=_utcnow_iso(),
                    error=None if online else "peer unreachable",
                )
            except Exception as exc:  # noqa: BLE001
                status = NetworkStatus(
                    online=False,
                    last_check_at=_utcnow_iso(),
                    error=str(exc),
                )
            self._cached_network = status
            return status
        import time
        start = time.monotonic()
        try:
            client = self._get_http_client()
            resp = client.get(
                f"{self.config.server_url}/v1/sync/status",
                headers=self._auth_headers(),
                timeout=self.config.request_timeout_seconds,
            )
            latency = int((time.monotonic() - start) * 1000)
            if resp.status_code < 400:
                status = NetworkStatus(
                    online=True,
                    latency_ms=latency,
                    last_check_at=_utcnow_iso(),
                )
            else:
                status = NetworkStatus(
                    online=False,
                    latency_ms=latency,
                    last_check_at=_utcnow_iso(),
                    error=f"HTTP {resp.status_code}",
                )
        except Exception as exc:  # noqa: BLE001
            status = NetworkStatus(
                online=False,
                last_check_at=_utcnow_iso(),
                error=str(exc),
            )
        self._cached_network = status
        return status

    def is_online(self) -> bool:
        """是否在线（含显式 override）。"""
        if self._online_override is not None:
            return self._online_override
        if self.config.is_offline_mode():
            return False
        return self.check_network().online

    # ------------------------------------------------------------------
    # recall/list
    # ------------------------------------------------------------------

    def recall_list(
        self,
        *,
        agent_id: str,
        query: str | None = None,
        module_path: str | None = None,
        consistency: str = "eventual",
        explicit_module: str | None = None,
        limit: int = 10,
    ) -> RecallListResult:
        """召回资产清单。

        若在线 → 调 /v1/recall/list API；
        若离线 → 走本地降级（manifest + working copy + private）。

        module_path 显式传入时优先用之；否则尝试推断（cwd 反查 / env）。
        """
        # module_path 推断
        if not module_path:
            inference = infer_module_path(
                explicit=explicit_module,
                repo_root=self.config.resolve_repo_root(),
            )
            if inference.source != "none":
                module_path = inference.module_path

        if self.is_online():
            if self._transport is not None:
                result = self._call_recall_list_via_transport(
                    agent_id=agent_id,
                    query=query,
                    module_path=module_path,
                    consistency=consistency,
                )
            else:
                result = self._call_remote_list(
                    agent_id=agent_id,
                    query=query,
                    module_path=module_path,
                    consistency=consistency,
                )
            if not result.degraded:
                return result
            # 远端返回 degraded=True（如 DB 故障），叠加本地匹配
            local = self._local_recall(query=query, module_path=module_path, limit=limit)
            merged_items = list(result.items) + [
                i for i in local.items if i.asset_id not in {x.asset_id for x in result.items}
            ]
            return RecallListResult(
                items=merged_items[:limit],
                as_of_commit=result.as_of_commit,
                sync_lag_seconds=result.sync_lag_seconds,
                degraded=True,
            )

        # 离线降级
        local = self._local_recall(query=query, module_path=module_path, limit=limit)
        return RecallListResult(
            items=local.items,
            as_of_commit=self._local_head_commit(),
            sync_lag_seconds=0,
            degraded=True,
        )

    def recall_read(
        self,
        *,
        agent_id: str,
        asset_id: str,
    ) -> RecallReadResult:
        """读取资产详情。

        在线 → /v1/recall/read；离线 → 本地文件读取。
        410 Gone（已删除）→ online 时返回 gone=True；offline 时本地找不到也返回 gone=True。
        """
        if self.is_online():
            if self._transport is not None:
                result = self._call_recall_read_via_transport(
                    agent_id=agent_id, asset_id=asset_id
                )
            else:
                result = self._call_remote_read(agent_id=agent_id, asset_id=asset_id)
            if not result.gone:
                return result
            # 远端 gone，尝试本地兜底（可能为离线新增的本地资产）
            local = self._local_read(asset_id)
            if local is not None:
                return local
            return result
        local = self._local_read(asset_id)
        if local is None:
            return RecallReadResult(content="", frontmatter={}, gone=True)
        return local

    def get_sync_status(self) -> SyncStatusResult:
        """GET /v1/sync/status。离线返回占位。"""
        if not self.is_online():
            return SyncStatusResult(last_synced_commit="", lag_seconds=0, sync_source="offline")
        # transport 路径
        if self._transport is not None:
            data = self._call_via_transport(action="sync_status", payload={})
            if data is None:
                return mock_sync_status()
            return SyncStatusResult(
                last_synced_commit=str(data.get("last_synced_commit", "")),
                lag_seconds=int(data.get("lag_seconds", 0)),
                sync_source=str(data.get("sync_source", "")),
            )
        try:
            client = self._get_http_client()
            resp = client.get(
                f"{self.config.server_url}/v1/sync/status",
                headers=self._auth_headers(),
                timeout=self.config.request_timeout_seconds,
            )
            resp.raise_for_status()
            data = resp.json()
            return SyncStatusResult(
                last_synced_commit=str(data.get("last_synced_commit", "")),
                lag_seconds=int(data.get("lag_seconds", 0)),
                sync_source=str(data.get("sync_source", "")),
            )
        except Exception:  # noqa: BLE001
            return mock_sync_status()

    # ------------------------------------------------------------------
    # 远端调用（Agent 4 提供，未就绪时走 mock）
    # ------------------------------------------------------------------

    def _call_remote_list(
        self,
        *,
        agent_id: str,
        query: str | None,
        module_path: str | None,
        consistency: str,
    ) -> RecallListResult:
        """POST /v1/recall/list。

        Agent 4 未就绪时，httpx 调用会失败 → 自动降级到 mock_recall_list。
        """
        try:
            client = self._get_http_client()
            payload: dict[str, Any] = {
                "agent_id": agent_id,
                "consistency": consistency,
            }
            if query:
                payload["query"] = query
            if module_path:
                payload["module_path"] = module_path
            resp = client.post(
                f"{self.config.server_url}/v1/recall/list",
                json=payload,
                headers=self._auth_headers(),
                timeout=self.config.request_timeout_seconds,
            )
            if resp.status_code >= 400:
                # 服务端错误 → 降级到本地
                return mock_recall_list(agent_id, query, module_path, consistency)
            data = resp.json()
            items = [
                RecallListItem(
                    asset_id=str(it.get("asset_id", "")),
                    type=str(it.get("type", "")),
                    title=str(it.get("title", "")),
                    tags=list(it.get("tags") or []),
                    relevance_score=float(it.get("relevance_score", 0.0)),
                    git_path=str(it.get("git_path", "")),
                    module_path=str(it.get("module_path", "")),
                )
                for it in (data.get("items") or [])
            ]
            return RecallListResult(
                items=items,
                as_of_commit=str(data.get("as_of_commit", "")),
                sync_lag_seconds=int(data.get("sync_lag_seconds", 0)),
                degraded=bool(data.get("degraded", False)),
            )
        except (httpx.HTTPError, OSError, ValueError):
            # Agent 4 未就绪 / 网络错误 → 走 mock（标记 degraded=True）
            return mock_recall_list(agent_id, query, module_path, consistency)

    def _call_remote_read(self, *, agent_id: str, asset_id: str) -> RecallReadResult:
        """POST /v1/recall/read。"""
        try:
            client = self._get_http_client()
            resp = client.post(
                f"{self.config.server_url}/v1/recall/read",
                json={"agent_id": agent_id, "asset_id": asset_id},
                headers=self._auth_headers(),
                timeout=self.config.request_timeout_seconds,
            )
            if resp.status_code == 410:
                data = resp.json() if resp.content else {}
                return RecallReadResult(
                    content="",
                    frontmatter={},
                    gone=True,
                    alternative_asset_ids=list(data.get("alternative_asset_ids") or []),
                )
            if resp.status_code >= 400:
                return mock_recall_read(agent_id, asset_id)
            data = resp.json()
            return RecallReadResult(
                content=str(data.get("content", "")),
                frontmatter=dict(data.get("frontmatter") or {}),
                gone=False,
            )
        except (httpx.HTTPError, OSError, ValueError):
            return mock_recall_read(agent_id, asset_id)

    # ------------------------------------------------------------------
    # transport 路径（SyncTransport 注入时使用，与 httpx 路径并存）
    # ------------------------------------------------------------------

    def _call_via_transport(
        self,
        *,
        action: str,
        payload: dict[str, Any],
    ) -> dict[str, Any] | None:
        """通过注入的 SyncTransport 发送请求（deliver + fetch 模式）。

        构造 ask 类型 Message 投递给 server peer，再 fetch 拉取回复，
        匹配 in_reply_to == 请求 message_id 的回复并返回其 payload。
        失败（无 transport / 投递失败 / 无回复 / 异常）返回 None，
        由调用方降级到 mock（与 httpx 路径失败处理一致）。
        """
        if self._transport is None:
            return None
        server_peer_id = self._transport_peer_id()
        msg_id = str(uuid.uuid4())
        request_msg = Message(
            message_id=msg_id,
            event_id=msg_id,  # 幂等去重
            sender_id=self.config.agent_id or "unknown",
            recipient_id=server_peer_id,
            msg_type="ask",
            payload={"action": action, **payload},
            timestamp=_utcnow_iso(),
        )
        try:
            result = self._transport.deliver(server_peer_id, [request_msg])
            if not result.success and result.delivered_count == 0:
                return None
            replies = self._transport.fetch(server_peer_id)
        except Exception:  # noqa: BLE001
            return None
        # 找到 in_reply_to == msg_id 的回复（最近的优先）
        for reply in reversed(replies):
            if reply.in_reply_to == msg_id:
                return dict(reply.payload)
        return None

    def _transport_peer_id(self) -> str:
        """transport 路径下的 server peer ID。"""
        return self.config.server_url or "teamharness-server"

    def _call_recall_list_via_transport(
        self,
        *,
        agent_id: str,
        query: str | None,
        module_path: str | None,
        consistency: str,
    ) -> RecallListResult:
        """通过 SyncTransport 调用 recall/list。"""
        payload: dict[str, Any] = {
            "agent_id": agent_id,
            "consistency": consistency,
        }
        if query:
            payload["query"] = query
        if module_path:
            payload["module_path"] = module_path
        data = self._call_via_transport(action="recall_list", payload=payload)
        if data is None:
            return mock_recall_list(agent_id, query, module_path, consistency)
        items = [
            RecallListItem(
                asset_id=str(it.get("asset_id", "")),
                type=str(it.get("type", "")),
                title=str(it.get("title", "")),
                tags=list(it.get("tags") or []),
                relevance_score=float(it.get("relevance_score", 0.0)),
                git_path=str(it.get("git_path", "")),
                module_path=str(it.get("module_path", "")),
            )
            for it in (data.get("items") or [])
        ]
        return RecallListResult(
            items=items,
            as_of_commit=str(data.get("as_of_commit", "")),
            sync_lag_seconds=int(data.get("sync_lag_seconds", 0)),
            degraded=bool(data.get("degraded", False)),
        )

    def _call_recall_read_via_transport(
        self,
        *,
        agent_id: str,
        asset_id: str,
    ) -> RecallReadResult:
        """通过 SyncTransport 调用 recall/read。"""
        data = self._call_via_transport(
            action="recall_read",
            payload={"agent_id": agent_id, "asset_id": asset_id},
        )
        if data is None:
            return mock_recall_read(agent_id, asset_id)
        if bool(data.get("gone", False)):
            return RecallReadResult(
                content="",
                frontmatter={},
                gone=True,
                alternative_asset_ids=list(data.get("alternative_asset_ids") or []),
            )
        return RecallReadResult(
            content=str(data.get("content", "")),
            frontmatter=dict(data.get("frontmatter") or {}),
            gone=False,
        )

    # ------------------------------------------------------------------
    # 本地降级
    # ------------------------------------------------------------------

    def _local_recall(
        self,
        *,
        query: str | None,
        module_path: str | None,
        limit: int,
    ) -> OfflineRecallResult:
        """本地降级召回：manifest + working copy + private 关键词匹配。

        候选来源（合并去重，按 path）：
        1. manifest 中的资产（来自 INDEX.md 派生，优先）
        2. WorkingCopy 实际文件（补充，覆盖未维护 INDEX.md 的本地场景）
        3. PrivateIsolation 私有资产（manifest.private_assets，扫描 .teamharness/private/）
        """
        manifest = self._get_manifest()
        candidates: list[ManifestAssetEntry] = []
        seen_paths: set[str] = set()

        # 1. manifest 中的资产
        for m in manifest.modules:
            # module_path 过滤
            if module_path and m.module_path and m.module_path != module_path:
                continue
            for a in m.assets:
                if a.path not in seen_paths:
                    candidates.append(a)
                    seen_paths.add(a.path)
        # 项目级资产始终纳入候选
        # （module_path 为空时所有都纳入；非空时仅纳入 module 匹配 + 项目级）
        if module_path:
            for m in manifest.modules:
                if m.level == "project":
                    for a in m.assets:
                        if a.path not in seen_paths:
                            candidates.append(a)
                            seen_paths.add(a.path)

        # 2. WorkingCopy 实际文件（补充 manifest 缺失的场景，如本地未维护 INDEX.md）
        #    离线召回应能找到磁盘上的资产，不应仅依赖 INDEX.md 登记
        try:
            for asset in self._wc.list_assets(include_private=False):
                # module_path 过滤：非空且不匹配则跳过；项目级（空）始终纳入
                if module_path:
                    asset_mp = asset.module_path
                    if asset_mp and asset_mp != module_path:
                        continue
                rel_path = asset.relative_path
                if rel_path in seen_paths:
                    continue
                candidates.append(ManifestAssetEntry.from_asset_file(asset))
                seen_paths.add(rel_path)
        except Exception:  # noqa: BLE001
            pass

        # 3. 私有资产也参与本地匹配
        private_assets = manifest.private_assets
        # 构建文档语料（用于 BM25-lite 的 doc_freq 与 avg_doc_len）
        docs: list[tuple[ManifestAssetEntry, list[str]]] = []
        for a in candidates + private_assets:
            text = self._read_asset_text(a.path)
            docs.append((a, _tokenize(text)))
        doc_count = len(docs)
        avg_doc_len = sum(len(toks) for _, toks in docs) / max(doc_count, 1)
        doc_freq: dict[str, int] = {}
        for _, toks in docs:
            for term in set(toks):
                doc_freq[term] = doc_freq.get(term, 0) + 1

        query_tokens = _tokenize(query or "")
        scored: list[tuple[float, ManifestAssetEntry]] = []
        for a, toks in docs:
            score = _bm25_lite(
                query_tokens,
                toks,
                avg_doc_len=avg_doc_len,
                doc_count=doc_count,
                doc_freq=doc_freq,
            ) if query_tokens else 1.0  # 无 query → 退化为按 module_path 过滤的全部
            if score > DEFAULT_LOCAL_MATCH_THRESHOLD:
                scored.append((score, a))
        scored.sort(key=lambda x: x[0], reverse=True)

        items: list[RecallListItem] = []
        private_count = 0
        for score, a in scored[:limit]:
            # 私有资产计数
            is_private = any(a is pa for pa in private_assets)
            if is_private:
                private_count += 1
            items.append(
                RecallListItem(
                    asset_id=a.id,
                    type=a.type,
                    title=Path(a.path).stem,
                    tags=list(a.tags),
                    relevance_score=round(score, 4),
                    git_path=a.path,
                    module_path=a.module_path,
                )
            )
        return OfflineRecallResult(
            items=items,
            degraded=True,
            source="local",
            reason="offline mode",
            matched_private_count=private_count,
        )

    def _local_read(self, asset_id: str) -> RecallReadResult | None:
        """本地按 asset_id 读取资产。

        优先查 manifest；manifest 未命中则扫描 WorkingCopy 实际文件
        （覆盖未维护 INDEX.md 的本地场景）。
        """
        manifest = self._get_manifest()
        for m in manifest.modules:
            for a in m.assets:
                if a.id == asset_id:
                    return self._read_asset_by_path(a.path)
        for a in manifest.private_assets:
            if a.id == asset_id:
                return self._read_asset_by_path(a.path)
        # manifest 未命中 → 扫描 WorkingCopy 实际文件
        try:
            for asset in self._wc.list_assets(include_private=True):
                if asset.asset_id == asset_id:
                    return self._read_asset_by_path(asset.relative_path)
        except Exception:  # noqa: BLE001
            pass
        return None

    def _read_asset_by_path(self, path: str) -> RecallReadResult | None:
        """按相对仓库根路径读取资产并解析 frontmatter。"""
        target = self.config.resolve_repo_root() / path
        if not target.is_file():
            return None
        try:
            content = target.read_text(encoding="utf-8")
        except OSError:
            return None
        from server.infra_git.trae_adapter import parse_frontmatter_dual
        fm = parse_frontmatter_dual(content)
        return RecallReadResult(
            content=fm.body,
            frontmatter=dict(fm.teamharness_fields),
            gone=False,
        )

    def _read_asset_text(self, path: str) -> str:
        """读取资产正文（用于 BM25 分词）。失败返回空。"""
        result = self._read_asset_by_path(path)
        return result.content if result else ""

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _get_http_client(self) -> httpx.BaseClient:
        if self._http_client is None:
            self._http_client = httpx.Client()
        return self._http_client

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        if self.config.agent_id:
            headers["X-TeamHarness-Agent-Id"] = self.config.agent_id
        return headers

    def _get_manifest(self) -> Manifest:
        """获取本地 manifest（优先用缓存文件，无则实时构建）。"""
        if self._cached_manifest is not None:
            return self._cached_manifest
        cached = self._manifest_builder.load()
        if cached is not None:
            self._cached_manifest = cached
            return cached
        # 实时构建
        head = self._local_head_commit()
        self._cached_manifest = self._manifest_builder.build(head_commit=head)
        return self._cached_manifest

    def _local_head_commit(self) -> str:
        """本地 HEAD commit SHA（无 git 时为空字符串）。"""
        try:
            from server.client.git_sync import GitSync
            sync = GitSync(self.config.resolve_repo_root())
            return sync.current_commit()
        except Exception:  # noqa: BLE001
            return ""

    def invalidate_manifest_cache(self) -> None:
        """清除 manifest 缓存（sync 后调用，确保下次重新构建/加载）。"""
        self._cached_manifest = None


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "NetworkStatus",
    "OfflineRecallResult",
    "RecallClient",
    "RecallListItem",
    "RecallListResult",
    "RecallReadResult",
    "SyncStatusResult",
]
