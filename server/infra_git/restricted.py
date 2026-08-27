"""restricted 读权限。

对应技术方案 SubTask 1.8 + 8.1：restricted scope 资产支持两种承载方式：
1. git-crypt 加密目录（主仓库 restricted/ 子目录，clone 后需 git-crypt unlock）
2. 独立受限仓库（单独的受限访问仓库，普通 clone 不可见）

普通 clone 无法看到明文；只有持 git-crypt key 或独立仓库访问权的服务/成员可读。
本模块提供统一 RestrictedReader 抽象，屏蔽两种承载方式差异。
"""

from __future__ import annotations

import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

# restricted 资产在主仓库的承载目录（对应技术方案 3.4.1）
RESTRICTED_DIRNAME = "restricted"


# ---------------------------------------------------------------------------
# 探测
# ---------------------------------------------------------------------------


def detect_restricted_dir(repo_root: Path) -> Path | None:
    """探测主仓库是否存在 restricted/ 目录。存在即返回路径。"""
    cand = Path(repo_root) / RESTRICTED_DIRNAME
    return cand if cand.is_dir() else None


def is_git_crypt_repo(repo_root: Path) -> bool:
    """判断仓库是否启用 git-crypt。

    判据：.gitattributes 含 `crypt` filter，或 .git/git-crypt 目录存在。
    """
    root = Path(repo_root)
    gitattributes = root / ".gitattributes"
    if gitattributes.is_file():
        try:
            text = gitattributes.read_text(encoding="utf-8")
        except OSError:
            text = ""
        if "filter=git-crypt" in text or "git-crypt" in text:
            return True
    return (root / ".git" / "git-crypt").is_dir()


def is_git_crypt_unlocked(repo_root: Path) -> bool:
    """判断 git-crypt 是否已解锁（工作区文件可读明文）。

    启发式：检测 restricted/ 下任一文件首字节是否为 git-crypt 魔数 \\0GIT-CRYPT。
    若有加密文件 → 视为未解锁；否则视为已解锁。
    若无文件则视为未解锁。

    注：git-crypt 完整魔数为 ``\\x00GIT-CRYPT\\x00``（10 字节），此处读取 16 字节
    并用 ``startswith(b"\\x00GIT-CRYPT")`` 判定，足够鲁棒。
    """
    restricted = detect_restricted_dir(repo_root)
    if restricted is None:
        return False
    for f in restricted.rglob("*"):
        if not f.is_file():
            continue
        try:
            with f.open("rb") as fp:
                head = fp.read(16)
        except OSError:
            continue
        # git-crypt 加密文件以 \0GIT-CRYPT 开头；明文文件不会以此开头
        if head.startswith(b"\x00GIT-CRYPT"):
            return False
        return True
    return False


def unlock_git_crypt(repo_root: Path, key_path: Path) -> None:
    """执行 git-crypt unlock <key>，解锁工作区。

    需要 git-crypt 可执行；缺失抛 FileNotFoundError。
    """
    if shutil.which("git-crypt") is None:
        raise FileNotFoundError("git-crypt 可执行未安装")
    subprocess.run(
        ["git-crypt", "unlock", str(key_path)],
        cwd=str(repo_root),
        check=True,
        capture_output=True,
    )


# ---------------------------------------------------------------------------
# RestrictedReader 抽象
# ---------------------------------------------------------------------------


class RestrictedReader(ABC):
    """restricted 资产统一读接口。"""

    @abstractmethod
    def read(self, path: str) -> str:
        """读取 restricted 资产明文。path 相对 restricted 根。"""

    @abstractmethod
    def list_files(self, path: str = "") -> list[str]:
        """列出 restricted 下文件。"""

    @abstractmethod
    def is_available(self) -> bool:
        """当前 reader 是否可用（已解锁 / 已授权）。"""


@dataclass
class GitCryptReader(RestrictedReader):
    """git-crypt 加密目录 reader。

    工作于主仓库 working copy；调用前需确保 git-crypt 已 unlock。
    """

    repo_root: Path

    @property
    def restricted_root(self) -> Path:
        return Path(self.repo_root) / RESTRICTED_DIRNAME

    def is_available(self) -> bool:
        return detect_restricted_dir(self.repo_root) is not None and (
            not is_git_crypt_repo(self.repo_root) or is_git_crypt_unlocked(self.repo_root)
        )

    def _resolve(self, path: str) -> Path:
        target = (self.restricted_root / path).resolve()
        # 防路径穿越
        if not str(target).startswith(str(self.restricted_root.resolve())):
            raise PermissionError(f"路径越界 restricted 根：{path}")
        return target

    def read(self, path: str) -> str:
        if not self.is_available():
            raise PermissionError("git-crypt 未解锁，无法读取 restricted 资产")
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"restricted 资产不存在：{path}")
        return target.read_text(encoding="utf-8")

    def list_files(self, path: str = "") -> list[str]:
        if not self.is_available():
            return []
        base = self._resolve(path) if path else self.restricted_root
        if not base.is_dir():
            return []
        return [
            str(f.relative_to(self.restricted_root).as_posix())
            for f in base.rglob("*")
            if f.is_file()
        ]


@dataclass
class IndependentRepoReader(RestrictedReader):
    """独立受限仓库 reader。

    独立仓库承载 restricted 资产，通过独立 clone 路径访问。
    普通 clone 主仓库者无法见到该仓库。
    """

    repo_path: Path  # 已 clone 的独立受限仓库本地路径

    def is_available(self) -> bool:
        return self.repo_path.is_dir() and (self.repo_path / ".git").exists()

    def _resolve(self, path: str) -> Path:
        target = (self.repo_path / path).resolve()
        if not str(target).startswith(str(self.repo_path.resolve())):
            raise PermissionError(f"路径越界独立受限仓库根：{path}")
        return target

    def read(self, path: str) -> str:
        if not self.is_available():
            raise PermissionError("独立受限仓库不可用")
        target = self._resolve(path)
        if not target.is_file():
            raise FileNotFoundError(f"受限仓库资产不存在：{path}")
        return target.read_text(encoding="utf-8")

    def list_files(self, path: str = "") -> list[str]:
        if not self.is_available():
            return []
        base = self._resolve(path) if path else self.repo_path
        if not base.is_dir():
            return []
        return [
            str(f.relative_to(self.repo_path).as_posix())
            for f in base.rglob("*")
            if f.is_file() and ".git" not in f.parts
        ]


def create_restricted_reader(
    repo_root: Path, *, key_path: Path | None = None, independent_repo: Path | None = None
) -> RestrictedReader:
    """工厂：按仓库实际承载方式创建 reader。

    优先独立仓库（若提供），否则 git-crypt 目录。
    """
    if independent_repo is not None:
        return IndependentRepoReader(repo_path=Path(independent_repo))
    if key_path is not None and is_git_crypt_repo(repo_root) and not is_git_crypt_unlocked(repo_root):
        unlock_git_crypt(repo_root, key_path)
    return GitCryptReader(repo_root=Path(repo_root))
