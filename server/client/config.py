"""客户端配置管理。

对应技术方案 3.5.1 职责 7：服务端地址、API Key、本地路径、mapping.yaml、同步策略。

配置来源优先级：
1. 显式参数（构造 ClientConfig 时传入）
2. 环境变量（TEAMHARNESS_*）
3. 配置文件（.teamharness/config.yaml，存在则加载）
4. 内置默认值

配置项：
- server_url：TeamHarness 服务端基址（如 https://th.example.com）
- api_key：成员 API Key（用于鉴权 /v1/recall/*、/v1/metrics 等）
- agent_id：当前 Agent 标识（API Key 反查得到，可缺省由服务端解析）
- member_id：成员标识
- repo_root：本地记忆文件夹根（= git working copy 根）
- mapping_path：mapping.yaml 路径（默认 .teamharness/mapping.yaml）
- manifest_path：manifest.json 路径（默认 .teamharness/manifest.json）
- private_dir：私有资产目录（默认 .teamharness/private/）
- sync_strategy：同步策略（manual / auto）
- personal_branch：个人分支名（默认 members/<member_id>）
- target_branch：PR 目标分支（默认 main）
- distill_schedule_cron：一级提炼调度 cron（默认每日 02:00）
- network_check_interval_seconds：网络检测周期（默认 60s）
- adoption_flush_interval_seconds：采纳率批量上报周期（默认 300s）
- request_timeout_seconds：HTTP 请求超时（默认 15s）
- offline_recall_local_only：离线时是否仅本地检索（默认 True）
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from server.transport.protocol import (
    TOPOLOGY_CENTRAL,
    VALID_TOPOLOGIES,
)

# .teamharness/ 目录名（与 categories.py 对齐）
TEAMHARNESS_DIR = ".teamharness"
CONFIG_FILENAME = "config.yaml"
MAPPING_FILENAME = "mapping.yaml"
MANIFEST_FILENAME = "manifest.json"
PRIVATE_DIRNAME = "private"

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 默认值
# ---------------------------------------------------------------------------


DEFAULT_SYNC_STRATEGY = "manual"
DEFAULT_PERSONAL_BRANCH_TEMPLATE = "members/{member_id}"
DEFAULT_TARGET_BRANCH = "main"
# 每日 02:00 调度一级提炼
DEFAULT_DISTILL_CRON = "0 2 * * *"
DEFAULT_NETWORK_CHECK_INTERVAL = 60
DEFAULT_ADOPTION_FLUSH_INTERVAL = 300
DEFAULT_REQUEST_TIMEOUT = 15
DEFAULT_OFFLINE_LOCAL_ONLY = True

# 通信拓扑默认值
DEFAULT_TOPOLOGY = TOPOLOGY_CENTRAL
DEFAULT_DISCOVERY = "seed"
# async_comm 子配置默认值
DEFAULT_ASYNC_COMM: dict[str, Any] = {
    "snapshot_policy": "on_demand",
    "snapshot_ttl_days": 30,
    "conflict_threshold": 0.3,
    "auto_confirm_threshold": 0.8,
    "realtime_session_timeout": 600,  # Task 27：实时会话超时（秒）
}


@dataclass
class ClientConfig:
    """客户端配置值对象。

    所有路径字段均为字符串形式，由调用方按需转 Path；repo_root 为空时
    默认使用当前工作目录。
    """

    server_url: str = ""
    api_key: str = ""
    agent_id: str = ""
    member_id: str = ""
    repo_root: str = ""
    mapping_path: str = ""
    manifest_path: str = ""
    private_dir: str = ""
    sync_strategy: str = DEFAULT_SYNC_STRATEGY
    personal_branch: str = ""
    target_branch: str = DEFAULT_TARGET_BRANCH
    distill_schedule_cron: str = DEFAULT_DISTILL_CRON
    network_check_interval_seconds: int = DEFAULT_NETWORK_CHECK_INTERVAL
    adoption_flush_interval_seconds: int = DEFAULT_ADOPTION_FLUSH_INTERVAL
    request_timeout_seconds: int = DEFAULT_REQUEST_TIMEOUT
    offline_recall_local_only: bool = DEFAULT_OFFLINE_LOCAL_ONLY
    # 通信拓扑配置
    topology: str = DEFAULT_TOPOLOGY  # central / p2p / hybrid
    # 种子 peer 列表：每项可为字符串（host:port 或 peer_id）或字典
    # {"peer_id": "bob", "endpoint": "host:port", "tags": ["前端", "运维"]}
    # dict 形式用于 P2P 模式下未收到 tags_sync 时的静态降级（Task 25 SubTask 25.6）
    peers: list[str | dict[str, Any]] = field(default_factory=list)
    discovery: str = DEFAULT_DISCOVERY  # mdns / seed / composite
    # 是否为管理员节点（P2P 模式下由 admin 广播 tags_sync，Task 25 SubTask 25.4）
    is_admin: bool = False
    # async_comm 子配置（与 DEFAULT_ASYNC_COMM 合并使用）
    async_comm: dict[str, Any] = field(default_factory=dict)
    # 额外字段（环境变量透传 / 用户自定义）
    extras: dict[str, Any] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 路径解析辅助
    # ------------------------------------------------------------------

    def resolve_repo_root(self) -> Path:
        """返回仓库根 Path，缺省为当前工作目录。"""
        return Path(self.repo_root) if self.repo_root else Path.cwd()

    def resolve_mapping_path(self) -> Path:
        """返回 mapping.yaml 绝对路径。"""
        if self.mapping_path:
            return Path(self.mapping_path)
        return self.resolve_repo_root() / TEAMHARNESS_DIR / MAPPING_FILENAME

    def resolve_manifest_path(self) -> Path:
        """返回 manifest.json 绝对路径。"""
        if self.manifest_path:
            return Path(self.manifest_path)
        return self.resolve_repo_root() / TEAMHARNESS_DIR / MANIFEST_FILENAME

    def resolve_private_dir(self) -> Path:
        """返回私有资产目录绝对路径。"""
        if self.private_dir:
            return Path(self.private_dir)
        return self.resolve_repo_root() / TEAMHARNESS_DIR / PRIVATE_DIRNAME

    def resolve_teamharness_dir(self) -> Path:
        """返回 .teamharness/ 目录绝对路径。"""
        return self.resolve_repo_root() / TEAMHARNESS_DIR

    def resolve_personal_branch(self) -> str:
        """返回个人分支名；未显式配置时按 member_id 模板生成。"""
        if self.personal_branch:
            return self.personal_branch
        if not self.member_id:
            return "members/default"
        return DEFAULT_PERSONAL_BRANCH_TEMPLATE.format(member_id=self.member_id)

    def is_auto_sync(self) -> bool:
        return self.sync_strategy == "auto"

    def resolve_async_comm_config(self) -> dict[str, Any]:
        """返回带默认值的 async_comm 配置（不修改 self.async_comm）。

        用户在 async_comm 中显式设置的键会覆盖默认值；未设置的键使用
        DEFAULT_ASYNC_COMM 中的默认值。
        """
        merged: dict[str, Any] = dict(DEFAULT_ASYNC_COMM)
        if self.async_comm:
            merged.update(self.async_comm)
        return merged

    def is_offline_mode(self) -> bool:
        """是否处于离线模式（无 server_url 或显式 extras['offline']）。"""
        if self.extras.get("offline") is True:
            return True
        return not self.server_url

    def resolve_peer_tags(self) -> dict[str, list[str]]:
        """从静态 peers 配置中解析 peer_id → tags 映射。

        Task 25 SubTask 25.6：P2P 模式下未收到 tags_sync 时的降级路径，
        从 ClientConfig.peers[].tags 静态配置中读取。

        - 字符串形式的 peers（无 tags 信息）会被跳过
        - dict 形式但缺 tags 字段视为空 tags 列表
        - 缺 peer_id 字段的 dict 用 endpoint 作为 key（再缺则跳过）

        Returns:
            {peer_id: [tag1, tag2, ...]} 映射；无任何 dict peer 时返回 {}
        """
        mapping: dict[str, list[str]] = {}
        for item in self.peers:
            if not isinstance(item, dict):
                continue
            peer_id = str(item.get("peer_id") or item.get("endpoint") or "")
            if not peer_id:
                continue
            raw_tags = item.get("tags") or []
            if isinstance(raw_tags, str):
                # 兼容字符串形式（逗号分隔）
                tags = [t.strip() for t in raw_tags.split(",") if t.strip()]
            elif isinstance(raw_tags, list):
                tags = [str(t).strip() for t in raw_tags if str(t).strip()]
            else:
                tags = []
            mapping[peer_id] = tags
        return mapping


# ---------------------------------------------------------------------------
# 加载逻辑
# ---------------------------------------------------------------------------


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.lower() in ("1", "true", "yes", "on")


def _coerce_int(value: Any, default: int) -> int:
    """将任意值（str/int/None）安全转为 int，失败时返回 default。

    用于 load_client_config 中合并多来源（参数/env/文件）的 int 字段，
    避免 env 或文件中配置了非法值（如 'not-a-number'）时抛 ValueError。
    """
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def _coerce_bool(value: Any, default: bool) -> bool:
    """将任意值安全转为 bool。

    字符串 'false'/'0'/'no'/'off' → False；'true'/'1'/'yes'/'on' → True。
    避免 bool('false') 误判为 True（非空字符串）。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.lower() in ("1", "true", "yes", "on")
    try:
        return bool(int(value))
    except (ValueError, TypeError):
        return default


