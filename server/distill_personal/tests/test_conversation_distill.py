"""Task 28 测试：梦境提炼集成（ConversationLog → 三阶段提炼 → tags 回灌）。

覆盖 SubTask 28.1-28.4：
- ConversationLogSessionProvider 转换（ConversationEvent → SessionTurn）
- list_sessions 按 peer 分组
- read_session 读取指定 peer
- is_completed 根据对话状态判断
- Light 阶段 needs_human_review 加权
- Light 阶段 tag_routing 跨职能信号加权
- run_with_conversation_log 集成测试
- tags 回灌（central 模式 PATCH 调用 + 错误隔离）
- 幂等性（相同 ConversationLog 多次提炼相同信号）
"""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from server.async_comm.constants import (
    CONV_STATE_ACTIVE,
    CONV_STATE_PAUSED,
    CONV_STATE_RESUMED,
    CONV_STATE_TIMEOUT_DISCONNECT,
    EVENT_ASK,
    EVENT_CONFIRMED,
    EVENT_NEEDS_HUMAN_REVIEW,
    EVENT_REALTIME_ANSWER,
    EVENT_REVISED,
    EVENT_SIMULATED_ANSWER,
)
from server.async_comm.conversation_log import ConversationLog
from server.async_comm.types import ConversationEvent, VectorClock
from server.distill_personal.light_stage import LightStage
from server.distill_personal.personal_distill import (
    CentralTagsFeedbackClient,
    LocalDreamsTagsFeedbackClient,
    PersonalDistill,
)
from server.distill_personal.session_provider import (
    ConversationLogSessionProvider,
    Session,
    SessionTurn,
)


# ---------------------------------------------------------------------------
# 辅助工厂
# ---------------------------------------------------------------------------


def _make_event(
    *,
    event_id: str | None = None,
    event_type: str = EVENT_ASK,
    peer_id: str = "bob",
    timestamp: str | None = None,
    payload: dict | None = None,
    in_reply_to: str = "",
    degraded: bool = False,
    realtime: bool = False,
    conversation_state: str = CONV_STATE_ACTIVE,
) -> ConversationEvent:
    """构造 ConversationEvent 测试实例。"""
    return ConversationEvent(
        event_id=event_id or str(uuid.uuid4()),
        event_type=event_type,
        peer_id=peer_id,
        timestamp=timestamp or datetime.now(timezone.utc).isoformat(),
        vector_clock=VectorClock(),
        payload=payload if payload is not None else {},
        in_reply_to=in_reply_to,
        degraded=degraded,
        realtime=realtime,
        conversation_state=conversation_state,
    )


