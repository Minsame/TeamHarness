"""TeamHarness 公共数据模型。

所有领域模块共享的值对象与枚举。纯数据类，不含业务逻辑，
领域服务通过组合这些类型构建自身能力。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any


class AssetType(str, Enum):
    """资产类型，对应技术方案 3.1.1。"""

    RULE = "rule"
    MEMORY = "memory"
    SKILL = "skill"
    TOOL = "tool"
    PROMPT = "prompt"


class Scope(str, Enum):
    """资产可见性范围，对应技术方案 8.1。"""

    PRIVATE = "private"
    TEAM = "team"
    RESTRICTED = "restricted"
    PUBLIC = "public"


class DiffStatus(str, Enum):
    """文件变更状态。"""

    ADDED = "added"
    MODIFIED = "modified"
    DELETED = "deleted"
    RENAMED = "renamed"


class TreeEntryType(str, Enum):
    """git 树条目类型。"""

    BLOB = "blob"
    TREE = "tree"


class INDEXLevel(str, Enum):
    """INDEX.md 层级，对应技术方案 3.1.4。"""

    PROJECT = "project"
    MODULE = "module"
    SUBMODULE = "submodule"


class PRStatus(str, Enum):
    """Pull Request 状态。"""

    OPEN = "open"
    MERGED = "merged"
    CLOSED = "closed"


@dataclass
class Asset:
    """资产值对象，对应技术方案 3.1.2 schema。

    `content` 为文本内容；`content_file_ref` 用于 file_ref 类型资产。
    `module_path` 为组织层级路径，根级为空字符串。
    """

    id: str
    type: AssetType
    owner: str
    scope: Scope
    content: str = ""
    content_hash: str | None = None
    embedding_id: str | None = None
    tags: list[str] = field(default_factory=list)
    version: str = "0.0.1"
    module_path: str = ""
    category: str | None = None
    related_to: list[str] = field(default_factory=list)
    content_file_ref: str | None = None
    schema_version: int = 1
    created_at: datetime | None = None
    updated_at: datetime | None = None


@dataclass
class DiffEntry:
    """git diff 单条变更。"""

    path: str
    status: DiffStatus
    old_path: str | None = None
    new_path: str | None = None
    old_sha: str | None = None
    new_sha: str | None = None
    additions: int = 0
    deletions: int = 0
    patch: str | None = None


@dataclass
class TreeEntry:
    """git 树条目，对应 ls_tree 返回项。"""

    path: str
    type: TreeEntryType
    sha: str
    size: int = 0
    mode: str = ""


@dataclass
class CommitRef:
    """commit 引用。"""

    sha: str
    message: str
    author: str
    authored_at: datetime
    committer: str | None = None
    committed_at: datetime | None = None
    parents: list[str] = field(default_factory=list)


@dataclass
class PRRef:
    """Pull Request 引用。"""

    id: int
    branch: str
    target: str
    title: str
    url: str | None = None
    status: PRStatus = PRStatus.OPEN


@dataclass
class WebhookEvent:
    """webhook 标准化事件，屏蔽 GitLab/Gitea 差异。"""

    provider: str
    event_type: str
    repo: str
    before: str
    after: str
    ref: str
    raw: dict[str, Any] = field(default_factory=dict)