def _validate_topology(topology: Any) -> str:
    """校验 topology 值，无效时回退到 central 并记录 warning。

    用于 load_client_config 在合并多来源（参数/env/文件/overrides）后统一校验，
    保证后续 transport 选型不会因非法拓扑值崩溃。
    """
    value = str(topology) if topology is not None else ""
    if value not in VALID_TOPOLOGIES:
        logger.warning(
            "Invalid topology %r, fallback to 'central'", value or None
        )
        return TOPOLOGY_CENTRAL
    return value


def _parse_peers_env(raw: str) -> list[str]:
    """解析 TEAMHARNESS_PEERS 环境变量（逗号分隔）为 peer 地址列表。

    空字符串返回 []；自动去除每项首尾空白并过滤空项。
    """
    if not raw:
        return []
    return [p.strip() for p in raw.split(",") if p.strip()]


def _load_config_file(repo_root: Path) -> dict[str, Any]:
    """从 .teamharness/config.yaml 加载配置（存在则返回 dict，否则空 dict）。"""
    cfg_path = repo_root / TEAMHARNESS_DIR / CONFIG_FILENAME
    if not cfg_path.is_file():
        return {}
    try:
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except (OSError, yaml.YAMLError):
        return {}
    return data if isinstance(data, dict) else {}


