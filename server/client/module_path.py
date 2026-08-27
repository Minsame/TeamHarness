"""module_path 上下文推断。

对应 SubTask 6.5 + 技术方案 3.2.2「module_path 获取方式」：
1. 客户端上下文推断：从 coding 软件当前打开的项目路径，
   经 mapping.yaml 反查所属模块（最常用）。
2. 用户显式指定：CLI `teamharness recall --module backend`。
3. 服务端 LLM 推断：召回 API 接受任务描述，服务端用小模型推断（兜底）。
4. 无 module_path：回退纯向量检索全量资产池（精度略低但可用）。

本模块负责策略 1（客户端上下文推断）与策略 2 的统一入口，
返回推断结果 + 来源标记（供召回客户端决策）。

跨软件路径差异（重点风险）：
- Trae：物理根 .trae-cn/memory/，cwd 可能为其子路径
- Cursor：物理根 ~/.cursor/memories/
- OpenClaw：物理根 ~/.openclaw/workspace/
- 自定义：任意路径
- 用户可能在仓库外（如系统目录）打开文件 → 推断返回 None，由上层走兜底

推断流程：
1. 拿到 cwd（绝对路径）
2. 与 mapping.yaml.root（物理根）求相对路径；不在物理根下 → 尝试匹配
   module_paths 表中的物理路径片段
3. 调 MappingConfig.physical_to_logical_module 得到 module_path
4. 返回 ModulePathInference(module_path, source, confidence)
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.client.mapping import MappingConfig, load_mapping


@dataclass
class ModulePathInference:
    """module_path 推断结果。

    source 取值：
    - 'cwd'：从 coding 软件 cwd 反查（策略 1）
    - 'explicit'：用户显式指定（策略 2）
    - 'env'：环境变量 TEAMHARNESS_MODULE_PATH
    - 'none'：未推断到（策略 4，调用方走纯向量检索）

    confidence：1.0（显式） / 0.8（cwd 精确匹配） / 0.5（cwd 前缀匹配） / 0.0（未推断）
    """

    module_path: str
    source: str = "none"
    confidence: float = 0.0


def infer_from_cwd(
    cwd: str | Path | None = None,
    *,
    mapping: MappingConfig | None = None,
    repo_root: str | Path | None = None,
) -> ModulePathInference:
    """从当前工作目录推断 module_path（策略 1）。

    cwd 缺省为 os.getcwd()。若 cwd 在物理根下，求相对路径后调
    mapping.physical_to_logical_module；否则尝试以 cwd 全路径做模糊匹配。

    返回的 module_path 为逻辑路径（如 'modules/backend'）；
    未识别则返回 source='none', confidence=0.0。
    """
    cwd_path = Path(cwd) if cwd else Path.cwd()
    if mapping is None:
        if repo_root is None:
            repo_root = Path.cwd()
        mapping = load_mapping(repo_root)

    # 1. cwd 在物理根下 → 求相对路径
    physical_root = mapping.physical_root(repo_root or Path.cwd())
    try:
        rel = cwd_path.resolve().relative_to(physical_root.resolve())
        rel_str = rel.as_posix().strip("/")
        if rel_str:
            mp = mapping.physical_to_logical_module(rel_str)
            if mp:
                # 精确或前缀匹配
                confidence = 0.8 if rel_str in mapping.module_paths else 0.5
                return ModulePathInference(module_path=mp, source="cwd", confidence=confidence)
        # rel 为空字符串表示 cwd == 物理根 → 根级，无 module_path
        return ModulePathInference(module_path="", source="none", confidence=0.0)
    except ValueError:
        pass

    # 2. cwd 不在物理根下 → 尝试在 cwd 路径片段中找 'modules/<seg>' 形式
    cwd_str = cwd_path.as_posix()
    mp = mapping.physical_to_logical_module(cwd_str)
    if mp:
        return ModulePathInference(module_path=mp, source="cwd", confidence=0.5)
    return ModulePathInference(module_path="", source="none", confidence=0.0)


def from_explicit(module: str | None) -> ModulePathInference:
    """用户显式指定 module_path（策略 2）。

    module 为 None 或空字符串 → 返回 source='none'，由上层处理。
    """
    if not module:
        return ModulePathInference(module_path="", source="none", confidence=0.0)
    return ModulePathInference(
        module_path=module.replace("\\", "/").strip("/"),
        source="explicit",
        confidence=1.0,
    )


def from_env(env_name: str = "TEAMHARNESS_MODULE_PATH") -> ModulePathInference:
    """从环境变量读取 module_path（CI / 守护进程场景）。"""
    raw = os.environ.get(env_name, "")
    if not raw:
        return ModulePathInference(module_path="", source="none", confidence=0.0)
    return ModulePathInference(
        module_path=raw.replace("\\", "/").strip("/"),
        source="env",
        confidence=0.9,
    )


def infer_module_path(
    *,
    explicit: str | None = None,
    cwd: str | Path | None = None,
    mapping: MappingConfig | None = None,
    repo_root: str | Path | None = None,
    use_env: bool = True,
) -> ModulePathInference:
    """统一入口：按优先级合并多来源 module_path 推断。

    优先级：
    1. explicit（CLI --module）
    2. cwd（coding 软件当前路径反查）
    3. env（TEAMHARNESS_MODULE_PATH）
    4. none（未推断到）

    任一来源返回 source != 'none' 即停止并返回；不混合多来源。
    """
    if explicit:
        result = from_explicit(explicit)
        if result.source != "none":
            return result
    result = infer_from_cwd(cwd, mapping=mapping, repo_root=repo_root)
    if result.source != "none":
        return result
    if use_env:
        result = from_env()
        if result.source != "none":
            return result
    return ModulePathInference(module_path="", source="none", confidence=0.0)


# ---------------------------------------------------------------------------
# 工具：模块路径合法性校验
# ---------------------------------------------------------------------------


def is_valid_module_path(module_path: str) -> bool:
    """module_path 合法性：空（根级）或形如 modules/<seg>[/<seg>...]。

    每段须为 kebab-case 或 snake_case 标识符。
    """
    if not module_path:
        return True  # 根级资产
    mp = module_path.replace("\\", "/").strip("/")
    if not mp:
        return True
    parts = mp.split("/")
    for p in parts:
        if not p or any(c.isupper() for c in p) or " " in p:
            return False
    return True


def normalize_module_path(module_path: str) -> str:
    """规范化 module_path：POSIX 风格 + 去首尾斜杠。"""
    if not module_path:
        return ""
    return module_path.replace("\\", "/").strip("/")


__all__ = [
    "ModulePathInference",
    "from_env",
    "from_explicit",
    "infer_from_cwd",
    "infer_module_path",
    "is_valid_module_path",
    "normalize_module_path",
]
