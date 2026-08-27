"""REM 阶段 — 意图归纳，区分一次性上下文 vs 可复用经验。

对应 SubTask 7.4 + 技术方案 3.3.2 ② REM 反思：
- 对候选轮次做意图归纳："用户实际想沉淀的是什么？"
- 识别重复出现的模式（如多次纠正 AI 同一错误 → 规则）
- 区分"一次性任务上下文" vs "可复用经验"
- 写入本地 .dreams/rem/（不入 git，纯临时）

REM 阶段不产出资产，只产出"意图"（intents）。
intent = 归纳后的可复用经验描述，含原始 signal 引用与归类判断。

intent 结构：
    {
      "intent_id": str,
      "description": str,        # 意图描述（"用户反复强调提交前跑 lint"）
      "candidate_type": str,     # rule / memory / skill / tool
      "reusable": bool,          # True=可复用经验，False=一次性上下文
      "reuse_hint": str,         # 可复用性判断理由
      "source_signal_ids": [str],
      "pattern_count": int,      # 重复出现次数（跨会话）
      "metadata": {...}
    }

设计要点：
- 规则启发式：关键词命中"这次"/"这个 bug"/"刚才" → 倾向一次性上下文
- 跨会话重复检测：相同 candidate_type + 关键词相似 → pattern_count++
- reusable=False 的 intent 不进入 Deep 阶段（直接丢弃）
- LLM 精筛可选（成本控制）：规则启发式 + LLM 复核
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from server.distill_personal.light_stage import Signal

logger = logging.getLogger(__name__)

DEFAULT_REM_DIR = Path(".teamharness-local") / "dreams" / "rem"

# 一次性上下文关键词（命中则倾向 reusable=False）
_ONE_TIME_KEYWORDS: tuple[str, ...] = (
    "这次", "这个 bug", "刚才", "临时", "暂时", "今天",
    "this time", "just now", "today", "tmp",
)


# ---------------------------------------------------------------------------
# intent 数据结构
# ---------------------------------------------------------------------------


@dataclass
class Intent:
    """REM 阶段产出的意图。"""

    intent_id: str
    description: str
    candidate_type: str  # rule / memory / skill / tool
    reusable: bool = True
    reuse_hint: str = ""
    source_signal_ids: list[str] = field(default_factory=list)
    pattern_count: int = 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "intent_id": self.intent_id,
            "description": self.description,
            "candidate_type": self.candidate_type,
            "reusable": self.reusable,
            "reuse_hint": self.reuse_hint,
            "source_signal_ids": list(self.source_signal_ids),
            "pattern_count": self.pattern_count,
            "metadata": dict(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Intent":
        return cls(
            intent_id=str(data.get("intent_id", "")),
            description=str(data.get("description", "")),
            candidate_type=str(data.get("candidate_type", "memory")),
            reusable=bool(data.get("reusable", True)),
            reuse_hint=str(data.get("reuse_hint", "")),
            source_signal_ids=list(data.get("source_signal_ids") or []),
            pattern_count=int(data.get("pattern_count", 1)),
            metadata=dict(data.get("metadata") or {}),
        )


@dataclass
class RemStageResult:
    """REM 阶段结果。"""

    intents: list[Intent] = field(default_factory=list)
    discarded_count: int = 0  # 被判为一次性上下文丢弃的数量
    pattern_detected_count: int = 0  # 跨会话重复模式数量

    @property
    def intent_count(self) -> int:
        return len(self.intents)

    @property
    def reusable_count(self) -> int:
        return sum(1 for i in self.intents if i.reusable)


# ---------------------------------------------------------------------------
# REM 阶段主流程
# ---------------------------------------------------------------------------


class RemStage:
    """REM 阶段：意图归纳 + 一次性 vs 可复用区分。

    使用：
        stage = RemStage()
        result = stage.run(signals)
        # result.intents → Deep 阶段输入（仅 reusable=True 的）
    """

    def __init__(
        self,
        *,
        min_pattern_count: int = 1,
        discard_one_time: bool = True,
    ) -> None:
        self.min_pattern_count = min_pattern_count
        self.discard_one_time = discard_one_time

    def run(self, signals: list[Signal]) -> RemStageResult:
        """对信号列表执行 REM 阶段。

        返回 RemStageResult，含 intents 列表（仅 reusable=True 的）。
        """
        if not signals:
            return RemStageResult()

        # 1. 按相似度聚类（相同 candidate_type + 关键词相似）
        clusters = self._cluster_signals(signals)
        # 2. 对每簇做意图归纳
        intents: list[Intent] = []
        discarded = 0
        pattern_detected = 0
        for cluster in clusters:
            intent = self._induce_intent(cluster)
            if intent is None:
                discarded += 1
                continue
            if intent.pattern_count > 1:
                pattern_detected += 1
            if not intent.reusable and self.discard_one_time:
                discarded += 1
                continue
            intents.append(intent)
        return RemStageResult(
            intents=intents,
            discarded_count=discarded,
            pattern_detected_count=pattern_detected,
        )

    # ------------------------------------------------------------------
    # 聚类（简化：相同 candidate_type + 关键词集合相似）
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_keywords(text: str, topk: int = 5) -> set[str]:
        """从文本提取关键词集合（简化：取长度>=2 的中文/英文词片段）。"""
        words: set[str] = set()
        # 英文词（长度 >= 3）
        for m in re.finditer(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
            words.add(m.group().lower())
        # 中文连续片段（取前 3 字作为关键词）
        for c in re.findall(r"[\u4e00-\u9fa5]{2,}", text):
            words.add(c[:3])
        # 限制 topk 个
        return set(list(words)[:topk])

    def _cluster_signals(self, signals: list[Signal]) -> list[list[Signal]]:
        """按 candidate_type + 关键词集合相似度聚类。

        简化策略：相同 candidate_type 且关键词集合有交集 → 同簇。
        不做严格聚类（避免依赖 sklearn 等重依赖），适合本地轻量运行。
        """
        clusters: list[list[Signal]] = []
        # 预计算每个 signal 的关键词集合，与 clusters 同步索引
        cluster_keywords: list[set[str]] = []
        for sig in signals:
            kws = self._extract_keywords(sig.content_excerpt)
            placed = False
            for idx, cluster in enumerate(clusters):
                ref_kws = cluster_keywords[idx]
                # 与簇中第一个 signal 比较
                if (
                    ref_kws & kws
                    and cluster[0].candidate_type == sig.candidate_type
                ):
                    cluster.append(sig)
                    # 合并关键词到簇（提升后续匹配召回）
                    cluster_keywords[idx] |= kws
                    placed = True
                    break
            if not placed:
                clusters.append([sig])
                cluster_keywords.append(kws)
        return clusters

    # ------------------------------------------------------------------
    # 意图归纳
    # ------------------------------------------------------------------

    def _induce_intent(self, cluster: list[Signal]) -> Intent | None:
        """对一簇信号做意图归纳。"""
        if not cluster:
            return None
        first = cluster[0]
        cand_type = first.candidate_type
        # description：取簇中 confidence 最高的 signal 的 content_excerpt 前 100 字
        best = max(cluster, key=lambda s: s.confidence)
        description = best.content_excerpt[:100].replace("\n", " ").strip()
        if not description:
            description = f"（{cand_type} 候选，无内容摘要）"

        # 一次性上下文判断
        combined_text = " ".join(s.content_excerpt for s in cluster)
        is_one_time = any(kw in combined_text for kw in _ONE_TIME_KEYWORDS)
        reusable = not is_one_time
        reuse_hint = (
            "一次性上下文（含 this time / 刚才 等关键词）" if is_one_time else "可复用经验"
        )

        # pattern_count：簇大小 + 跨会话数
        pattern_count = len(cluster)
        cross_session = len({s.session_id for s in cluster})
        if cross_session > 1:
            reuse_hint += f"（跨 {cross_session} 个会话重复出现）"

        return Intent(
            intent_id=str(uuid.uuid4()),
            description=description,
            candidate_type=cand_type,
            reusable=reusable,
            reuse_hint=reuse_hint,
            source_signal_ids=[s.signal_id for s in cluster],
            pattern_count=pattern_count,
            metadata={
                "best_confidence": best.confidence,
                "cross_session_count": cross_session,
                "cluster_size": len(cluster),
            },
        )


# ---------------------------------------------------------------------------
# intent 持久化
# ---------------------------------------------------------------------------


def save_intents(
    intents: list[Intent],
    *,
    repo_root: Path | None = None,
    rem_dir: Path | None = None,
) -> Path:
    """把 intents 写入 .dreams/rem/intents.json。"""
    if rem_dir is not None:
        target_dir = rem_dir
    elif repo_root is not None:
        target_dir = repo_root / DEFAULT_REM_DIR
    else:
        target_dir = Path.cwd() / DEFAULT_REM_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / "intents.json"
    payload = {
        "saved_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "count": len(intents),
        "intents": [i.to_dict() for i in intents],
    }
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
    return path


def load_intents(
    *,
    repo_root: Path | None = None,
    rem_dir: Path | None = None,
) -> list[Intent]:
    """从 .dreams/rem/intents.json 加载 intents。"""
    if rem_dir is not None:
        path = rem_dir / "intents.json"
    elif repo_root is not None:
        path = repo_root / DEFAULT_REM_DIR / "intents.json"
    else:
        path = Path.cwd() / DEFAULT_REM_DIR / "intents.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("加载 intents 失败: %s", exc)
        return []
    raw = data.get("intents") or []
    return [Intent.from_dict(i) for i in raw if isinstance(i, dict)]


__all__ = [
    "DEFAULT_REM_DIR",
    "Intent",
    "RemStage",
    "RemStageResult",
    "load_intents",
    "save_intents",
]
