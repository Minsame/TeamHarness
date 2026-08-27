"""manifest.json 本地缓存索引（从 INDEX.md + 资产派生）。

对应 SubTask 6.9 + 技术方案 3.4.1 表格：
- manifest.json 为客户端本地缓存索引，不入 git（已在 .gitignore）
- 从仓库 INDEX.md（项目级/模块级/子模块级）+ 实际资产文件 frontmatter 派生
- 用途：
    1. 离线召回降级时本地快速检索（避免每次扫全仓库）
    2. 客户端 UI 列表展示
    3. 同步前后差异比对（哪些资产新增/修改/删除）

manifest.json 结构：
    {
      "version": 1,
      "generated_at": "2026-08-07T10:00:00Z",
      "repo_root": "/abs/path",
      "head_commit": "abc123",
      "modules": [
        {
          "module_path": "",            # 项目级为空字符串
          "level": "project",
          "index_path": "INDEX.md",
          "assets": [
            {
              "id": "rule-global-lint",
              "type": "rule",
              "path": "rules/global-lint.md",
              "owner": "alice",
              "scope": "team",
              "tags": ["lint"],
              "category": "rule-backend",
              "version": "0.0.1",
              "content_hash": "sha256:...",
              "module_path": ""
            }
          ],
          "submodules": ["backend"]
        },
        ...
      ],
      "private_assets": [ ... ],        # 私有资产索引（同上结构）
      "counts": { "assets": 5, "modules": 2 }
    }

派生策略：
- 优先信任 INDEX.md 的 assets 登记（防孤岛）；同时读取实际文件 frontmatter
  补全 id/owner/scope/tags 等字段
- 私有资产单独索引（不入 modules[].assets，放 private_assets 数组）
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.client.config import MANIFEST_FILENAME, TEAMHARNESS_DIR
from server.client.private_isolation import PrivateIsolation
from server.client.working_copy import WorkingCopy
from server.common.models import Scope
from server.infra_git.index_manager import discover_levels
from server.infra_git.trae_adapter import parse_frontmatter_dual

MANIFEST_VERSION = 1


@dataclass
class ManifestAssetEntry:
    """manifest.json 单个资产条目。"""

    id: str
    type: str
    path: str  # 相对仓库根的 POSIX 路径
    owner: str = ""
    scope: str = Scope.PRIVATE.value
    tags: list[str] = field(default_factory=list)
    category: str = ""
    version: str = "0.0.1"
    content_hash: str = ""
    module_path: str = ""

    @classmethod
    def from_asset_file(cls, asset: Any) -> "ManifestAssetEntry":
        """从 working_copy.AssetFile 构造。"""
        return cls(
            id=asset.asset_id or "",
            type=asset.asset_type.value if asset.asset_type else str(asset.frontmatter.get("type", "")),
            path=asset.relative_path,
            owner=asset.owner,
            scope=asset.scope.value,
            tags=list(asset.tags),
            category=str(asset.frontmatter.get("category", "")),
            version=str(asset.frontmatter.get("version", "0.0.1")),
            content_hash=_hash_content(asset.body),
            module_path=asset.module_path,
        )


@dataclass
class ManifestModuleEntry:
    """manifest.json 模块条目。"""

    module_path: str
    level: str  # project / module / submodule
    index_path: str
    assets: list[ManifestAssetEntry] = field(default_factory=list)
    submodules: list[str] = field(default_factory=list)


@dataclass
class Manifest:
    """manifest.json 顶层结构。"""

    version: int = MANIFEST_VERSION
    generated_at: str = ""
    repo_root: str = ""
    head_commit: str = ""
    modules: list[ManifestModuleEntry] = field(default_factory=list)
    private_assets: list[ManifestAssetEntry] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "generated_at": self.generated_at,
            "repo_root": self.repo_root,
            "head_commit": self.head_commit,
            "modules": [
                {
                    "module_path": m.module_path,
                    "level": m.level,
                    "index_path": m.index_path,
                    "assets": [asdict(a) for a in m.assets],
                    "submodules": list(m.submodules),
                }
                for m in self.modules
            ],
            "private_assets": [asdict(a) for a in self.private_assets],
            "counts": dict(self.counts),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Manifest":
        """从 dict 反序列化（容忍字段缺失）。"""
        modules: list[ManifestModuleEntry] = []
        for m in data.get("modules") or []:
            assets = [
                ManifestAssetEntry(
                    id=str(a.get("id", "")),
                    type=str(a.get("type", "")),
                    path=str(a.get("path", "")),
                    owner=str(a.get("owner", "")),
                    scope=str(a.get("scope", Scope.PRIVATE.value)),
                    tags=list(a.get("tags") or []),
                    category=str(a.get("category", "")),
                    version=str(a.get("version", "0.0.1")),
                    content_hash=str(a.get("content_hash", "")),
                    module_path=str(a.get("module_path", "")),
                )
                for a in (m.get("assets") or [])
            ]
            modules.append(
                ManifestModuleEntry(
                    module_path=str(m.get("module_path", "")),
                    level=str(m.get("level", "project")),
                    index_path=str(m.get("index_path", "")),
                    assets=assets,
                    submodules=list(m.get("submodules") or []),
                )
            )
        private_assets = [
            ManifestAssetEntry(
                id=str(a.get("id", "")),
                type=str(a.get("type", "")),
                path=str(a.get("path", "")),
                owner=str(a.get("owner", "")),
                scope=str(a.get("scope", Scope.PRIVATE.value)),
                tags=list(a.get("tags") or []),
                category=str(a.get("category", "")),
                version=str(a.get("version", "0.0.1")),
                content_hash=str(a.get("content_hash", "")),
                module_path=str(a.get("module_path", "")),
            )
            for a in (data.get("private_assets") or [])
        ]
        return cls(
            version=int(data.get("version", MANIFEST_VERSION)),
            generated_at=str(data.get("generated_at", "")),
            repo_root=str(data.get("repo_root", "")),
            head_commit=str(data.get("head_commit", "")),
            modules=modules,
            private_assets=private_assets,
            counts=dict(data.get("counts") or {}),
        )


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _hash_content(body: str) -> str:
    """计算 body 内容的 SHA-256 摘要（用于增量比对）。"""
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()
    return f"sha256:{h}"


def _utcnow_iso() -> str:
    """统一 UTC ISO 时间戳。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# 派生与持久化
