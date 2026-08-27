"""TeamHarness 公共类型定义包。

提供跨模块复用的数据模型（Asset / DiffEntry / TreeEntry / CommitRef 等）。
所有领域模块（infra_git / infra_db / recall / binding ...）共享这些类型，
避免重复定义与循环依赖。
"""

from server.common.models import (
    Asset,
    AssetType,
    CommitRef,
    DiffEntry,
    DiffStatus,
    INDEXLevel,
    PRRef,
    PRStatus,
    Scope,
    TreeEntry,
    TreeEntryType,
    WebhookEvent,
)

__all__ = [
    "Asset",
    "AssetType",
    "CommitRef",
    "DiffEntry",
    "DiffStatus",
    "INDEXLevel",
    "PRRef",
    "PRStatus",
    "Scope",
    "TreeEntry",
    "TreeEntryType",
    "WebhookEvent",
]
