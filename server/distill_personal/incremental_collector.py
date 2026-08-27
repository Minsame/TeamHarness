"""对话记录增量采集 — 只处理新会话。

对应 SubTask 7.2：
- 基于 mtime 水位线，只读取新会话（或上次采集后修改的会话）
- 水位线持久化到 .teamharness-local/distill/watermark.json（不入 git，纯本地状态）
- 增量采集只返回 SessionMeta，调用方按需 read_session 拿正文
- 隐私：原始对话内容只在本机读取，不上传（隐私保护见 privacy.py）
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from server.distill_personal.session_provider import (
    Session,
    SessionMeta,
    SessionProvider,
)

logger = logging.getLogger(__name__)

# 水位线默认路径（相对 repo_root）
DEFAULT_WATERMARK_DIR = Path(".teamharness-local") / "distill"
DEFAULT_WATERMARK_FILENAME = "watermark.json"


@dataclass
class IncrementalCollectResult:
    """增量采集结果。"""

    new_sessions: list[SessionMeta]
    skipped_count: int  # 因 mtime < 水位线被跳过的会话数
    watermark_before: float  # 采集前水位线（epoch）
    watermark_after: float  # 采集后水位线（epoch）

    @property
    def new_count(self) -> int:
        return len(self.new_sessions)


class IncrementalCollector:
    """增量采集器。

    使用：
        collector = IncrementalCollector(provider, watermark_path=...)
        result = collector.collect()  # 只返回新会话
        for meta in result.new_sessions:
            session = provider.read_session(meta.session_id)
            ...

    水位线语义：
    - 首次采集（水位线文件不存在）→ 返回全部会话，水位线设为最新 mtime
    - 后续采集 → 只返回 mtime > 水位线 的会话，水位线更新为最新 mtime
    - mtime 相同的会话不重复返回（严格大于）
    """

    def __init__(
        self,
        provider: SessionProvider,
        *,
        watermark_path: Path | None = None,
        repo_root: Path | None = None,
    ) -> None:
        self.provider = provider
        if watermark_path is not None:
            self.watermark_path = watermark_path
        elif repo_root is not None:
            self.watermark_path = repo_root / DEFAULT_WATERMARK_DIR / DEFAULT_WATERMARK_FILENAME
        else:
            # 缺省落到当前工作目录（仅测试场景使用，生产由调用方传入 repo_root）
            self.watermark_path = Path.cwd() / DEFAULT_WATERMARK_DIR / DEFAULT_WATERMARK_FILENAME

    # ------------------------------------------------------------------
    # 水位线读写
    # ------------------------------------------------------------------

    def read_watermark(self) -> float:
        """读取水位线（epoch）。文件不存在返回 0.0。"""
        if not self.watermark_path.is_file():
            return 0.0
        try:
            data = json.loads(self.watermark_path.read_text(encoding="utf-8"))
            return float(data.get("mtime_waterline", 0.0))
        except (OSError, json.JSONDecodeError, ValueError, TypeError) as exc:
            logger.warning("读取水位线失败，重置为 0: %s", exc)
            return 0.0

    def write_watermark(self, mtime: float) -> None:
        """写入水位线。父目录自动创建。"""
        self.watermark_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"mtime_waterline": mtime, "updated_at": _utcnow_iso()}
        # 原子写
        tmp = self.watermark_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.watermark_path)

    # ------------------------------------------------------------------
    # 增量采集
    # ------------------------------------------------------------------

    def collect(
        self,
        *,
        max_sessions: int | None = None,
        update_watermark: bool = True,
    ) -> IncrementalCollectResult:
        """执行一次增量采集。

        - max_sessions：单次最多返回的新会话数（None=不限）
        - update_watermark=True：采集后更新水位线为最新 mtime
          （若设为 False，调用方需手动 write_watermark）

        返回 IncrementalCollectResult。
        """
        before = self.read_watermark()
        all_metas = self.provider.list_sessions(since=None)
        # 严格大于水位线（mtime == 水位线视为已采集）
        new_metas = [m for m in all_metas if m.mtime > before]
        skipped = len(all_metas) - len(new_metas)
        if max_sessions is not None and max_sessions > 0:
            new_metas = new_metas[:max_sessions]
        # 水位线更新为本次采集到的最大 mtime（而非全部会话的最大 mtime，
        # 避免 max_sessions 截断后跳过后续会话）
        after = before
        if new_metas:
            after = max(m.mtime for m in new_metas)
        if update_watermark and after > before:
            self.write_watermark(after)
        return IncrementalCollectResult(
            new_sessions=new_metas,
            skipped_count=skipped,
            watermark_before=before,
            watermark_after=after,
        )

    def collect_full_sessions(
        self,
        *,
        max_sessions: int | None = None,
    ) -> list[Session]:
        """增量采集并直接读取完整会话内容。

        便捷方法：collect() + 对每个新会话 read_session。
        失败的 read_session 记录 warning 并跳过，不中断整体采集。
        """
        result = self.collect(max_sessions=max_sessions)
        sessions: list[Session] = []
        for meta in result.new_sessions:
            try:
                session = self.provider.read_session(meta.session_id)
            except (FileNotFoundError, OSError) as exc:
                logger.warning(
                    "读取会话 %s 失败，跳过: %s",
                    meta.session_id,
                    exc,
                )
                continue
            sessions.append(session)
        return sessions

    # ------------------------------------------------------------------
    # 重置（测试 / 重新全量采集用）
    # ------------------------------------------------------------------

    def reset_watermark(self) -> None:
        """重置水位线为 0（下次采集返回全部会话）。"""
        self.write_watermark(0.0)


def _utcnow_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


__all__ = [
    "DEFAULT_WATERMARK_DIR",
    "DEFAULT_WATERMARK_FILENAME",
    "IncrementalCollectResult",
    "IncrementalCollector",
]
