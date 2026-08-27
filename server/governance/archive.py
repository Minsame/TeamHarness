"""SemanticArchiveService — 语义归档服务（SubTask 9.2）。

对应技术方案 3.2.2 + 关键风险提醒（🟡 黄线）：
- 语义归档必须移入 archive/<date>/，6 个月后删除文件
- 禁止直接文件删除（必须先归档，再按 TTL 清理）

设计要点：
- 归档动作：将资产内容快照写入 archive/<YYYY-MM-DD>/<asset_id>.md
- 归档元数据：append 到 archive/_manifest.json（含 hard_delete_at = archived_at + 6 月）
- 资产状态：asset_index.status 置为 'deleted'（召回双重过滤自动排除）
- 物理删除：cleanup_expired() 扫描 manifest，删除 hard_delete_at 已到的归档文件
  并从 manifest 移除记录（仅删归档副本，不触碰 git 原文件——git 文件由 PR 评审流程处理）
- 不依赖额外 ORM 表（避免侵入 Agent 2 schema），manifest 即归档账本
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import select, update

from server.governance.models import ArchiveRecord, ArchiveResult
from server.infra_db.db import Database
from server.infra_db.models import AssetIndex as AssetIndexRow

logger = logging.getLogger(__name__)


# 归档保留期（6 个月，对应技术方案黄线）
ARCHIVE_TTL_MONTHS = 6
# 近似换算为天（6 * 30 = 180 天），用于 hard_delete_at 计算
ARCHIVE_TTL_DAYS = 180
# manifest 文件名
MANIFEST_FILENAME = "_manifest.json"


class SemanticArchiveService:
    """语义归档服务。

    用法：
        svc = SemanticArchiveService(database, archive_root=Path("./archive"))
        # 归档单条（语义归并后的重复资产）
        record = svc.archive_asset(asset_id="rule-x", reason="semantic_merge", merged_into="rule-y")
        # 批量归档
        result = svc.archive_batch(["rule-a", "rule-b"], reason="semantic_merge")
        # 清理过期归档（cron 调用）
        cleaned = svc.cleanup_expired()
    """

    def __init__(
        self,
        database: Database,
        *,
        archive_root: Path | str = "archive",
        ttl_days: int = ARCHIVE_TTL_DAYS,
    ) -> None:
        self._db = database
        self._archive_root = Path(archive_root)
        self._ttl_days = ttl_days

    # ------------------------------------------------------------------
    # 对外契约 API
    # ------------------------------------------------------------------

    def archive_asset(
        self,
        *,
        asset_id: str,
        reason: str = "semantic_merge",
        merged_into: str | None = None,
    ) -> ArchiveRecord:
        """归档单条资产。

        流程：
        1. 读取 asset_index 行（含 content_snapshot）
        2. 写入 archive/<YYYY-MM-DD>/<asset_id>.md
        3. append 归档记录到 manifest（含 hard_delete_at）
        4. 更新 asset_index.status = 'deleted'（召回自动排除）

        禁止直接删除 git 原文件（黄线遵守）。
        """
        with self._db.session() as sess:
            row = sess.get(AssetIndexRow, asset_id)
            if row is None:
                raise ValueError(f"资产不存在：{asset_id}")
            # 已归档/已删除 → 幂等返回已有记录
            if row.status == "deleted":
                existing = self._find_manifest_record(asset_id)
                if existing is not None:
                    return existing
            content = row.content_snapshot or ""
            original_path = row.git_path
            # 标记为 deleted（同事务）
            sess.execute(
                update(AssetIndexRow)
                .where(AssetIndexRow.id == asset_id)
                .values(status="deleted")
            )

        now = datetime.now(timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        archive_dir = self._archive_root / date_str
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive_file = archive_dir / f"{asset_id}.md"
        # 写入归档副本（若已存在不覆盖，幂等）
        if not archive_file.exists():
            archive_file.write_text(content, encoding="utf-8")

        hard_delete_at = now + timedelta(days=self._ttl_days)
        record = ArchiveRecord(
            asset_id=asset_id,
            original_path=original_path,
            archive_path=str(archive_file.relative_to(self._archive_root)),
            archived_at=now,
            hard_delete_at=hard_delete_at,
            reason=reason,
        )
        # merged_into 记入 extra（ArchiveRecord 无此字段，用 reason 携带）
        extra_meta: dict[str, Any] = {}
        if merged_into:
            extra_meta["merged_into"] = merged_into

        self._append_manifest(record, extra_meta)
        logger.info(
            "资产归档完成 asset_id=%s archive_path=%s hard_delete_at=%s",
            asset_id, record.archive_path, hard_delete_at.isoformat(),
        )
        return record

    def archive_batch(
        self,
        asset_ids: list[str],
        *,
        reason: str = "semantic_merge",
        merged_into_map: dict[str, str] | None = None,
    ) -> ArchiveResult:
        """批量归档。"""
        result = ArchiveResult()
        merged_into_map = merged_into_map or {}
        for asset_id in asset_ids:
            try:
                record = self.archive_asset(
                    asset_id=asset_id,
                    reason=reason,
                    merged_into=merged_into_map.get(asset_id),
                )
                result.archived.append(record)
            except Exception as exc:  # noqa: BLE001
                logger.warning("归档失败 asset_id=%s err=%s", asset_id, exc)
                result.failed.append({"asset_id": asset_id, "error": str(exc)})
        return result

    def cleanup_expired(self) -> int:
        """清理过期归档文件（hard_delete_at 已到）。

        仅删除 archive/<date>/<asset_id>.md 归档副本，
        不触碰 git 原文件（git 文件由 PR 评审流程处理）。
        返回清理的文件数。
        """
        manifest = self._read_manifest()
        if not manifest:
            return 0
        now = datetime.now(timezone.utc)
        kept: list[dict[str, Any]] = []
        cleaned = 0
        for entry in manifest:
            hard_delete_str = entry.get("hard_delete_at", "")
            if not hard_delete_str:
                kept.append(entry)
                continue
            try:
                hard_delete_at = datetime.fromisoformat(hard_delete_str)
            except (ValueError, TypeError):
                kept.append(entry)
                continue
            if hard_delete_at <= now:
                # 删除归档副本文件
                archive_path = entry.get("archive_path", "")
                if archive_path:
                    full_path = self._archive_root / archive_path
                    try:
                        if full_path.is_file():
                            full_path.unlink()
                            cleaned += 1
                            logger.info(
                                "清理过期归档 asset_id=%s path=%s",
                                entry.get("asset_id", ""), archive_path,
                            )
                    except OSError as exc:
                        logger.warning(
                            "删除归档文件失败 path=%s err=%s", full_path, exc
                        )
            else:
                kept.append(entry)
        self._write_manifest(kept)
        return cleaned

    def list_archived(self) -> list[ArchiveRecord]:
        """列出全部归档记录（含未过期的）。"""
        manifest = self._read_manifest()
        records: list[ArchiveRecord] = []
        for entry in manifest:
            try:
                records.append(
                    ArchiveRecord(
                        asset_id=entry.get("asset_id", ""),
                        original_path=entry.get("original_path", ""),
                        archive_path=entry.get("archive_path", ""),
                        archived_at=datetime.fromisoformat(
                            entry.get("archived_at", "")
                        ),
                        hard_delete_at=datetime.fromisoformat(
                            entry.get("hard_delete_at", "")
                        ),
                        reason=entry.get("reason", "semantic_merge"),
                    )
                )
            except (ValueError, TypeError):
                continue
        return records

    def find_pending_cleanup(self) -> list[ArchiveRecord]:
        """列出即将/已到期的归档记录（治理看板告警源）。"""
        now = datetime.now(timezone.utc)
        return [r for r in self.list_archived() if r.hard_delete_at <= now]

    # ------------------------------------------------------------------
    # 内部：manifest 读写
    # ------------------------------------------------------------------

    def _manifest_path(self) -> Path:
        return self._archive_root / MANIFEST_FILENAME

    def _read_manifest(self) -> list[dict[str, Any]]:
        path = self._manifest_path()
        if not path.is_file():
            return []
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return data
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("manifest 读取失败 path=%s err=%s", path, exc)
        return []

    def _write_manifest(self, entries: list[dict[str, Any]]) -> None:
        self._archive_root.mkdir(parents=True, exist_ok=True)
        path = self._manifest_path()
        # 原子写：先临时文件再替换
        tmp = path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp, path)

    def _append_manifest(
        self, record: ArchiveRecord, extra: dict[str, Any] | None = None
    ) -> None:
        entries = self._read_manifest()
        # 移除同 asset_id 旧记录（幂等）
        entries = [e for e in entries if e.get("asset_id") != record.asset_id]
        entry: dict[str, Any] = {
            "asset_id": record.asset_id,
            "original_path": record.original_path,
            "archive_path": record.archive_path,
            "archived_at": record.archived_at.isoformat(),
            "hard_delete_at": record.hard_delete_at.isoformat(),
            "reason": record.reason,
        }
        if extra:
            entry["extra"] = extra
        entries.append(entry)
        self._write_manifest(entries)

    def _find_manifest_record(self, asset_id: str) -> ArchiveRecord | None:
        entries = self._read_manifest()
        for entry in entries:
            if entry.get("asset_id") == asset_id:
                try:
                    return ArchiveRecord(
                        asset_id=asset_id,
                        original_path=entry.get("original_path", ""),
                        archive_path=entry.get("archive_path", ""),
                        archived_at=datetime.fromisoformat(
                            entry.get("archived_at", "")
                        ),
                        hard_delete_at=datetime.fromisoformat(
                            entry.get("hard_delete_at", "")
                        ),
                        reason=entry.get("reason", "semantic_merge"),
                    )
                except (ValueError, TypeError):
                    return None
        return None


__all__ = [
    "ARCHIVE_TTL_DAYS",
    "ARCHIVE_TTL_MONTHS",
    "MANIFEST_FILENAME",
    "SemanticArchiveService",
]