def _ts(seconds: int) -> str:
    """生成可控时间戳（基于固定基准 + 偏移秒数），用于测试排序。"""
    base = datetime(2026, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
    return (base + timedelta(seconds=seconds)).isoformat()


def _make_log(tmp_path: Path) -> ConversationLog:
    """构造 ConversationLog 实例。"""
    return ConversationLog(tmp_path / "conversation.jsonl")


def _make_llm_returning_asset(tags: list[str] | None = None) -> Any:
    """返回产出资产的 LLM stub（含指定 tags）。"""
    asset_tags = tags if tags is not None else ["lint", "账号管理专家"]

    class _StubLLM:
        def chat(self, messages, *, schema=None, **kw):
            return {
                "content": json.dumps({
                    "skip": False,
                    "asset": {
                        "title": "提炼规则",
                        "content": "提交前必须跑 lint，禁止跳过",
                        "tags": asset_tags,
                        "rationale": "用户反复强调",
                    },
                    "confidence": 0.9,
                }),
                "usage": {"total_tokens": 200},
            }

    return _StubLLM()


# ---------------------------------------------------------------------------
# SubTask 28.1：ConversationLogSessionProvider 转换测试
# ---------------------------------------------------------------------------


class TestConversationLogSessionProviderConversion:
    """ConversationEvent → SessionTurn 转换测试。"""

    def test_ask_event_to_user_turn(self, tmp_path: Path) -> None:
        """ask 事件转为 role=user 的 SessionTurn。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "如何配置 lint?"},
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert len(session.turns) == 1
        assert session.turns[0].role == "user"
        assert session.turns[0].content == "如何配置 lint?"
        assert session.turns[0].metadata["event_type"] == EVENT_ASK

    def test_realtime_answer_to_assistant_turn(self, tmp_path: Path) -> None:
        """realtime_answer 事件转为 role=assistant 的 SessionTurn。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"answer": "lint 规则参考 .eslintrc"},
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert session.turns[0].role == "assistant"
        assert session.turns[0].content == "lint 规则参考 .eslintrc"
        assert session.turns[0].metadata["event_type"] == EVENT_REALTIME_ANSWER

    def test_simulated_answer_to_assistant_turn(self, tmp_path: Path) -> None:
        """simulated_answer 事件转为 role=assistant 的 SessionTurn。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_SIMULATED_ANSWER,
            peer_id="bob",
            payload={"answer": "模拟回答内容"},
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert session.turns[0].role == "assistant"

    def test_needs_human_review_to_assistant_turn(self, tmp_path: Path) -> None:
        """needs_human_review 事件转为 role=assistant 且 metadata 含 needs_human_review=True。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_NEEDS_HUMAN_REVIEW,
            peer_id="bob",
            payload={"answer": "需人工审核的回答"},
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert session.turns[0].role == "assistant"
        assert session.turns[0].metadata["needs_human_review"] is True
        assert session.turns[0].metadata["event_type"] == EVENT_NEEDS_HUMAN_REVIEW

    def test_tag_routing_metadata_preserved(self, tmp_path: Path) -> None:
        """payload 中的 tag_routing 被携带到 SessionTurn.metadata。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            payload={"question": "跨职能问题", "tag_routing": "no_tag_candidate"},
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert session.turns[0].metadata["tag_routing"] == "no_tag_candidate"

    def test_degraded_and_realtime_metadata(self, tmp_path: Path) -> None:
        """degraded / realtime 标记被携带到 metadata。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            payload={"answer": "降级回答"},
            degraded=True,
            realtime=True,
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert session.turns[0].metadata["degraded"] is True
        assert session.turns[0].metadata["realtime"] is True

    def test_full_conversation_to_session(self, tmp_path: Path) -> None:
        """完整问答对话转为含多轮 turn 的 Session。"""
        log = _make_log(tmp_path)
        ask = _make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "如何配置 lint?"},
        )
        log.append(ask)
        log.append(_make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            timestamp=_ts(10),
            payload={"answer": "参考 .eslintrc"},
            in_reply_to=ask.event_id,
        ))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        assert len(session.turns) == 2
        assert session.turns[0].role == "user"
        assert session.turns[1].role == "assistant"
        assert session.started_at == _ts(0)
        assert session.ended_at == _ts(10)


# ---------------------------------------------------------------------------
# SubTask 28.1：list_sessions 按 peer 分组
# ---------------------------------------------------------------------------


