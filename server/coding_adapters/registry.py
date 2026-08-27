"""本机 AI coding 软件检测与 provider 路由。

对应 Task 1：CodingSoftwareRegistry 遍历 SOFTWARE_FINGERPRINTS，探测本机已安装软件，
输出 InstalledSoftware 列表，供后续 Task 按 provider_name 路由到具体 Adapter。

对应 Task 2：三级探测策略（路径直探 / PATH 扫描 / 指纹模糊匹配）：
1. 路径直探：detect.paths 中任一路径经 resolve_path 解析后存在 → 命中
   （discover_installed 中实现）
2. PATH 扫描：paths 全 miss 且 detect.cli 非空 → shutil.which(cli) 命中
   （discover_installed 中实现）
3. 指纹模糊匹配：扫描 ~ 下含 sessions/*.jsonl / .rules / memory/ 等特征目录的文件夹，
   命中后用 GenericJsonlSessionProvider 兜底（discover_by_fingerprint 中实现）
"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from server.coding_adapters.fingerprints import (
    SOFTWARE_FINGERPRINTS,
    resolve_path,
)


@dataclass
class InstalledSoftware:
    """已安装软件信息。"""

    name: str  # 软件标识（trae/claude_code/...）
    install_path: Path  # 安装路径（命中的 paths 解析路径或 cli 路径）
    cli_path: Path | None = None  # CLI 路径（如通过 shutil.which 命中）
    provider_name: str = ""  # 对应 Adapter 类名


class CodingSoftwareRegistry:
    """本机 AI 软件检测与 provider 路由。"""

    def discover_installed(self) -> list[InstalledSoftware]:
        """探测本机装了哪些 AI coding 软件。

        遍历 SOFTWARE_FINGERPRINTS，对每个软件：
        1. 检查 detect.paths 中的路径是否存在
        2. 若 paths 都不存在，尝试 shutil.which(detect.cli)
        3. 任一命中则记录为 InstalledSoftware
        """
        results: list[InstalledSoftware] = []
        for name, fp in SOFTWARE_FINGERPRINTS.items():
            detect = fp.get("detect", {}) or {}
            paths = detect.get("paths", []) or []
            cli = detect.get("cli")

            install_path: Path | None = None
            cli_path: Path | None = None

            # 1. 检查已知安装路径
            for path_template in paths:
                resolved = resolve_path(path_template)
                if resolved.exists():
                    install_path = resolved
                    break

            # 2. 路径未命中且配置了 cli，尝试 PATH 查找
            if install_path is None and cli:
                which_result = shutil.which(cli)
                if which_result:
                    cli_path = Path(which_result)
                    install_path = cli_path

            if install_path is None:
                continue

            results.append(
                InstalledSoftware(
                    name=name,
                    install_path=install_path,
                    cli_path=cli_path,
                    provider_name=fp.get("provider", ""),
                )
            )
        return results

    def get_session_providers(self) -> list[str]:
        """返回已安装软件对应的 provider 类名列表。"""
        return [
            s.provider_name
            for s in self.discover_installed()
            if s.provider_name
        ]

    # ------------------------------------------------------------------
    # 三级探测之一：指纹模糊匹配（兜底）
    # ------------------------------------------------------------------

    def discover_by_fingerprint(
        self, scan_root: Path | None = None
    ) -> list[InstalledSoftware]:
        """指纹模糊匹配兜底探测。

        扫描 scan_root（默认 ~）下的目录，查找含 sessions/*.jsonl
        或 .rules / memory/ 等特征目录的文件夹，识别为未知 AI 软件。

        探测特征（任一命中即识别为疑似 AI coding 软件）：
        - <dir>/sessions/ 目录存在且内含 *.jsonl
        - <dir>/.rules 文件或目录存在
        - <dir>/memory/ 目录存在

        命中后用 GenericJsonlSessionProvider 作为兜底 provider。
        已被 SOFTWARE_FINGERPRINTS 中 paths 命中的目录会被跳过，
        避免与 discover_installed 结果重复。

        Args:
            scan_root: 扫描根目录，None 时默认 Path.home()。

        Returns:
            识别到的未知 AI 软件列表，name 形如 ``unknown_<sanitized_dirname>``。
        """
        root = scan_root if scan_root is not None else Path.home()
        # 工程规则：iterdir 前必须先 is_dir() 短路
        if not root.is_dir():
            return []

        known_paths = self._known_install_path_set()

        results: list[InstalledSoftware] = []
        for child in root.iterdir():
            if not child.is_dir():
                continue
            try:
                resolved = child.resolve()
            except OSError:
                continue
            if resolved in known_paths:
                continue
            if self._looks_like_ai_software(child):
                name = self._sanitize_unknown_name(child.name)
                results.append(
                    InstalledSoftware(
                        name=name,
                        install_path=child,
                        cli_path=None,
                        provider_name="GenericJsonlSessionProvider",
                    )
                )
        return results

    # ------------------------------------------------------------------
    # 辅助
    # ------------------------------------------------------------------

    @staticmethod
    def _known_install_path_set() -> set[Path]:
        """收集 SOFTWARE_FINGERPRINTS 中 detect.paths 解析后的路径集合。

        用于 discover_by_fingerprint 跳过已知软件安装目录，避免重复。
        """
        known: set[Path] = set()
        for fp in SOFTWARE_FINGERPRINTS.values():
            paths = (fp.get("detect", {}) or {}).get("paths", []) or []
            for tpl in paths:
                try:
                    known.add(resolve_path(tpl).resolve())
                except OSError:
                    continue
        return known

    @staticmethod
    def _looks_like_ai_software(directory: Path) -> bool:
        """检查目录是否符合 AI coding 软件特征。

        任一命中即返回 True：
        - sessions/ 子目录存在且内含 *.jsonl
        - .rules 文件或目录存在
        - memory/ 目录存在
        """
        # sessions/*.jsonl（glob 前先 is_dir 短路）
        sessions_dir = directory / "sessions"
        if sessions_dir.is_dir():
            try:
                for p in sessions_dir.glob("*.jsonl"):
                    if p.is_file():
                        return True
            except OSError:
                pass
        # .rules 文件或目录
        rules_path = directory / ".rules"
        if rules_path.exists():
            return True
        # memory/ 目录
        memory_dir = directory / "memory"
        if memory_dir.is_dir():
            return True
        return False

    @staticmethod
    def _sanitize_unknown_name(raw: str) -> str:
        """规整未知软件名：非字母数字下划线替换为 _，小写，加 unknown_ 前缀。"""
        sanitized = re.sub(r"[^a-zA-Z0-9_]+", "_", raw).strip("_").lower()
        if not sanitized:
            sanitized = "unknown"
        return f"unknown_{sanitized}"


__all__ = ["CodingSoftwareRegistry", "InstalledSoftware"]
