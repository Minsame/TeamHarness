"""SessionProvider 抽象 — 一级提炼的对话记录适配层。

对应 SubTask 7.1 + 技术方案 4.3：
- 抽象接口 list_sessions / read_session / is_completed
- TraeSessionProvider：复用 Agent 1 的 discover_sessions_root + list_trae_sessions
  读取 .trae-cn/sessions/*.jsonl
- GenericJsonlSessionProvider：通用 JSONL 兜底，每行一条消息（json with role/content/ts）
- 通过 mapping.yaml 的 session.target 配置切换，或显式参数 create_session_provider(target=...)

设计要点：
- 复用 Agent 1（infra_git/trae_adapter.py）的 discover_sessions_root 与
  list_trae_sessions，避免重复实现 OS 路径探测
- 统一输出内部 Session / SessionTurn 结构（含轮次、角色、时间戳、工具调用记录）
- 增量采集：基于 since 时间戳，只读取新会话
- 隐私：只在本机读取与提炼，不上传原始对话内容
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from server.infra_git.trae_adapter import (
    discover_sessions_root,
    list_trae_sessions,
)

if TYPE_CHECKING:
    # 仅用于类型注解，运行时不导入以避免循环依赖：
    # session_provider ← coding_adapters ← session_provider
    from server.coding_adapters.registry import (
        CodingSoftwareRegistry,
        InstalledSoftware,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 内部统一结构
# ---------------------------------------------------------------------------


@dataclass
class SessionTurn:
    """单轮对话（统一内部表示，屏蔽不同 coding 软件格式差异）。"""

    role: str  # user / assistant / system / tool
    content: str = ""
    timestamp: str = ""  # ISO 字符串，原始格式各软件可能不同
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    # 额外元数据（如模型名、tokens 等）
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class Session:
    """完整会话（统一内部表示）。"""

    session_id: str
    turns: list[SessionTurn] = field(default_factory=list)
    started_at: str = ""
    ended_at: str = ""
    # 会话文件路径（用于增量采集的水位线比对，原始路径不上传）
    source_path: str = ""
    # 是否已完成（非进行中）
    completed: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def turn_count(self) -> int:
        return len(self.turns)

    def to_dict(self) -> dict[str, Any]:
        return {
            "session_id": self.session_id,
            "turns": [
                {
                    "role": t.role,
                    "content": t.content,
                    "timestamp": t.timestamp,
                    "tool_calls": list(t.tool_calls),
                    "metadata": dict(t.metadata),
                }
                for t in self.turns
            ],
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "completed": self.completed,
            "turn_count": self.turn_count,
            "metadata": dict(self.metadata),
        }


@dataclass
class SessionMeta:
    """会话元数据（list_sessions 返回，不含正文，节省内存）。"""

    session_id: str
    source_path: str
    mtime: float  # 文件修改时间（epoch）
    size: int = 0
    completed: bool = True


# ---------------------------------------------------------------------------
# SessionProvider 抽象
# ---------------------------------------------------------------------------


class SessionProvider(Protocol):
    """对话记录适配器接口（技术方案 4.3）。"""

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        """列出会话（按 mtime 升序）。

        since 为 epoch 时间戳，仅返回 mtime >= since 的会话（增量采集）。
        """
        ...

    def read_session(self, session_id: str) -> Session:
        """读取完整会话内容。"""
        ...

    def is_completed(self, session_id: str) -> bool:
        """会话是否已完成（非进行中）。"""
        ...


# ---------------------------------------------------------------------------
# TraeSessionProvider — 复用 Agent 1 的 Trae 适配
# ---------------------------------------------------------------------------


class TraeSessionProvider:
    """Trae 会话 Provider。

    复用 Agent 1（infra_git/trae_adapter.py）的：
    - discover_sessions_root()：按 OS 自动探测 .trae-cn/sessions/
    - list_trae_sessions(since)：列出 *.jsonl，按 mtime 升序

    Trae 会话文件格式（*.jsonl）：每行一条 JSON 消息，常见字段：
    - role：user / assistant / system / tool
    - content：消息正文
    - timestamp：ISO 字符串或 epoch
    - tool_calls：assistant 调用工具的详情（可选）

    由于 Trae 实际会话 schema 可能随版本变化，本实现采用宽松解析策略：
    - 每行 JSON 必须能解析，否则跳过（记录 warning）
    - role/content 字段缺失时用默认值
    - 其余字段透传到 SessionTurn.metadata
    """

    PROVIDER_NAME = "trae"

    def __init__(self, sessions_root: Path | None = None) -> None:
        """初始化。

        sessions_root 为 None 时自动探测（discover_sessions_root）。
        显式传入用于测试或自定义安装路径。
        """
        self._root = sessions_root

    def _resolve_root(self) -> Path | None:
        """解析会话根目录（显式 > 自动探测）。"""
        if self._root is not None:
            return self._root if self._root.is_dir() else None
        return discover_sessions_root()

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        root = self._resolve_root()
        if root is None:
            return []
        # 复用 Agent 1 的 list_trae_sessions，按 mtime 升序
        paths = list_trae_sessions(since=since)
        metas: list[SessionMeta] = []
        for p in paths:
            try:
                stat = p.stat()
            except OSError:
                continue
            metas.append(
                SessionMeta(
                    session_id=p.stem,  # 文件名去掉 .jsonl
                    source_path=str(p),
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    completed=True,  # Trae 会话文件落盘即视为已完成
                )
            )
        return metas

    def read_session(self, session_id: str) -> Session:
        root = self._resolve_root()
        if root is None:
            raise FileNotFoundError(f"Trae sessions root 未找到，无法读取 {session_id}")
        path = root / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"会话文件不存在: {path}")
        return self._parse_jsonl_file(path, session_id)

    def is_completed(self, session_id: str) -> bool:
        # Trae 会话文件落盘即视为已完成（进行中的会话由 Trae 在内存中维护）
        root = self._resolve_root()
        if root is None:
            return False
        return (root / f"{session_id}.jsonl").is_file()

    # ------------------------------------------------------------------
    # 内部解析
    # ------------------------------------------------------------------

    @staticmethod
    def _parse_jsonl_file(path: Path, session_id: str) -> Session:
        """解析 Trae *.jsonl 会话文件，每行一条 JSON 消息。

        宽松解析策略：
        - 空行 / 非法 JSON 跳过
        - role 缺失默认 "user"，content 缺失默认 ""
        - timestamp 字段优先取 "timestamp" / "ts" / "time" / "@timestamp"
        - tool_calls 字段优先取 "tool_calls" / "toolCalls"
        - 其余字段透传到 metadata
        """
        turns: list[SessionTurn] = []
        started_at = ""
        ended_at = ""
        first_ts_found = False
        with path.open("r", encoding="utf-8") as f:
            for line_no, raw in enumerate(f, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "Trae 会话 %s 第 %d 行非合法 JSON，跳过: %s",
                        path.name,
                        line_no,
                        exc,
                    )
                    continue
                if not isinstance(obj, dict):
                    continue
                role = str(obj.get("role", "user")).lower()
                content = str(obj.get("content", ""))
                # timestamp 候选键
                ts = ""
                for key in ("timestamp", "ts", "time", "@timestamp"):
                    if key in obj:
                        ts = str(obj[key])
                        break
                if ts and not first_ts_found:
                    started_at = ts
                    first_ts_found = True
                if ts:
                    ended_at = ts
                # tool_calls 候选键
                tool_calls: list[dict[str, Any]] = []
                for key in ("tool_calls", "toolCalls"):
                    val = obj.get(key)
                    if isinstance(val, list):
                        tool_calls = [v for v in val if isinstance(v, dict)]
                        break
                # metadata：剔除已知键后透传
                known_keys = {"role", "content", "timestamp", "ts", "time", "@timestamp",
                              "tool_calls", "toolCalls"}
                metadata = {k: v for k, v in obj.items() if k not in known_keys}
                turns.append(
                    SessionTurn(
                        role=role,
                        content=content,
                        timestamp=ts,
                        tool_calls=tool_calls,
                        metadata=metadata,
                    )
                )
        return Session(
            session_id=session_id,
            turns=turns,
            started_at=started_at,
            ended_at=ended_at,
            source_path=str(path),
            completed=True,
        )


# ---------------------------------------------------------------------------
# GenericJsonlSessionProvider — 通用 JSONL 兜底
# ---------------------------------------------------------------------------


class GenericJsonlSessionProvider:
    """通用 JSONL 会话 Provider（兜底实现）。

    适用场景：用户自定义 coding 软件、CI 测试、非 Trae 环境。
    与 TraeSessionProvider 的差异：
    - 不依赖 .trae-cn 路径，由显式参数指定会话目录
    - 文件命名仍为 *.jsonl，每行一条 JSON 消息
    - 解析逻辑与 TraeSessionProvider 一致（复用 _parse_jsonl_file）

    通过 create_session_provider(target="generic", sessions_root=Path(...)) 创建。
    """

    PROVIDER_NAME = "generic"

    def __init__(self, sessions_root: Path) -> None:
        if not sessions_root.is_dir():
            raise FileNotFoundError(f"会话目录不存在: {sessions_root}")
        self._root = sessions_root

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        metas: list[SessionMeta] = []
        for p in self._root.glob("*.jsonl"):
            if not p.is_file():
                continue
            try:
                stat = p.stat()
            except OSError:
                continue
            if since is not None and stat.st_mtime < since:
                continue
            metas.append(
                SessionMeta(
                    session_id=p.stem,
                    source_path=str(p),
                    mtime=stat.st_mtime,
                    size=stat.st_size,
                    completed=True,
                )
            )
        metas.sort(key=lambda m: m.mtime)
        return metas

    def read_session(self, session_id: str) -> Session:
        path = self._root / f"{session_id}.jsonl"
        if not path.is_file():
            raise FileNotFoundError(f"会话文件不存在: {path}")
        return TraeSessionProvider._parse_jsonl_file(path, session_id)

    def is_completed(self, session_id: str) -> bool:
        return (self._root / f"{session_id}.jsonl").is_file()


# ---------------------------------------------------------------------------
# ConversationLogSessionProvider — Task 28：从 async_comm ConversationLog 适配
# ---------------------------------------------------------------------------


# ConversationEvent.event_type → SessionTurn.role 映射
# ask → user（提问方）；realtime_answer/simulated_answer/confirmed/revised → assistant（回答方）
# needs_human_review → assistant（回答方触发的人工审核标记）
_EVENT_TYPE_TO_ROLE: dict[str, str] = {
    "ask": "user",
    "realtime_answer": "assistant",
    "simulated_answer": "assistant",
    "confirmed": "assistant",
    "revised": "assistant",
    "needs_human_review": "assistant",
}


class ConversationLogSessionProvider:
    """从 :class:`ConversationLog` 适配为 :class:`SessionProvider`（Task 28）。

    将 peer 间交流事件（ask / answer / needs_human_review）转换为统一的
    Session / SessionTurn 结构，供 Light/REM/Deep 三阶段提炼使用。

    - :meth:`list_sessions`：按 ``peer_id`` 分组，每个 peer 一个 Session
      （``session_id`` = ``peer_id``，含该 peer 的全部事件）
    - :meth:`read_session`：读取指定 peer 的全部事件，按时间升序转为 SessionTurn
    - :meth:`is_completed`：对话状态为 ``active`` / ``paused`` / ``timeout_disconnect``
      时返回 ``False``（进行中或中断未恢复），``resumed`` 或无状态时返回 ``True``（可提炼）

    SessionTurn.metadata 携带协作信号字段（供 Light 阶段加权）：
    - ``event_type``：原始事件类型（ask / realtime_answer / ... ）
    - ``degraded``：影子联络标记
    - ``realtime``：实时通信标记
    - ``conversation_state``：对话状态
    - ``tag_routing``：跨职能路由原因（若 payload 中存在）
    - ``needs_human_review``：是否需人工审核（``event_type=needs_human_review`` 时为 ``True``）

    隐私：仅在本机读取 ConversationLog，不上传原始对话内容（遵循 PrivacyGuard）。
    """

    PROVIDER_NAME = "conversation_log"

    def __init__(
        self,
        conversation_log: Any,
        *,
        member_id: str = "",
    ) -> None:
        """初始化。

        Args:
            conversation_log: :class:`ConversationLog` 实例（已由 Task 15/27 实现）。
            member_id: 本成员 ID（仅用于日志，不参与会话分组）。
        """
        self._log = conversation_log
        self._member_id = member_id

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        """列出会话（按 peer_id 分组，每个 peer 一个 Session）。

        since 参数语义对齐 SessionProvider Protocol（epoch 时间戳），
        但 ConversationLog 事件以 ISO 字符串记录时间，此处按事件时间字符串
        过滤（since 转为 ISO 比较不直观，故 since 仅作为软过滤：取该 peer
        最新事件时间对应的 epoch >= since）。

        实际场景中 ConversationLog 通常全量提炼，since 一般为 None。
        """
        try:
            all_events = self._log.load_all()
        except Exception as exc:  # noqa: BLE001 — 读取失败不阻断，返回空
            logger.warning("ConversationLog load_all 失败: %s", exc)
            return []

        # 按 peer_id 分组
        peer_events: dict[str, list[Any]] = {}
        for ev in all_events:
            peer_events.setdefault(ev.peer_id, []).append(ev)

        metas: list[SessionMeta] = []
        for peer_id, events in peer_events.items():
            if not events:
                continue
            # 按时间排序取最新事件作为 mtime 基准
            events.sort(key=lambda e: e.timestamp)
            latest = events[-1]
            # ISO 字符串排序与 epoch 排序一致（UTC ISO 8601 字典序 = 时间序）
            mtime = self._iso_to_epoch(latest.timestamp)
            if since is not None and mtime < since:
                continue
            completed = self._is_state_completed(peer_id)
            metas.append(
                SessionMeta(
                    session_id=peer_id,
                    source_path="",  # ConversationLog 无单文件路径
                    mtime=mtime,
                    size=len(events),
                    completed=completed,
                )
            )
        metas.sort(key=lambda m: m.mtime)
        return metas

    def read_session(self, session_id: str) -> Session:
        """读取指定 peer 的全部事件，转为 Session。"""
        try:
            events = self._log.load_by_peer(session_id)
        except Exception as exc:  # noqa: BLE001
            raise FileNotFoundError(
                f"读取 peer '{session_id}' 的对话失败: {exc}"
            ) from exc
        if not events:
            raise FileNotFoundError(f"peer '{session_id}' 无对话事件")
        events.sort(key=lambda e: e.timestamp)
        turns = [self._event_to_turn(ev) for ev in events]
        # 跳过 role 无法识别的事件（如未知 event_type）
        turns = [t for t in turns if t.role in ("user", "assistant")]
        started = events[0].timestamp if events else ""
        ended = events[-1].timestamp if events else ""
        return Session(
            session_id=session_id,
            turns=turns,
            started_at=started,
            ended_at=ended,
            source_path="",
            completed=self._is_state_completed(session_id),
            metadata={"provider": self.PROVIDER_NAME, "event_count": len(events)},
        )

    def is_completed(self, session_id: str) -> bool:
        """会话是否已完成（可提炼）。

        active / paused / timeout_disconnect → False（进行中或中断未恢复）
        resumed / 无状态 → True（已恢复或已结束，可提炼）
        """
        return self._is_state_completed(session_id)

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    def _is_state_completed(self, peer_id: str) -> bool:
        """根据 conversation_state 判断是否可提炼。"""
        try:
            entry = self._log.get_conversation_state(peer_id)
        except Exception:  # noqa: BLE001 — 状态查询失败视为可提炼
            return True
        if entry is None:
            return True
        state = entry.get("state", "")
        # active（进行中）/ paused（中断）/ timeout_disconnect（超时断开）→ 未完成
        if state in ("active", "paused", "timeout_disconnect"):
            return False
        # resumed（已恢复）或其他 → 已完成可提炼
        return True

    @staticmethod
    def _event_to_turn(ev: Any) -> SessionTurn:
        """将 ConversationEvent 转为 SessionTurn。

        - ask → role=user, content=payload.question
        - realtime_answer/simulated_answer/confirmed/revised → role=assistant, content=payload.answer
        - needs_human_review → role=assistant, content=payload.answer 或 payload.question
        """
        role = _EVENT_TYPE_TO_ROLE.get(ev.event_type, "")
        # content 提取：ask 用 question，其余用 answer（needs_human_review 优先 answer）
        if ev.event_type == "ask":
            content = str(ev.payload.get("question", "")) if ev.payload else ""
        else:
            payload = ev.payload or {}
            content = str(payload.get("answer", "") or payload.get("question", ""))

        # metadata 携带协作信号字段
        metadata: dict[str, Any] = {
            "event_type": ev.event_type,
            "degraded": bool(getattr(ev, "degraded", False)),
            "realtime": bool(getattr(ev, "realtime", False)),
            "conversation_state": getattr(ev, "conversation_state", "active"),
            "event_id": ev.event_id,
        }
        # tag_routing 跨职能路由标记（Task 25 写入 payload）
        if ev.payload and ev.payload.get("tag_routing"):
            metadata["tag_routing"] = ev.payload.get("tag_routing")
        # needs_human_review 标记
        if ev.event_type == "needs_human_review":
            metadata["needs_human_review"] = True

        return SessionTurn(
            role=role,
            content=content,
            timestamp=ev.timestamp,
            tool_calls=[],
            metadata=metadata,
        )

    @staticmethod
    def _iso_to_epoch(ts: str) -> float:
        """ISO 时间字符串 → epoch（容错：解析失败返回 0）。"""
        if not ts:
            return 0.0
        try:
            from datetime import datetime
            # 兼容带/不带时区的 ISO 字符串
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return dt.timestamp()
        except (ValueError, TypeError):
            return 0.0


# ---------------------------------------------------------------------------
# MultiSessionProvider — 多 provider 聚合
# ---------------------------------------------------------------------------


class MultiSessionProvider:
    """多 provider 聚合器（实现 :class:`SessionProvider` Protocol）。

    持有多个 ``(prefix, sub_provider)`` 对，将不同 coding 软件的会话统一暴露：
    - :meth:`list_sessions`：合并所有子 provider 的 :class:`SessionMeta`，
      ``session_id`` 加前缀（格式 ``<prefix>:<original_session_id>``），
      按 ``mtime`` 升序排序
    - :meth:`read_session` / :meth:`is_completed`：根据 ``session_id`` 前缀
      路由到对应子 provider（去除前缀后调用）

    前缀分隔符选用 ``:``，因为各 Adapter 的 ``session_id`` 不含该字符：
    - :class:`ClaudeCodeAdapter` 用 ``/``（如 ``proj_a/abc``）
    - :class:`AiderAdapter` 用 ``#``（如 ``aider.chat.history.md#0``）
    - 其他 Adapter 用文件名 stem

    :meth:`str.partition` 只切第一个 ``:``，因此 sub ``session_id`` 中的
    ``/`` 不会被误切。

    允许空 ``providers`` 列表（本机未安装任何软件时）：
    - :meth:`list_sessions` 返回 ``[]``
    - :meth:`read_session` 抛 :class:`FileNotFoundError`
    - :meth:`is_completed` 返回 ``False``
    """

    PREFIX_SEP = ":"

    def __init__(self, providers: list[tuple[str, SessionProvider]]) -> None:
        """初始化。

        Args:
            providers: ``(prefix, sub_provider)`` 列表，prefix 需唯一
                （通常为 :class:`InstalledSoftware.name`）。
        """
        self._providers: list[tuple[str, SessionProvider]] = list(providers)

    def list_sessions(self, since: float | None = None) -> list[SessionMeta]:
        merged: list[SessionMeta] = []
        for prefix, sub in self._providers:
            try:
                sub_metas = sub.list_sessions(since=since)
            except Exception as exc:  # noqa: BLE001 — 子 provider 失败不阻断聚合
                logger.warning(
                    "子 provider '%s' list_sessions 失败，跳过: %s", prefix, exc
                )
                continue
            for meta in sub_metas:
                merged.append(
                    SessionMeta(
                        session_id=f"{prefix}{self.PREFIX_SEP}{meta.session_id}",
                        source_path=meta.source_path,
                        mtime=meta.mtime,
                        size=meta.size,
                        completed=meta.completed,
                    )
                )
        merged.sort(key=lambda m: m.mtime)
        return merged

    def read_session(self, session_id: str) -> Session:
        prefix, sub_id = self._split_prefix(session_id)
        sub = self._find_provider(prefix)
        if sub is None:
            raise FileNotFoundError(
                f"未知 provider 前缀 '{prefix}'，无法读取 {session_id}"
            )
        session = sub.read_session(sub_id)
        # 覆盖 session_id 为带前缀形式，保证调用方看到的一致性
        session.session_id = session_id
        return session

    def is_completed(self, session_id: str) -> bool:
        prefix, sub_id = self._split_prefix(session_id)
        sub = self._find_provider(prefix)
        if sub is None:
            return False
        try:
            return sub.is_completed(sub_id)
        except Exception as exc:  # noqa: BLE001 — 子 provider 失败不阻断聚合
            logger.warning(
                "子 provider '%s' is_completed 失败: %s", prefix, exc
            )
            return False

    def _split_prefix(self, session_id: str) -> tuple[str, str]:
        """session_id → (prefix, sub_id)。

        无 ``:`` 时返回 ``("", session_id)``，``_find_provider("")`` 将返回 None
        （除非有 prefix 为空的子 provider，本设计中不存在）。
        """
        if self.PREFIX_SEP not in session_id:
            return "", session_id
        prefix, _, sub_id = session_id.partition(self.PREFIX_SEP)
        return prefix, sub_id

    def _find_provider(self, prefix: str) -> SessionProvider | None:
        for p, sub in self._providers:
            if p == prefix:
                return sub
        return None


# ---------------------------------------------------------------------------
# 工厂
# ---------------------------------------------------------------------------


def create_session_provider(
    *,
    target: str = "trae",
    sessions_root: Path | None = None,
    registry: CodingSoftwareRegistry | None = None,
    adapter_factory: Callable[[InstalledSoftware], SessionProvider | None] | None = None,
) -> SessionProvider:
    """按 target 创建 SessionProvider。

    target 取值：
    - "trae"：TraeSessionProvider（sessions_root=None 时自动探测）
    - "generic"：GenericJsonlSessionProvider（必须显式传入 sessions_root）
    - "multi"：MultiSessionProvider（聚合本机已安装软件的会话）

    multi 模式（Task 4）：
    - 用 :meth:`CodingSoftwareRegistry.discover_installed` 检测已安装软件
    - 默认对每个 ``InstalledSoftware.provider_name`` 实例化对应 Adapter
      （无参构造，Adapter 使用各自默认路径）
    - 可传入 ``adapter_factory`` 自定义实例化逻辑（测试时注入临时路径）
    - 可传入 ``registry`` 替换默认的 :class:`CodingSoftwareRegistry`
      （测试时注入 mock）

    通过客户端 mapping.yaml 的 session.target 配置切换。
    """
    target = (target or "trae").lower()
    if target == "trae":
        return TraeSessionProvider(sessions_root=sessions_root)
    if target == "generic":
        if sessions_root is None:
            raise ValueError("generic provider 必须显式传入 sessions_root")
        return GenericJsonlSessionProvider(sessions_root=sessions_root)
    if target == "multi":
        return _create_multi_provider(
            registry=registry,
            adapter_factory=adapter_factory,
        )
    raise ValueError(f"未知 session provider target: {target}")


def _create_multi_provider(
    *,
    registry: CodingSoftwareRegistry | None = None,
    adapter_factory: Callable[[InstalledSoftware], SessionProvider | None] | None = None,
) -> MultiSessionProvider:
    """multi 模式工厂：聚合本机已安装软件的会话 provider。

    延迟导入 :mod:`server.coding_adapters` 以避免循环依赖
    （``session_provider`` ← ``coding_adapters`` ← ``session_provider``）。
    """
    from server.coding_adapters import (
        AiderAdapter,
        ClaudeCodeAdapter,
        CodexAdapter,
        CursorAdapter,
        WindsurfAdapter,
    )
    from server.coding_adapters.registry import (
        CodingSoftwareRegistry,
        InstalledSoftware,
    )

    # provider_name（指纹表中的 provider 字段）→ Adapter 类
    # 注意：指纹表中 trae 的 provider 名是 "TraeAdapter"，
    # 但本包内对应的类是 TraeSessionProvider（历史命名），需特例映射
    provider_class_map: dict[str, type] = {
        "ClaudeCodeAdapter": ClaudeCodeAdapter,
        "CodexAdapter": CodexAdapter,
        "CursorAdapter": CursorAdapter,
        "AiderAdapter": AiderAdapter,
        "WindsurfAdapter": WindsurfAdapter,
        "TraeAdapter": TraeSessionProvider,
    }

    def default_factory(sw: InstalledSoftware) -> SessionProvider | None:
        cls = provider_class_map.get(sw.provider_name)
        if cls is None:
            logger.warning(
                "未知 provider_name '%s'（软件 %s），跳过",
                sw.provider_name,
                sw.name,
            )
            return None
        return cls()

    reg = registry if registry is not None else CodingSoftwareRegistry()
    factory = adapter_factory or default_factory
    installed = reg.discover_installed()
    sub_providers: list[tuple[str, SessionProvider]] = []
    for sw in installed:
        try:
            sub = factory(sw)
        except Exception as exc:  # noqa: BLE001 — 单个 Adapter 失败不阻断聚合
            logger.warning(
                "adapter_factory 实例化 %s (%s) 失败，跳过: %s",
                sw.name,
                sw.provider_name,
                exc,
            )
            continue
        if sub is not None:
            sub_providers.append((sw.name, sub))
    if not sub_providers:
        logger.info(
            "multi 模式未发现任何可用的 AI coding 软件，返回空 MultiSessionProvider"
        )
    return MultiSessionProvider(sub_providers)


__all__ = [
    "ConversationLogSessionProvider",
    "GenericJsonlSessionProvider",
    "MultiSessionProvider",
    "Session",
    "SessionMeta",
    "SessionProvider",
    "SessionTurn",
    "TraeSessionProvider",
    "create_session_provider",
]