class TestListSessionsByPeer:
    """list_sessions 按 peer_id 分组测试。"""

    def test_multiple_peers_grouped(self, tmp_path: Path) -> None:
        """多个 peer 的事件分组为多个 Session。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", timestamp=_ts(0), payload={"question": "q1"}))
        log.append(_make_event(event_type=EVENT_ASK, peer_id="alice", timestamp=_ts(5), payload={"question": "q2"}))
        log.append(_make_event(event_type=EVENT_REALTIME_ANSWER, peer_id="bob", timestamp=_ts(10), payload={"answer": "a1"}))
        provider = ConversationLogSessionProvider(log)
        metas = provider.list_sessions()
        assert len(metas) == 2
        peer_ids = {m.session_id for m in metas}
        assert peer_ids == {"bob", "alice"}

    def test_list_sessions_ordered_by_mtime(self, tmp_path: Path) -> None:
        """list_sessions 按 mtime 升序。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", timestamp=_ts(100), payload={"question": "q1"}))
        log.append(_make_event(event_type=EVENT_ASK, peer_id="alice", timestamp=_ts(0), payload={"question": "q2"}))
        provider = ConversationLogSessionProvider(log)
        metas = provider.list_sessions()
        # alice 最新事件 _ts(0) 早于 bob 最新事件 _ts(100)
        assert metas[0].session_id == "alice"
        assert metas[1].session_id == "bob"

    def test_list_sessions_empty_log(self, tmp_path: Path) -> None:
        """空日志返回空列表。"""
        log = _make_log(tmp_path)
        provider = ConversationLogSessionProvider(log)
        metas = provider.list_sessions()
        assert metas == []

    def test_list_sessions_size_field(self, tmp_path: Path) -> None:
        """SessionMeta.size 记录该 peer 的事件数。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.append(_make_event(event_type=EVENT_REALTIME_ANSWER, peer_id="bob", payload={"answer": "a1"}))
        provider = ConversationLogSessionProvider(log)
        metas = provider.list_sessions()
        assert metas[0].size == 2

    def test_list_sessions_load_failure_returns_empty(self, tmp_path: Path) -> None:
        """ConversationLog load_all 抛异常时返回空列表。"""
        class _BrokenLog:
            def load_all(self):
                raise RuntimeError("log broken")
            def get_conversation_state(self, peer_id):
                return None
        provider = ConversationLogSessionProvider(_BrokenLog())
        metas = provider.list_sessions()
        assert metas == []


# ---------------------------------------------------------------------------
# SubTask 28.1：read_session 读取指定 peer
# ---------------------------------------------------------------------------


class TestReadSession:
    """read_session 测试。"""

    def test_read_session_not_found(self, tmp_path: Path) -> None:
        """不存在的 peer 抛 FileNotFoundError。"""
        log = _make_log(tmp_path)
        provider = ConversationLogSessionProvider(log)
        with pytest.raises(FileNotFoundError):
            provider.read_session("nonexistent")

    def test_read_session_load_failure_raises_file_not_found(self, tmp_path: Path) -> None:
        """load_by_peer 抛异常时转为 FileNotFoundError。"""
        class _BrokenLog:
            def load_by_peer(self, peer_id):
                raise RuntimeError("read error")
            def get_conversation_state(self, peer_id):
                return None
        provider = ConversationLogSessionProvider(_BrokenLog())
        with pytest.raises(FileNotFoundError):
            provider.read_session("bob")

    def test_read_session_turns_ordered_by_timestamp(self, tmp_path: Path) -> None:
        """SessionTurn 按时间升序排列。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_REALTIME_ANSWER, peer_id="bob", timestamp=_ts(10), payload={"answer": "a1"}))
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", timestamp=_ts(0), payload={"question": "q1"}))
        provider = ConversationLogSessionProvider(log)
        session = provider.read_session("bob")
        # ask (ts=0) 应在前，answer (ts=10) 应在后
        assert session.turns[0].content == "q1"
        assert session.turns[1].content == "a1"


# ---------------------------------------------------------------------------
# SubTask 28.1：is_completed 根据对话状态判断
# ---------------------------------------------------------------------------


