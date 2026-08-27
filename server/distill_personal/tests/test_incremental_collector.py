"""SubTask 7.2 — 对话记录增量采集测试。

验证点：
- 增量采集只处理新会话（基于 mtime 水位线）
- 水位线持久化到 watermark.json
- 首次采集返回全部会话
- 第二次采集返回 0（无新会话）
- 新增会话后再次采集返回新会话
- collect_full_sessions 读取完整内容
- reset_watermark 重置后返回全部
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from server.distill_personal.incremental_collector import IncrementalCollector
from server.distill_personal.session_provider import GenericJsonlSessionProvider


@pytest.fixture
def collector(tmp_path: Path, fake_trae_sessions_dir: Path) -> IncrementalCollector:
    """构造增量采集器，水位线落 tmp_path。"""
    provider = GenericJsonlSessionProvider(fake_trae_sessions_dir)
    watermark_path = tmp_path / "watermark.json"
    return IncrementalCollector(provider, watermark_path=watermark_path)


def test_first_collect_returns_all(collector: IncrementalCollector):
    """首次采集返回全部会话。"""
    result = collector.collect()
    assert result.new_count == 3
    assert result.skipped_count == 0
    assert result.watermark_before == 0.0
    assert result.watermark_after > 0.0


def test_second_collect_returns_zero(collector: IncrementalCollector):
    """第二次采集无新会话。"""
    collector.collect()
    result = collector.collect()
    assert result.new_count == 0
    assert result.skipped_count == 3


def test_new_session_picked_up(
    collector: IncrementalCollector,
    fake_trae_sessions_dir: Path,
):
    """新增会话后再次采集返回新会话。"""
    collector.collect()
    # 等一小段时间确保 mtime 更新
    time.sleep(0.05)
    new_session = fake_trae_sessions_dir / "session-004.jsonl"
    new_session.write_text(
        json.dumps({"role": "user", "content": "新会话"}) + "\n",
        encoding="utf-8",
    )
    result = collector.collect()
    assert result.new_count == 1
    assert result.new_sessions[0].session_id == "session-004"


def test_watermark_persisted(collector: IncrementalCollector):
    """水位线持久化到 watermark.json。"""
    collector.collect()
    # 重新构造 collector，水位线应能读取
    new_collector = IncrementalCollector(
        collector.provider,
        watermark_path=collector.watermark_path,
    )
    assert new_collector.read_watermark() == collector.read_watermark()


def test_watermark_corrupt_resets_to_zero(collector: IncrementalCollector):
    """水位线文件损坏时重置为 0。"""
    collector.watermark_path.parent.mkdir(parents=True, exist_ok=True)
    collector.watermark_path.write_text("not a json", encoding="utf-8")
    assert collector.read_watermark() == 0.0


def test_collect_full_sessions(collector: IncrementalCollector):
    """collect_full_sessions 返回完整 Session 列表。"""
    sessions = collector.collect_full_sessions()
    assert len(sessions) == 3
    assert all(hasattr(s, "turns") for s in sessions)


def test_reset_watermark(collector: IncrementalCollector):
    """reset 后再次采集返回全部。"""
    collector.collect()
    collector.reset_watermark()
    result = collector.collect()
    assert result.new_count == 3


def test_max_sessions_limit(collector: IncrementalCollector):
    """max_sessions 限制单次返回数量。"""
    result = collector.collect(max_sessions=2)
    assert result.new_count == 2
    # 水位线更新为本次返回的最大 mtime（非全部会话的最大 mtime）
    # 第三次会话下次还能采集到
    result2 = collector.collect()
    assert result2.new_count == 1


def test_update_watermark_false(collector: IncrementalCollector):
    """update_watermark=False 时不更新水位线。"""
    result1 = collector.collect(update_watermark=False)
    assert result1.watermark_after > 0.0
    # 水位线未更新，下次仍能采集全部
    result2 = collector.collect()
    assert result2.new_count == 3
