"""私有资产隔离（.teamharness/private/ + .gitignore）。

对应 SubTask 6.8 + 技术方案 3.1.3「私有资产处理」：
- scope=private 的资产不入中央仓库，存放于本地 .teamharness/private/
- .teamharness/private/ 加入 .gitignore，防止误提交导致泄露
- 一级提炼产出的资产默认 private，用户显式改为 team/public 后才会被 sync 纳入提交
- private 资产不进入 DB 索引层（不在服务端存储），召回时仅本地匹配

设计要点（重点风险 🔴 私有资产隔离，泄露风险）：
1. 私有资产目录结构与中央仓库镜像分层结构（modules/<m>/submodules/<s>/rules/...）
   → 召回时本地匹配可保留 module_path 维度
2. .gitignore 必须：
   - 整体忽略 .teamharness/private/
   - 整体忽略 .teamharness/manifest.json（本地缓存索引）
   - 不忽略 .teamharness/mapping.yaml、categories.yaml、config.yaml 等共享配置
3. sync 前置检查：若检测到 .teamharness/private/ 未在 .gitignore 中 →
   自动追加规则 + 告警（防误提交）
4. 暴露 private 检索能力（离线召回降级时使用）
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.client.config import MANIFEST_FILENAME, MAPPING_FILENAME, PRIVATE_DIRNAME, TEAMHARNESS_DIR
from server.client.working_copy import (
    ASSET_SUFFIX,
    AssetFile,
    DIR_TO_ASSET_TYPE,
)
from server.common.models import Scope
from server.infra_git.trae_adapter import parse_frontmatter_dual

# .gitignore 中必须包含的规则（防泄露）
# 注意：mapping.yaml / categories.yaml / config.yaml 入 git，不在忽略列表
REQUIRED_GITIGNORE_RULES: tuple[str, ...] = (
    f"{TEAMHARNESS_DIR}/{PRIVATE_DIRNAME}/",
    f"{TEAMHARNESS_DIR}/{MANIFEST_FILENAME}",
    # 采纳率本地缓存（adoption.py 写入）
    f"{TEAMHARNESS_DIR}/adoption-cache.json",
    # 一级提炼 .dreams/ 暂存区（不入 git）
    f"{TEAMHARNESS_DIR}/dreams/",
    # 本地 state 文件
    f"{TEAMHARNESS_DIR}/state.json",
)

# .gitignore 中显式不忽略的共享配置（! 规则）
ALLOWED_SHARED_FILES: tuple[str, ...] = (
    f"!{TEAMHARNESS_DIR}/mapping.yaml",
    f"!{TEAMHARNESS_DIR}/categories.yaml",
    f"!{TEAMHARNESS_DIR}/config.yaml",
    f"!{TEAMHARNESS_DIR}/hooks.yaml",
    f"!{TEAMHARNESS_DIR}/routing.yaml",
)


@dataclass
class GitignoreStatus:
    """.gitignore 检查结果。"""

    gitignore_path: Path
    exists: bool
    missing_rules: list[str]
    extra_rules: list[str]  # 客户端管理的额外规则（供调试）
    fixed: bool = False  # 是否已自动修复

    @property
    def ok(self) -> bool:
        return not self.missing_rules


class PrivateIsolation:
    """私有资产隔离管理器。

    职责：
    - 维护 .teamharness/private/ 目录（镜像中央仓库分层结构）
    - 检查并修复 .gitignore（防泄露）
    - 写入/读取/列举/删除私有资产
    - 离线召回降级时供 recall_client 调用做本地匹配
    """

    def __init__(self, repo_root: Path | str) -> None:
        self.repo_root = Path(repo_root)
        self.private_root = self.repo_root / TEAMHARNESS_DIR / PRIVATE_DIRNAME

    # ------------------------------------------------------------------
    # .gitignore 维护
    # ------------------------------------------------------------------

    def gitignore_path(self) -> Path:
        return self.repo_root / ".gitignore"

    def check_gitignore(self) -> GitignoreStatus:
        """检查 .gitignore 是否含必要的忽略规则。

        返回 missing_rules 列表（缺失的规则），ok 属性判断是否通过。
        """
        path = self.gitignore_path()
        if not path.is_file():
            return GitignoreStatus(
                gitignore_path=path,
                exists=False,
                missing_rules=list(REQUIRED_GITIGNORE_RULES),
                extra_rules=[],
            )
        text = path.read_text(encoding="utf-8")
        existing_lines = {line.strip() for line in text.splitlines() if line.strip()}
        missing = [r for r in REQUIRED_GITIGNORE_RULES if r not in existing_lines]
        # extra = 用户自定义但不在 REQUIRED 也不在 ALLOWED 的客户端规则（仅信息性，不强制）
        client_managed_prefix = f"{TEAMHARNESS_DIR}/"
        extra = [
            line
            for line in existing_lines
            if line.startswith(client_managed_prefix)
            and line not in REQUIRED_GITIGNORE_RULES
            and not line.startswith("!")
        ]
        return GitignoreStatus(
            gitignore_path=path,
            exists=True,
            missing_rules=missing,
            extra_rules=extra,
        )

    def ensure_gitignore(self, *, append: bool = True) -> GitignoreStatus:
        """确保 .gitignore 含必要规则；缺失则追加。

        append=True 时保留既有内容，在末尾追加缺失规则；
        append=False 时若缺失任何必要规则则报错（不修改文件）。
        返回修复后的状态。
        """
        status = self.check_gitignore()
        if status.ok:
            return status
        if not append:
            return status
        path = self.gitignore_path()
        existing_text = path.read_text(encoding="utf-8") if path.is_file() else ""
        # 追加 TeamHarness 隔离区块
        block_lines = ["", "# TeamHarness 私有资产隔离（自动维护，请勿删除）"]
        block_lines.extend(status.missing_rules)
        # 共享配置的负向规则
        block_lines.append("# 共享配置不忽略")
        block_lines.extend(ALLOWED_SHARED_FILES)
        block_text = "\n".join(block_lines) + "\n"
        new_text = existing_text
        if new_text and not new_text.endswith("\n"):
            new_text += "\n"
        new_text += block_text
        path.write_text(new_text, encoding="utf-8")
        # 重新检查
        new_status = self.check_gitignore()
        new_status.fixed = True
        return new_status

    # ------------------------------------------------------------------
    # 私有资产目录与文件读写
    # ------------------------------------------------------------------

    def private_dir_for(self, module_path: str = "", asset_type_dir: str = "") -> Path:
        """返回私有资产目录路径（按 module_path + type_dir 镜像分层结构）。"""
        parts = [self.private_root]
        if module_path:
            mp = module_path.replace("\\", "/").strip("/")
            parts.append(Path(mp))
        if asset_type_dir:
            parts.append(Path(asset_type_dir))
        return Path(*parts)

    def write_private_asset(
        self,
        asset_type_dir: str,
        name: str,
        frontmatter: dict[str, Any],
        body: str,
        *,
        module_path: str = "",
        coding_fields: dict[str, Any] | None = None,
    ) -> Path:
        """写入私有资产。强制 frontmatter.scope='private'。

        asset_type_dir 为 'rules' / 'memory' / 'skills' / 'tools' / 'prompts'。
        module_path 会写入 frontmatter，供后续 promote_to_team / list 反查使用。
        """
        if asset_type_dir not in DIR_TO_ASSET_TYPE:
            raise ValueError(f"非法资产目录: {asset_type_dir}")
        # 强制 scope=private + 注入 module_path（保留调用方已设值或覆盖空值）
        frontmatter = {
            **frontmatter,
            "scope": Scope.PRIVATE.value,
            "module_path": module_path or frontmatter.get("module_path", ""),
        }
        filename = name if name.endswith(ASSET_SUFFIX) else f"{name}{ASSET_SUFFIX}"
        target_dir = self.private_dir_for(module_path, asset_type_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / filename
        # 序列化（复用双区 frontmatter）
        from server.infra_git.trae_adapter import (
            TraeFrontmatter,
            serialize_frontmatter_dual,
        )
        fm_obj = TraeFrontmatter(
            coding_fields=coding_fields or {"coding": "trae"},
            teamharness_fields=dict(frontmatter),
            body=body,
        )
        target.write_text(serialize_frontmatter_dual(fm_obj), encoding="utf-8")
        return target

    def read_private_asset(self, relative_to_private: str) -> AssetFile:
        """读取私有资产（路径相对 .teamharness/private/）。"""
        target = (self.private_root / relative_to_private).resolve()
        if not str(target).startswith(str(self.private_root.resolve())):
            raise PermissionError(f"路径越界私有资产根: {relative_to_private}")
        if not target.is_file():
            raise FileNotFoundError(f"私有资产不存在: {relative_to_private}")
        content = target.read_text(encoding="utf-8")
        fm = parse_frontmatter_dual(content)
        rel = target.relative_to(self.private_root).as_posix()
        return AssetFile(
            relative_path=rel,
            frontmatter=dict(fm.teamharness_fields),
            body=fm.body,
            coding_fields=dict(fm.coding_fields),
        )

    def list_private_assets(
        self,
        *,
        asset_type_dir: str | None = None,
        module_path: str | None = None,
    ) -> list[AssetFile]:
        """列举私有资产。

        - 仅 module_path：搜索该模块下所有类型（递归子目录）
        - 仅 asset_type_dir：跨所有模块搜索该类型（私有目录镜像分层结构，
          type 子目录可能位于 private/<type>/ 或 private/modules/<m>/<type>/ 等，
          故从 private_root 递归扫描后按路径中的 type 段过滤）
        - 两者均给：精确到 private/<module_path>/<type>/
        - 均不给：列举全部
        """
        if not self.private_root.is_dir():
            return []
        results: list[AssetFile] = []
        # 计算搜索根
        if module_path and asset_type_dir:
            search_root = self.private_dir_for(module_path, asset_type_dir)
        elif module_path:
            search_root = self.private_dir_for(module_path)
        elif asset_type_dir:
            # 仅按 type 过滤：跨模块搜索，从 private_root 递归后按路径段过滤
            search_root = self.private_root
        else:
            search_root = self.private_root
        if not search_root.is_dir():
            return []
        for f in sorted(search_root.rglob(f"*{ASSET_SUFFIX}")):
            if not f.is_file():
                continue
            # 仅 asset_type_dir 时按路径中的 type 段过滤
            if asset_type_dir and not module_path:
                rel_parts = f.relative_to(self.private_root).parts
                if asset_type_dir not in rel_parts:
                    continue
            try:
                asset = self.read_private_asset(
                    str(f.relative_to(self.private_root).as_posix())
                )
                results.append(asset)
            except (OSError, PermissionError):
                continue
        return results

    def delete_private_asset(self, relative_to_private: str) -> bool:
        """删除私有资产。"""
        target = (self.private_root / relative_to_private).resolve()
        if not str(target).startswith(str(self.private_root.resolve())):
            raise PermissionError(f"路径越界私有资产根: {relative_to_private}")
        if not target.is_file():
            return False
        target.unlink()
        return True

    # ------------------------------------------------------------------
    # 提升为团队资产
    # ------------------------------------------------------------------

    def promote_to_team(
        self,
        relative_to_private: str,
        target_repo_root: Path | str,
        *,
        new_scope: Scope = Scope.TEAM,
    ) -> Path:
        """将私有资产提升为团队/公开资产（写入中央仓库工作区）。

        流程：
        1. 读取私有资产
        2. 改写 frontmatter.scope = new_scope
        3. 写入中央仓库对应位置（按 module_path + type 推算路径）
        4. 删除私有副本（可选，由调用方决定）

        返回中央仓库新路径；调用方随后用 GitSync.commit + sync 推送。
        """
        if new_scope == Scope.PRIVATE:
            raise ValueError("提升目标 scope 不能为 private")
        asset = self.read_private_asset(relative_to_private)
        # 推算中央仓库路径
        from server.client.working_copy import resolve_asset_path
        type_dir = next(
            (
                d
                for d, t in DIR_TO_ASSET_TYPE.items()
                if t == asset.asset_type
            ),
            None,
        )
        if type_dir is None:
            raise ValueError(f"私有资产 type 字段非法: {asset.frontmatter.get('type')}")
        # 解析资产名（去前缀 + 去 .md）
        name = Path(relative_to_private).name
        target_path = resolve_asset_path(
            Path(target_repo_root),
            asset.asset_type or type_dir,
            name,
            module_path=asset.module_path,
        )
        target_path.parent.mkdir(parents=True, exist_ok=True)
        # 写入新 scope
        new_frontmatter = {**asset.frontmatter, "scope": new_scope.value}
        from server.infra_git.trae_adapter import (
            TraeFrontmatter,
            serialize_frontmatter_dual,
        )
        fm_obj = TraeFrontmatter(
            coding_fields=asset.coding_fields or {"coding": "trae"},
            teamharness_fields=new_frontmatter,
            body=asset.body,
        )
        target_path.write_text(serialize_frontmatter_dual(fm_obj), encoding="utf-8")
        return target_path


__all__ = [
    "ALLOWED_SHARED_FILES",
    "GitignoreStatus",
    "PrivateIsolation",
    "REQUIRED_GITIGNORE_RULES",
]