def load_client_config(
    *,
    repo_root: str | Path | None = None,
    server_url: str | None = None,
    api_key: str | None = None,
    agent_id: str | None = None,
    member_id: str | None = None,
    **overrides: Any,
) -> ClientConfig:
    """按优先级合并配置：参数 > 环境变量 > 配置文件 > 默认值。

    overrides 中的任意键将覆盖对应字段（如 sync_strategy='auto'）。
    """
    root = Path(repo_root) if repo_root else Path(_env("TEAMHARNESS_REPO_ROOT", "") or ".")
    file_data = _load_config_file(root)

    def pick(key: str, param_val: Any, env_name: str, default: Any) -> Any:
        if param_val is not None:
            return param_val
        env_val = _env(env_name)
        if env_val:
            return env_val
        if key in file_data:
            return file_data[key]
        return default

    # peers：env TEAMHARNESS_PEERS（逗号分隔，仅字符串形式）> 文件 peers(list, 支持 str|dict) > []
    peers_env = _env("TEAMHARNESS_PEERS")
    if peers_env:
        # env 形式只支持字符串 peer（host:port）
        peers: list[str | dict[str, Any]] = _parse_peers_env(peers_env)
    elif "peers" in file_data and isinstance(file_data["peers"], list):
        # 文件中允许 str 或 dict 形式（dict 含 peer_id / tags 等）
        peers = [
            item if isinstance(item, dict) else str(item).strip()
            for item in file_data["peers"]
            if (isinstance(item, dict) and item) or (isinstance(item, (str, int, float)) and str(item).strip())
        ]
    else:
        peers = []

    # is_admin：env TEAMHARNESS_IS_ADMIN > 文件 is_admin > False
    is_admin_value = pick("is_admin", None, "TEAMHARNESS_IS_ADMIN", False)
    is_admin = _coerce_bool(is_admin_value, False)

    # async_comm：仅从文件加载（必须是 dict），否则空 dict
    async_comm_raw = file_data.get("async_comm")
    async_comm: dict[str, Any] = (
        dict(async_comm_raw) if isinstance(async_comm_raw, dict) else {}
    )

    cfg = ClientConfig(
        server_url=str(pick("server_url", server_url, "TEAMHARNESS_SERVER_URL", "")),
        api_key=str(pick("api_key", api_key, "TEAMHARNESS_API_KEY", "")),
        agent_id=str(pick("agent_id", agent_id, "TEAMHARNESS_AGENT_ID", "")),
        member_id=str(pick("member_id", member_id, "TEAMHARNESS_MEMBER_ID", "")),
        repo_root=str(root),
        sync_strategy=str(
            pick("sync_strategy", None, "TEAMHARNESS_SYNC_STRATEGY", DEFAULT_SYNC_STRATEGY)
        ),
        personal_branch=str(
            pick("personal_branch", None, "TEAMHARNESS_PERSONAL_BRANCH", "")
        ),
        target_branch=str(
            pick("target_branch", None, "TEAMHARNESS_TARGET_BRANCH", DEFAULT_TARGET_BRANCH)
        ),
        distill_schedule_cron=str(
            pick(
                "distill_schedule_cron",
                None,
                "TEAMHARNESS_DISTILL_CRON",
                DEFAULT_DISTILL_CRON,
            )
        ),
        network_check_interval_seconds=_coerce_int(
            pick(
                "network_check_interval_seconds",
                None,
                "TEAMHARNESS_NETWORK_CHECK_INTERVAL",
                DEFAULT_NETWORK_CHECK_INTERVAL,
            ),
            DEFAULT_NETWORK_CHECK_INTERVAL,
        ),
        adoption_flush_interval_seconds=_coerce_int(
            pick(
                "adoption_flush_interval_seconds",
                None,
                "TEAMHARNESS_ADOPTION_FLUSH_INTERVAL",
                DEFAULT_ADOPTION_FLUSH_INTERVAL,
            ),
            DEFAULT_ADOPTION_FLUSH_INTERVAL,
        ),
        request_timeout_seconds=_coerce_int(
            pick(
                "request_timeout_seconds",
                None,
                "TEAMHARNESS_REQUEST_TIMEOUT",
                DEFAULT_REQUEST_TIMEOUT,
            ),
            DEFAULT_REQUEST_TIMEOUT,
        ),
        offline_recall_local_only=_coerce_bool(
            pick(
                "offline_recall_local_only",
                None,
                "TEAMHARNESS_OFFLINE_LOCAL_ONLY",
                DEFAULT_OFFLINE_LOCAL_ONLY,
            ),
            DEFAULT_OFFLINE_LOCAL_ONLY,
        ),
        topology=str(
            pick("topology", None, "TEAMHARNESS_TOPOLOGY", DEFAULT_TOPOLOGY)
        ),
        peers=peers,
        discovery=str(
            pick("discovery", None, "TEAMHARNESS_DISCOVERY", DEFAULT_DISCOVERY)
        ),
        is_admin=is_admin,
        async_comm=async_comm,
    )

    # 应用 overrides
    for k, v in overrides.items():
        if hasattr(cfg, k):
            setattr(cfg, k, v)
        else:
            cfg.extras[k] = v

    # 拓扑校验：合并多来源后统一校验，无效值回退到 central（覆盖 overrides 之后）
    cfg.topology = _validate_topology(cfg.topology)

    return cfg