class TestIsCompleted:
    """is_completed 测试。"""

    def test_active_state_not_completed(self, tmp_path: Path) -> None:
        """active 状态返回 False（进行中）。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.set_conversation_state("bob", CONV_STATE_ACTIVE)
        provider = ConversationLogSessionProvider(log)
        assert provider.is_completed("bob") is False

    def test_paused_state_not_completed(self, tmp_path: Path) -> None:
        """paused 状态返回 False（中断未恢复）。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.set_conversation_state("bob", CONV_STATE_PAUSED)
        provider = ConversationLogSessionProvider(log)
        assert provider.is_completed("bob") is False

    def test_timeout_disconnect_not_completed(self, tmp_path: Path) -> None:
        """timeout_disconnect 状态返回 False。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.set_conversation_state("bob", CONV_STATE_TIMEOUT_DISCONNECT)
        provider = ConversationLogSessionProvider(log)
        assert provider.is_completed("bob") is False

    def test_resumed_state_completed(self, tmp_path: Path) -> None:
        """resumed 状态返回 True（已恢复，可提炼）。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.set_conversation_state("bob", CONV_STATE_RESUMED)
        provider = ConversationLogSessionProvider(log)
        assert provider.is_completed("bob") is True

    def test_no_state_completed(self, tmp_path: Path) -> None:
        """无状态记录返回 True（可提炼）。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        provider = ConversationLogSessionProvider(log)
        assert provider.is_completed("bob") is True

    def test_list_sessions_respects_completed(self, tmp_path: Path) -> None:
        """list_sessions 的 completed 字段与 is_completed 一致。"""
        log = _make_log(tmp_path)
        log.append(_make_event(event_type=EVENT_ASK, peer_id="bob", payload={"question": "q1"}))
        log.append(_make_event(event_type=EVENT_ASK, peer_id="alice", payload={"question": "q2"}))
        log.set_conversation_state("bob", CONV_STATE_ACTIVE)
        # bob active → completed=False；alice 无状态 → completed=True
        provider = ConversationLogSessionProvider(log)
        metas = provider.list_sessions()
        meta_map = {m.session_id: m for m in metas}
        assert meta_map["bob"].completed is False
        assert meta_map["alice"].completed is True


# ---------------------------------------------------------------------------
# SubTask 28.2：Light 阶段 needs_human_review 加权
# ---------------------------------------------------------------------------


class TestLightNeedsHumanReviewWeighting:
    """Light 阶段 needs_human_review 事件加权测试。"""

    def test_needs_human_review_signal_extracted(self) -> None:
        """含 needs_human_review 标记的 turn 即使无关键词命中也被抽取。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="assistant",
                    content="这段代码需要人工审核确认",
                    metadata={"event_type": "needs_human_review", "needs_human_review": True},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        assert result.signal_count == 1
        sig = result.signals[0]
        # needs_human_review 优先标注为 rule
        assert sig.candidate_type == "rule"
        # 加权后 confidence >= 0.4 + 0.2 = 0.6
        assert sig.confidence >= 0.6
        assert sig.metadata.get("needs_human_review") is True

    def test_needs_human_review_confidence_boosted(self) -> None:
        """含关键词命中的 needs_human_review turn confidence 额外 +0.2。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="assistant",
                    content="必须提交代码前跑测试",  # rule 关键词命中 score=1（"必须"）
                    metadata={"event_type": "needs_human_review", "needs_human_review": True},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        sig = result.signals[0]
        # 基础 confidence（score=1）= 0.45，+0.2 = 0.65
        assert sig.confidence == pytest.approx(0.65)
        assert sig.candidate_type == "rule"

    def test_needs_human_review_bypasses_noise_filter(self) -> None:
        """needs_human_review turn 即使内容很短也跳过噪声过滤。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="assistant",
                    content="短",  # 正常会被噪声过滤
                    metadata={"event_type": "needs_human_review", "needs_human_review": True},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        assert result.signal_count == 1

    def test_needs_human_review_candidate_type_rule(self) -> None:
        """含 memory 关键词的 needs_human_review turn 被重新标注为 rule。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="assistant",
                    content="我们决定用 SQLAlchemy 作为 ORM",  # memory 关键词命中
                    metadata={"event_type": "needs_human_review", "needs_human_review": True},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        sig = result.signals[0]
        # needs_human_review 优先标注为 rule（即使原本是 memory）
        assert sig.candidate_type == "rule"


# ---------------------------------------------------------------------------
# SubTask 28.2：Light 阶段 tag_routing 跨职能信号加权
# ---------------------------------------------------------------------------


class TestLightTagRoutingWeighting:
    """Light 阶段 tag_routing 跨职能协作信号加权测试。"""

    def test_tag_routing_signal_extracted(self) -> None:
        """含 tag_routing 标记的 turn 即使无关键词命中也被抽取。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="user",
                    content="跨职能路由的问题",
                    metadata={"event_type": "ask", "tag_routing": "no_tag_candidate"},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        assert result.signal_count == 1
        sig = result.signals[0]
        # tag_routing 无关键词命中时默认标注为 memory
        assert sig.candidate_type == "memory"
        # 加权后 confidence >= 0.4 + 0.1 = 0.5
        assert sig.confidence >= 0.5
        assert sig.metadata.get("tag_routing") == "no_tag_candidate"

    def test_tag_routing_confidence_boosted(self) -> None:
        """含关键词命中的 tag_routing turn confidence 额外 +0.1。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="user",
                    content="必须提交代码前跑测试",  # rule 关键词命中 score=1（"必须"）
                    metadata={"event_type": "ask", "tag_routing": "no_probe_response"},
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        sig = result.signals[0]
        # 基础 confidence（score=1）= 0.45，+0.1 = 0.55
        assert sig.confidence == pytest.approx(0.55)

    def test_needs_review_and_tag_routing_combined(self) -> None:
        """同时含 needs_human_review 和 tag_routing 的 turn 双重加权。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(
                    role="assistant",
                    content="必须提交代码前跑测试",  # rule 关键词命中 score=1（"必须"）
                    metadata={
                        "event_type": "needs_human_review",
                        "needs_human_review": True,
                        "tag_routing": "no_tag_candidate",
                    },
                ),
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        sig = result.signals[0]
        # 基础 confidence（score=1）= 0.45，+0.2（review）+0.1（tag_routing）= 0.75
        assert sig.confidence == pytest.approx(0.75)
        assert sig.candidate_type == "rule"  # needs_human_review 优先标注为 rule

    def test_normal_turns_unaffected_by_weighting(self) -> None:
        """无协作信号标记的 turn 不受加权影响（回归验证）。"""
        session = Session(
            session_id="s1",
            turns=[
                SessionTurn(role="user", content="必须提交代码前跑测试"),  # rule score=1, 无 metadata
            ],
        )
        stage = LightStage()
        result = stage.run([session])
        sig = result.signals[0]
        # 基础 confidence（score=1）= 0.45，无加权
        assert sig.confidence == pytest.approx(0.45)
        assert "needs_human_review" not in sig.metadata
        assert "tag_routing" not in sig.metadata