# ---------------------------------------------------------------------------


class ManifestBuilder:
    """manifest.json 构建器。

    使用：
        builder = ManifestBuilder(repo_root=Path(...))
        manifest = builder.build(head_commit="abc123")
        builder.save(manifest)
    """

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)
        self.manifest_path = self.repo_root / TEAMHARNESS_DIR / MANIFEST_FILENAME

    def build(
        self,
        *,
        head_commit: str = "",
        include_private: bool = True,
    ) -> Manifest:
        """从仓库 INDEX.md + 实际资产派生 manifest。"""
        # 1. 解析分层 INDEX.md
        index_docs = discover_levels(self.repo_root)
        modules: list[ManifestModuleEntry] = []
        wc = WorkingCopy(self.repo_root)
        for doc in index_docs:
            module_path = doc.module or ""
            level = doc.level.value if doc.level else "project"
            index_path = (
                doc.source_path.relative_to(self.repo_root).as_posix()
                if doc.source_path
                else "INDEX.md"
            )
            # 收集该层登记的资产 + 实际文件 frontmatter
            asset_entries: list[ManifestAssetEntry] = []
            for asset_entry in doc.assets:
                asset_file_path = asset_entry.path
                try:
                    asset = wc.read_asset(asset_file_path)
                    me = ManifestAssetEntry.from_asset_file(asset)
                    # 若 INDEX.md 登记 module 与 frontmatter 不一致，以 INDEX.md 所在层 module_path 为准
                    me.module_path = module_path if doc.level and doc.level.value != "project" else me.module_path
                    asset_entries.append(me)
                except FileNotFoundError:
                    # INDEX.md 登记但文件不存在（防孤岛违规场景）→ 仍记录基本信息
                    asset_entries.append(
                        ManifestAssetEntry(
                            id=asset_entry.id,
                            type=asset_entry.type,
                            path=asset_file_path,
                            module_path=module_path,
                        )
                    )
            sub_names = [s.name for s in doc.submodules]
            modules.append(
                ManifestModuleEntry(
                    module_path=module_path,
                    level=level,
                    index_path=index_path,
                    assets=asset_entries,
                    submodules=sub_names,
                )
            )

        # 2. 私有资产索引
        private_assets: list[ManifestAssetEntry] = []
        if include_private:
            pi = PrivateIsolation(self.repo_root)
            for asset in pi.list_private_assets():
                me = ManifestAssetEntry.from_asset_file(asset)
                # 私有资产 path 转为相对仓库根的路径（供离线召回按路径读取）
                me.path = (pi.private_root / asset.relative_path).relative_to(self.repo_root).as_posix()
                private_assets.append(me)

        # 3. counts 统计
        total_assets = sum(len(m.assets) for m in modules)
        total_modules = sum(1 for m in modules if m.level != "project")
        counts = {
            "assets": total_assets,
            "modules": total_modules,
            "private_assets": len(private_assets),
        }

        return Manifest(
            version=MANIFEST_VERSION,
            generated_at=_utcnow_iso(),
            repo_root=str(self.repo_root),
            head_commit=head_commit,
            modules=modules,
            private_assets=private_assets,
            counts=counts,
        )

    def save(self, manifest: Manifest) -> Path:
        """写入 manifest.json。父目录自动创建。"""
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)
        self.manifest_path.write_text(
            json.dumps(manifest.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return self.manifest_path

    def load(self) -> Manifest | None:
        """读取已有 manifest.json；不存在返回 None。"""
        if not self.manifest_path.is_file():
            return None
        try:
            data = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return Manifest.from_dict(data)

    def diff(
        self,
        old: Manifest,
        new: Manifest,
    ) -> dict[str, list[dict[str, Any]]]:
        """比对两个 manifest，返回差异（新增/修改/删除）。

        用于 sync 前后差异展示、采纳率统计等。
        """
        old_assets: dict[str, ManifestAssetEntry] = {}
        for m in old.modules:
            for a in m.assets:
                old_assets[a.path] = a
        for a in old.private_assets:
            old_assets[a.path] = a
        new_assets: dict[str, ManifestAssetEntry] = {}
        for m in new.modules:
            for a in m.assets:
                new_assets[a.path] = a
        for a in new.private_assets:
            new_assets[a.path] = a

        added: list[dict[str, Any]] = []
        modified: list[dict[str, Any]] = []
        deleted: list[dict[str, Any]] = []
        for path, new_a in new_assets.items():
            if path not in old_assets:
                added.append({"path": path, "id": new_a.id, "type": new_a.type})
            else:
                old_a = old_assets[path]
                if old_a.content_hash != new_a.content_hash or old_a.version != new_a.version:
                    modified.append(
                        {
                            "path": path,
                            "id": new_a.id,
                            "type": new_a.type,
                            "old_version": old_a.version,
                            "new_version": new_a.version,
                        }
                    )
        for path, old_a in old_assets.items():
            if path not in new_assets:
                deleted.append({"path": path, "id": old_a.id, "type": old_a.type})
        return {"added": added, "modified": modified, "deleted": deleted}


__all__ = [
    "MANIFEST_VERSION",
    "Manifest",
    "ManifestAssetEntry",
    "ManifestBuilder",
    "ManifestModuleEntry",
]