def save_client_config(cfg: ClientConfig, *, path: Path | None = None) -> Path:
    """将配置写回 .teamharness/config.yaml（敏感字段 api_key 不写入）。"""
    target = path or (cfg.resolve_teamharness_dir() / CONFIG_FILENAME)
    target.parent.mkdir(parents=True, exist_ok=True)
    data: dict[str, Any] = {
        "server_url": cfg.server_url,
        "agent_id": cfg.agent_id,
        "member_id": cfg.member_id,
        "sync_strategy": cfg.sync_strategy,
        "personal_branch": cfg.personal_branch,
        "target_branch": cfg.target_branch,
        "distill_schedule_cron": cfg.distill_schedule_cron,
        "network_check_interval_seconds": cfg.network_check_interval_seconds,
        "adoption_flush_interval_seconds": cfg.adoption_flush_interval_seconds,
        "request_timeout_seconds": cfg.request_timeout_seconds,
        "offline_recall_local_only": cfg.offline_recall_local_only,
        "topology": cfg.topology,
        # peers 保持原始类型（str 或 dict）写回，便于静态 tags 配置持久化
        "peers": list(cfg.peers),
        "discovery": cfg.discovery,
        "is_admin": cfg.is_admin,
        "async_comm": dict(cfg.async_comm),
    }
    if cfg.extras:
        data.update(cfg.extras)
    # api_key 不落盘（避免泄露），由环境变量管理
    target.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
    return target