# ---------------------------------------------------------------------------
# SubTask 28.3 + 28.4：run_with_conversation_log 集成测试
# ---------------------------------------------------------------------------


class TestRunWithConversationLog:
    """run_with_conversation_log 集成测试。"""

    def test_basic_distill_from_conversation_log(self, tmp_path: Path) -> None:
        """从 ConversationLog 加载会话并执行三阶段提炼。"""
        log = _make_log(tmp_path)
        # 构造含规则信号的对话
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint，禁止跳过"},
        ))
        log.append(_make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            timestamp=_ts(10),
            payload={"answer": "明白，已记录规则"},
        ))
        distill = PersonalDistill(
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.error is None
        assert result.conversation_session_count == 1
        assert result.light is not None
        assert result.light.signal_count > 0

    def test_skips_active_conversations(self, tmp_path: Path) -> None:
        """active 状态的对话被跳过（不提炼）。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            payload={"question": "提交前必须跑 lint"},
        ))
        log.set_conversation_state("bob", CONV_STATE_ACTIVE)
        distill = PersonalDistill(promotion_threshold=0.0)
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.error is None
        assert result.conversation_session_count == 0

    def test_includes_resumed_conversations(self, tmp_path: Path) -> None:
        """resumed 状态的对话被提炼。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint，禁止跳过"},
        ))
        log.set_conversation_state("bob", CONV_STATE_RESUMED)
        distill = PersonalDistill(
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.conversation_session_count == 1
        assert result.light.signal_count > 0

    def test_empty_log_returns_no_error(self, tmp_path: Path) -> None:
        """空 ConversationLog 不报错，session_count=0。"""
        log = _make_log(tmp_path)
        distill = PersonalDistill(promotion_threshold=0.0)
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.error is None
        assert result.conversation_session_count == 0

    def test_multiple_peers_distilled(self, tmp_path: Path) -> None:
        """多 peer 对话均被提炼。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint"},
        ))
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="alice",
            timestamp=_ts(10),
            payload={"question": "我们决定用 SQLAlchemy 作为 ORM"},
        ))
        distill = PersonalDistill(
            repo_root=tmp_path,
            member_id="carol",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(log, member_id="carol")
        assert result.conversation_session_count == 2

    def test_needs_human_review_signal_in_distill(self, tmp_path: Path) -> None:
        """ConversationLog 中的 needs_human_review 事件在提炼中被加权抽取。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_NEEDS_HUMAN_REVIEW,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"answer": "需人工审核的协作回答"},
        ))
        distill = PersonalDistill(
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.light.signal_count > 0
        sig = result.light.signals[0]
        assert sig.candidate_type == "rule"
        assert sig.metadata.get("needs_human_review") is True


# ---------------------------------------------------------------------------
# SubTask 28.3：tags 回灌测试
# ---------------------------------------------------------------------------


class TestTagsFeedback:
    """tags 回灌测试。"""

    def test_central_tags_feedback_calls_patch(self) -> None:
        """CentralTagsFeedbackClient 调用 PATCH /v1/team/members/{id}。"""
        class _MockResponse:
            status_code = 200
            text = '{"ok": true}'

        class _MockClient:
            def __init__(self):
                self.calls = []

            def patch(self, url, json=None, headers=None, timeout=None):
                self.calls.append({
                    "url": url, "json": json, "headers": headers, "timeout": timeout,
                })
                return _MockResponse()

        mock = _MockClient()
        client = CentralTagsFeedbackClient(
            base_url="http://test:8000",
            api_key="test-key",
            http_client=mock,
        )
        ok = client.feedback_tags("alice", ["账号管理专家", "lint"])
        assert ok is True
        assert len(mock.calls) == 1
        assert mock.calls[0]["url"] == "http://test:8000/v1/team/members/alice"
        assert mock.calls[0]["json"] == {"tags": ["账号管理专家", "lint"]}
        assert mock.calls[0]["headers"]["Authorization"] == "Bearer test-key"

    def test_central_tags_feedback_failure_returns_false(self) -> None:
        """PATCH 返回非 200 时返回 False。"""
        class _MockResponse:
            status_code = 403
            text = '{"detail": "forbidden"}'

        class _MockClient:
            def patch(self, url, json=None, headers=None, timeout=None):
                return _MockResponse()

        client = CentralTagsFeedbackClient(http_client=_MockClient())
        ok = client.feedback_tags("alice", ["tag1"])
        assert ok is False

    def test_central_tags_feedback_rate_limit(self) -> None:
        """同 member 每日最多回灌 1 次（限频）。"""
        class _MockResponse:
            status_code = 200
            text = '{}'

        class _MockClient:
            def __init__(self):
                self.call_count = 0

            def patch(self, url, json=None, headers=None, timeout=None):
                self.call_count += 1
                return _MockResponse()

        mock = _MockClient()
        client = CentralTagsFeedbackClient(http_client=mock)
        # 第一次回灌
        ok1 = client.feedback_tags("alice", ["tag1"])
        assert ok1 is True
        assert mock.call_count == 1
        # 第二次回灌（同日）→ 限频跳过，不调用 HTTP
        ok2 = client.feedback_tags("alice", ["tag2"])
        assert ok2 is True
        assert mock.call_count == 1  # 未增加

    def test_central_tags_feedback_empty_tags_returns_false(self) -> None:
        """空 tags 列表返回 False。"""
        client = CentralTagsFeedbackClient()
        ok = client.feedback_tags("alice", [])
        assert ok is False

    def test_local_dreams_tags_feedback_writes_entry(self, tmp_path: Path) -> None:
        """LocalDreamsTagsFeedbackClient 写入 DREAMS.md。"""
        from server.infra_git.dreams import parse_entries, read_month

        client = LocalDreamsTagsFeedbackClient(repo_root=tmp_path)
        ok = client.feedback_tags("alice", ["账号管理专家", "lint"])
        assert ok is True
        # 验证 DREAMS.md 写入
        content = read_month(tmp_path, datetime.now(timezone.utc).strftime("%Y-%m"))
        assert "tags 回灌建议" in content
        assert "账号管理专家" in content
        entries = parse_entries(content)
        assert any(e.stage == "tags" for e in entries)

    def test_local_dreams_tags_feedback_no_repo_root(self) -> None:
        """无 repo_root 时 LocalDreamsTagsFeedbackClient 仍返回 True（仅记录日志）。"""
        client = LocalDreamsTagsFeedbackClient(repo_root=None)
        ok = client.feedback_tags("alice", ["tag1"])
        assert ok is True

    def test_tags_feedback_in_run_with_conversation_log(self, tmp_path: Path) -> None:
        """run_with_conversation_log 启用 tags 回灌时调用 client。"""
        class _MockTagsClient:
            def __init__(self):
                self.calls = []

            def feedback_tags(self, member_id, new_tags):
                self.calls.append({"member_id": member_id, "tags": new_tags})
                return True

        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint，禁止跳过"},
        ))
        mock_client = _MockTagsClient()
        distill = PersonalDistill(
            llm=_make_llm_returning_asset(tags=["账号管理专家"]),
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(
            log,
            member_id="alice",
            enable_tags_feedback=True,
            tags_feedback_client=mock_client,
        )
        # 若产出资产，则 tags 回灌被调用
        if result.produced_count > 0:
            assert result.tags_feedback_applied is True
            assert len(mock_client.calls) == 1
            assert mock_client.calls[0]["member_id"] == "alice"
            assert "账号管理专家" in mock_client.calls[0]["tags"]

    def test_tags_feedback_disabled_by_default(self, tmp_path: Path) -> None:
        """enable_tags_feedback 默认 False，不回灌。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            payload={"question": "提交前必须跑 lint"},
        ))
        distill = PersonalDistill(
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(log, member_id="alice")
        assert result.tags_feedback_applied is False

    def test_tags_feedback_error_isolated(self, tmp_path: Path) -> None:
        """tags 回灌失败不阻断提炼流程。"""
        class _BrokenTagsClient:
            def feedback_tags(self, member_id, new_tags):
                raise RuntimeError("tags feedback broken")

        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint，禁止跳过"},
        ))
        distill = PersonalDistill(
            llm=_make_llm_returning_asset(),
            repo_root=tmp_path,
            member_id="alice",
            promotion_threshold=0.0,
        )
        result = distill.run_with_conversation_log(
            log,
            member_id="alice",
            enable_tags_feedback=True,
            tags_feedback_client=_BrokenTagsClient(),
        )
        # 提炼流程不受影响
        assert result.error is None
        assert result.light is not None
        # 但回灌错误被记录
        if result.produced_count > 0:
            assert result.tags_feedback_applied is False
            assert result.tags_feedback_error is not None


# ---------------------------------------------------------------------------
# SubTask 28.4：幂等性测试
# ---------------------------------------------------------------------------


class TestIdempotency:
    """幂等性测试：相同 ConversationLog 多次提炼产出相同信号。"""

    def test_same_log_produces_same_signal_count(self, tmp_path: Path) -> None:
        """相同 ConversationLog 两次提炼产出相同数量的信号。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_ASK,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"question": "提交前必须跑 lint，禁止跳过"},
        ))
        log.append(_make_event(
            event_type=EVENT_REALTIME_ANSWER,
            peer_id="bob",
            timestamp=_ts(10),
            payload={"answer": "明白，已记录规则"},
        ))
        distill = PersonalDistill(promotion_threshold=0.0)
        result1 = distill.run_with_conversation_log(log, member_id="alice", save_intermediate=False)
        result2 = distill.run_with_conversation_log(log, member_id="alice", save_intermediate=False)
        assert result1.light.signal_count == result2.light.signal_count

    def test_same_log_produces_same_confidence(self, tmp_path: Path) -> None:
        """相同 ConversationLog 两次提炼产出的信号 confidence 一致。"""
        log = _make_log(tmp_path)
        log.append(_make_event(
            event_type=EVENT_NEEDS_HUMAN_REVIEW,
            peer_id="bob",
            timestamp=_ts(0),
            payload={"answer": "需人工审核的回答"},
        ))
        distill = PersonalDistill(promotion_threshold=0.0)
        result1 = distill.run_with_conversation_log(log, member_id="alice", save_intermediate=False)
        result2 = distill.run_with_conversation_log(log, member_id="alice", save_intermediate=False)
        assert len(result1.light.signals) == len(result2.light.signals)
        for s1, s2 in zip(result1.light.signals, result2.light.signals):
            assert s1.confidence == s2.confidence
            assert s1.candidate_type == s2.candidate_type
