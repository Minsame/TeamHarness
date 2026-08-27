"""客户端领域包：本地记忆文件夹读写 + git 同步封装 + 召回客户端 + CLI + 守护进程。

对应技术方案 3.5：
- 本地记忆文件夹读写（= git working copy 管理）
- git 同步封装（sync: pull --rebase + push / pr / 冲突 diff 辅助）
- mapping.yaml 目录映射（两层适配模型）
- 召回客户端（调 /v1/recall/* API / 离线降级本地文件）
- module_path 上下文推断（从 coding 软件路径反查 mapping.yaml）
- CLI 6 命令（sync/pr/recall/category-suggest/cost-estimate/index-reconcile）
- 守护进程（定时一级提炼调度 / 网络检测 / 离线召回代理 / 采纳率批量上报）
- 私有资产隔离（.teamharness/private/ + .gitignore）
- manifest.json 本地缓存索引（从 INDEX.md + 资产派生）
- 采纳率上报（本地缓存 + 联网时批量 flush）

公共 API：
- ClientCLI：teamharness sync / pr / recall / category-suggest / cost-estimate / index-reconcile
- ClientDaemon：定时一级提炼调度 / 网络状态检测 / 采纳率批量上报
"""

from server.client.config import ClientConfig, load_client_config
from server.client.git_sync import (
    ConflictDiff,
    GitSync,
    GitSyncResult,
    PrCreationResult,
)
from server.client.recall_client import (
    OfflineRecallResult,
    RecallClient,
    RecallListResult,
    RecallReadResult,
    SyncStatusResult,
)
from server.client.working_copy import WorkingCopy

__all__ = [
    "ClientConfig",
    "ClientCLI",  # 在 cli.py 中实现，下方延迟导入
    "ClientDaemon",  # 在 daemon.py 中实现，下方延迟导入
    "ConflictDiff",
    "GitSync",
    "GitSyncResult",
    "PrCreationResult",
    "OfflineRecallResult",
    "RecallClient",
    "RecallListResult",
    "RecallReadResult",
    "SyncStatusResult",
    "WorkingCopy",
    "load_client_config",
]


def __getattr__(name: str):  # PEP 562 延迟导入，避免循环依赖
    if name == "ClientCLI":
        from server.client.cli import ClientCLI
        return ClientCLI
    if name == "ClientDaemon":
        from server.client.daemon import ClientDaemon
        return ClientDaemon
    raise AttributeError(f"module 'server.client' has no attribute {name!r}")
