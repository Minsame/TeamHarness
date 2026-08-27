"""SubTask 7.1 — SessionProvider 抽象测试。

验证点：
- SessionProvider Trae 适配可用（读 *.jsonl）
- SessionProvider 通用 JSONL 兜底可用
- discover_sessions_root 按 OS 自动探测（复用 Agent 1）
- TraeSessionProvider 与 GenericJsonlSessionProvider 解析格式一致
- 增量参数 since 生效
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from server.coding_adapters.registry import InstalledSoftware
from server.distill_personal.session_provider import (
    GenericJsonlSessionProvider,
    MultiSessionProvider,
    Session,
    SessionTurn,
    TraeSessionProvider,
    create_session_provider,
)
from server.infra_git.trae_adapter import discover_sessions_root


# ---------------------------------------------------------------------------
# TraeSessionProvider
# ---------------------------------------------------------------------------


def test_trae_session_provider_list_and_read(fake_trae_sessions_dir: Path, monkeypatch):
    """TraeSessionProvider 读取 *.jsonl 会话。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_trae_sessions_dir))
    provider = TraeSessionProvider()
    metas = provider.list_sessions()
    assert len(metas) == 3
    # 按 mtime 升序
    assert metas[0].session_id == "session-001"
    # 读取完整会话
    session = provider.read_session("session-001")
    assert isinstance(session, Session)
    assert session.session_id == "session-001"
    assert len(session.turns) == 4
    assert session.turns[0].role == "user"
    assert "必须" in session.turns[2].content
    assert session.completed is True


def test_trae_session_provider_since_filter(fake_trae_sessions_dir: Path, monkeypatch):
    """since 时间戳增量过滤。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_trae_sessions_dir))
    provider = TraeSessionProvider()
    # 取第二个会话的 mtime 作为 since
    all_metas = provider.list_sessions()
    cutoff = all_metas[1].mtime
    new_metas = provider.list_sessions(since=cutoff)
    # mtime >= cutoff 的有 2 个（session-002 和 session-003）
    assert len(new_metas) == 2
    assert {m.session_id for m in new_metas} == {"session-002", "session-003"}


def test_trae_session_provider_completed(fake_trae_sessions_dir: Path, monkeypatch):
    """is_completed 返回 True（Trae 会话落盘即完成）。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_trae_sessions_dir))
    provider = TraeSessionProvider()
    assert provider.is_completed("session-001") is True
    assert provider.is_completed("nonexistent") is False


def test_trae_session_provider_root_not_found(monkeypatch, tmp_path: Path):
    """根目录不存在时返回空列表。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(tmp_path / "missing"))
    provider = TraeSessionProvider()
    assert provider.list_sessions() == []


def test_trae_session_provider_read_nonexistent_raises(monkeypatch, tmp_path: Path):
    """读取不存在的会话抛 FileNotFoundError。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(tmp_path))
    provider = TraeSessionProvider()
    with pytest.raises(FileNotFoundError):
        provider.read_session("nope")


def test_trae_session_provider_invalid_json_skipped(tmp_path: Path):
    """非法 JSON 行被跳过（不抛异常）。"""
    sessions_dir = tmp_path / "trae-sessions"
    sessions_dir.mkdir()
    bad = sessions_dir / "bad.jsonl"
    bad.write_text(
        json.dumps({"role": "user", "content": "valid line"}) + "\n"
        "not a json line\n"
        + json.dumps({"role": "assistant", "content": "another valid"}) + "\n",
        encoding="utf-8",
    )
    provider = TraeSessionProvider(sessions_root=sessions_dir)
    session = provider.read_session("bad")
    assert len(session.turns) == 2  # 非法行跳过


def test_trae_session_provider_loose_field_parsing(tmp_path: Path):
    """role/content 缺失用默认值，timestamp 候选键生效。"""
    sessions_dir = tmp_path / "loose"
    sessions_dir.mkdir()
    f = sessions_dir / "loose.jsonl"
    f.write_text(
        json.dumps({"ts": "2026-08-07T10:00:00Z", "text": "no role/content"}) + "\n",
        encoding="utf-8",
    )
    provider = TraeSessionProvider(sessions_root=sessions_dir)
    session = provider.read_session("loose")
    assert session.turns[0].role == "user"  # 默认 user
    assert session.turns[0].content == ""  # 默认空
    assert session.turns[0].timestamp == "2026-08-07T10:00:00Z"  # ts 候选键


# ---------------------------------------------------------------------------
# GenericJsonlSessionProvider
# ---------------------------------------------------------------------------


def test_generic_provider_list_and_read(fake_trae_sessions_dir: Path):
    """通用 JSONL 兜底 provider。"""
    provider = GenericJsonlSessionProvider(fake_trae_sessions_dir)
    metas = provider.list_sessions()
    assert len(metas) == 3
    session = provider.read_session("session-002")
    assert len(session.turns) == 3
    assert "决定" in session.turns[0].content


def test_generic_provider_since_filter(fake_trae_sessions_dir: Path):
    provider = GenericJsonlSessionProvider(fake_trae_sessions_dir)
    all_metas = provider.list_sessions()
    cutoff = all_metas[1].mtime
    new_metas = provider.list_sessions(since=cutoff)
    assert len(new_metas) == 2


def test_generic_provider_nonexistent_root_raises(tmp_path: Path):
    """根目录不存在抛 FileNotFoundError。"""
    with pytest.raises(FileNotFoundError):
        GenericJsonlSessionProvider(tmp_path / "missing")


# ---------------------------------------------------------------------------
# create_session_provider 工厂
# ---------------------------------------------------------------------------


def test_create_session_provider_trae(fake_trae_sessions_dir: Path, monkeypatch):
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_trae_sessions_dir))
    provider = create_session_provider(target="trae")
    assert isinstance(provider, TraeSessionProvider)
    assert len(provider.list_sessions()) == 3


def test_create_session_provider_generic(fake_trae_sessions_dir: Path):
    provider = create_session_provider(target="generic", sessions_root=fake_trae_sessions_dir)
    assert isinstance(provider, GenericJsonlSessionProvider)


def test_create_session_provider_generic_requires_root():
    """generic provider 必须显式传入 sessions_root。"""
    with pytest.raises(ValueError, match="generic provider"):
        create_session_provider(target="generic", sessions_root=None)


def test_create_session_provider_unknown_target():
    with pytest.raises(ValueError, match="未知 session provider"):
        create_session_provider(target="unknown")


# ---------------------------------------------------------------------------
# discover_sessions_root（复用 Agent 1）
# ---------------------------------------------------------------------------


def test_discover_sessions_root_env_override(fake_trae_sessions_dir: Path, monkeypatch):
    """环境变量覆盖。"""
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(fake_trae_sessions_dir))
    assert discover_sessions_root() == fake_trae_sessions_dir


def test_discover_sessions_root_nonexistent_returns_none(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("TRAE_SESSIONS_ROOT", str(tmp_path / "missing"))
    assert discover_sessions_root() is None


# ---------------------------------------------------------------------------
# MultiSessionProvider
# ---------------------------------------------------------------------------


def _make_jsonl_session(
    dir_: Path,
    name: str,
    lines: list[dict],
    mtime_offset: float = 0.0,
) -> Path:
    """在 dir_ 下创建 <name>.jsonl，写入 lines（每行一条 JSON），设置 mtime。

    mtime_offset 相对 time.time() 的偏移（秒），用于构造可排序的时间序列，
    避免 Windows 文件系统 mtime 精度问题。
    """
    path = dir_ / f"{name}.jsonl"
    path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n",
        encoding="utf-8",
    )
    base = time.time() + mtime_offset
    os.utime(path, (base, base))
    return path


def test_multi_provider_merges_sessions_sorted_by_mtime(tmp_path: Path):
    """合并多个子 provider 的会话，按 mtime 升序，session_id 加前缀。"""
    dir_a = tmp_path / "claude"
    dir_b = tmp_path / "windsurf"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_jsonl_session(
        dir_a, "sess_a",
        [{"role": "user", "content": "a", "timestamp": "2026-08-07T10:00:00Z"}],
        mtime_offset=0.0,
    )
    _make_jsonl_session(
        dir_b, "sess_b",
        [{"role": "user", "content": "b", "timestamp": "2026-08-07T11:00:00Z"}],
        mtime_offset=10.0,
    )
    _make_jsonl_session(
        dir_a, "sess_a2",
        [{"role": "user", "content": "a2", "timestamp": "2026-08-07T12:00:00Z"}],
        mtime_offset=20.0,
    )

    sub_a = GenericJsonlSessionProvider(dir_a)
    sub_b = GenericJsonlSessionProvider(dir_b)
    multi = MultiSessionProvider([("claude_code", sub_a), ("windsurf", sub_b)])

    metas = multi.list_sessions()
    assert len(metas) == 3
    # 按 mtime 升序，session_id 带前缀
    assert metas[0].session_id == "claude_code:sess_a"
    assert metas[1].session_id == "windsurf:sess_b"
    assert metas[2].session_id == "claude_code:sess_a2"
    # mtime 单调不降
    assert metas[0].mtime <= metas[1].mtime <= metas[2].mtime


def test_multi_provider_read_session_routes_by_prefix(tmp_path: Path):
    """read_session 根据前缀路由到对应子 provider，返回的 session_id 带前缀。"""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_jsonl_session(
        dir_a, "s1",
        [{"role": "user", "content": "from_a", "timestamp": "2026-08-07T10:00:00Z"}],
    )
    _make_jsonl_session(
        dir_b, "s2",
        [{"role": "assistant", "content": "from_b", "timestamp": "2026-08-07T11:00:00Z"}],
        mtime_offset=10.0,
    )

    multi = MultiSessionProvider([
        ("a", GenericJsonlSessionProvider(dir_a)),
        ("b", GenericJsonlSessionProvider(dir_b)),
    ])

    session_a = multi.read_session("a:s1")
    assert session_a.session_id == "a:s1"  # 带前缀
    assert session_a.turns[0].content == "from_a"

    session_b = multi.read_session("b:s2")
    assert session_b.session_id == "b:s2"
    assert session_b.turns[0].content == "from_b"


def test_multi_provider_is_completed_routes_by_prefix(tmp_path: Path):
    """is_completed 根据前缀路由。"""
    dir_a = tmp_path / "a"
    dir_a.mkdir()
    _make_jsonl_session(
        dir_a, "s1",
        [{"role": "user", "content": "x", "timestamp": "2026-08-07T10:00:00Z"}],
    )

    multi = MultiSessionProvider([("a", GenericJsonlSessionProvider(dir_a))])
    assert multi.is_completed("a:s1") is True
    assert multi.is_completed("a:nonexistent") is False
    # 未知前缀返回 False（不抛异常）
    assert multi.is_completed("unknown:s1") is False


def test_multi_provider_read_session_unknown_prefix_raises(tmp_path: Path):
    """未知前缀 read_session 抛 FileNotFoundError。"""
    multi = MultiSessionProvider([])
    with pytest.raises(FileNotFoundError, match="未知 provider 前缀"):
        multi.read_session("unknown:s1")


def test_multi_provider_empty_providers_returns_empty_list():
    """空 providers 列表 list_sessions 返回 []。"""
    multi = MultiSessionProvider([])
    assert multi.list_sessions() == []
    assert multi.list_sessions(since=0.0) == []


def test_multi_provider_since_filter_propagates(tmp_path: Path):
    """since 参数传递给子 provider，做增量过滤。"""
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    _make_jsonl_session(
        dir_a, "old",
        [{"role": "user", "content": "old", "timestamp": "2026-08-07T10:00:00Z"}],
        mtime_offset=0.0,
    )
    _make_jsonl_session(
        dir_b, "new",
        [{"role": "user", "content": "new", "timestamp": "2026-08-07T11:00:00Z"}],
        mtime_offset=100.0,
    )

    multi = MultiSessionProvider([
        ("a", GenericJsonlSessionProvider(dir_a)),
        ("b", GenericJsonlSessionProvider(dir_b)),
    ])

    all_metas = multi.list_sessions()
    assert len(all_metas) == 2
    # 取较早会话的 mtime + 50 秒作为 since，排除 old
    cutoff = all_metas[0].mtime + 50
    new_metas = multi.list_sessions(since=cutoff)
    assert len(new_metas) == 1
    assert new_metas[0].session_id == "b:new"


def test_multi_provider_sub_provider_failure_does_not_block(tmp_path: Path):
    """子 provider list_sessions 抛异常时不阻断聚合，跳过该 provider。"""

    class FailingProvider:
        def list_sessions(self, since: float | None = None):
            raise RuntimeError("boom")

        def read_session(self, session_id: str):
            raise FileNotFoundError()

        def is_completed(self, session_id: str) -> bool:
            return False

    dir_ok = tmp_path / "ok"
    dir_ok.mkdir()
    _make_jsonl_session(
        dir_ok, "s1",
        [{"role": "user", "content": "ok", "timestamp": "2026-08-07T10:00:00Z"}],
    )

    multi = MultiSessionProvider([
        ("bad", FailingProvider()),
        ("ok", GenericJsonlSessionProvider(dir_ok)),
    ])

    metas = multi.list_sessions()
    assert len(metas) == 1
    assert metas[0].session_id == "ok:s1"


def test_multi_provider_session_id_with_slash_not_split(tmp_path: Path):
    """子 provider session_id 含 / 时，前缀路由不误切（ClaudeCode 场景）。"""
    from server.coding_adapters import ClaudeCodeAdapter

    projects_root = tmp_path / "projects"
    proj_dir = projects_root / "proj_a"
    proj_dir.mkdir(parents=True)
    _make_jsonl_session(
        proj_dir, "abc",
        [{"role": "user", "content": "deep", "timestamp": "2026-08-07T10:00:00Z"}],
    )

    multi = MultiSessionProvider([
        ("claude_code", ClaudeCodeAdapter(projects_root=projects_root)),
    ])

    metas = multi.list_sessions()
    assert len(metas) == 1
    # ClaudeCode session_id 形如 "proj_a/abc"，加前缀后 "claude_code:proj_a/abc"
    assert metas[0].session_id == "claude_code:proj_a/abc"

    session = multi.read_session("claude_code:proj_a/abc")
    assert session.session_id == "claude_code:proj_a/abc"
    assert session.turns[0].content == "deep"
    assert multi.is_completed("claude_code:proj_a/abc") is True


# ---------------------------------------------------------------------------
# create_session_provider(target="multi")
# ---------------------------------------------------------------------------


class _FakeRegistry:
    """假 CodingSoftwareRegistry，返回预设的 InstalledSoftware 列表。

    用于 multi 模式工厂测试，避免依赖真实本机安装情况。
    """

    def __init__(self, installed: list[InstalledSoftware]) -> None:
        self._installed = list(installed)
        self.discover_called = False

    def discover_installed(self) -> list[InstalledSoftware]:
        self.discover_called = True
        return list(self._installed)


def test_create_session_provider_multi_with_custom_factory(tmp_path: Path):
    """multi 模式 + 自定义 adapter_factory：聚合临时目录中的会话。"""
    from server.coding_adapters import ClaudeCodeAdapter, WindsurfAdapter

    claude_root = tmp_path / "claude_projects"
    windsurf_root = tmp_path / "windsurf_sessions"
    claude_root.mkdir()
    windsurf_root.mkdir()

    _make_jsonl_session(
        claude_root, "c1",
        [{"role": "user", "content": "claude", "timestamp": "2026-08-07T10:00:00Z"}],
        mtime_offset=0.0,
    )
    _make_jsonl_session(
        windsurf_root, "w1",
        [{"role": "user", "content": "windsurf", "timestamp": "2026-08-07T11:00:00Z"}],
        mtime_offset=10.0,
    )

    fake_installed = [
        InstalledSoftware(
            name="claude_code",
            install_path=claude_root,
            provider_name="ClaudeCodeAdapter",
        ),
        InstalledSoftware(
            name="windsurf",
            install_path=windsurf_root,
            provider_name="WindsurfAdapter",
        ),
    ]
    fake_registry = _FakeRegistry(fake_installed)

    def factory(sw: InstalledSoftware):
        if sw.provider_name == "ClaudeCodeAdapter":
            return ClaudeCodeAdapter(projects_root=claude_root)
        if sw.provider_name == "WindsurfAdapter":
            return WindsurfAdapter(sessions_root=windsurf_root)
        return None

    provider = create_session_provider(
        target="multi",
        registry=fake_registry,
        adapter_factory=factory,
    )
    assert isinstance(provider, MultiSessionProvider)
    assert fake_registry.discover_called is True

    metas = provider.list_sessions()
    assert len(metas) == 2
    assert metas[0].session_id == "claude_code:c1"
    assert metas[1].session_id == "windsurf:w1"

    session = provider.read_session("claude_code:c1")
    assert session.turns[0].content == "claude"


def test_create_session_provider_multi_empty_when_no_software():
    """multi 模式无软件时返回空 MultiSessionProvider（不抛异常）。"""
    fake_registry = _FakeRegistry([])
    provider = create_session_provider(target="multi", registry=fake_registry)
    assert isinstance(provider, MultiSessionProvider)
    assert provider.list_sessions() == []


def test_create_session_provider_multi_default_factory(
    monkeypatch, tmp_path: Path
):
    """multi 模式默认 factory 根据 provider_name 实例化对应 Adapter（无参构造）。"""
    from server.coding_adapters import WindsurfAdapter

    windsurf_root = tmp_path / "windsurf_sessions"
    windsurf_root.mkdir()
    _make_jsonl_session(
        windsurf_root, "w1",
        [{"role": "user", "content": "ws", "timestamp": "2026-08-07T10:00:00Z"}],
    )

    # mock WindsurfAdapter.__init__：无参构造时指向临时目录
    real_init = WindsurfAdapter.__init__

    def fake_init(self, sessions_root=None):
        real_init(self, sessions_root=windsurf_root)

    monkeypatch.setattr(WindsurfAdapter, "__init__", fake_init)

    fake_installed = [
        InstalledSoftware(
            name="windsurf",
            install_path=windsurf_root,
            provider_name="WindsurfAdapter",
        ),
    ]
    fake_registry = _FakeRegistry(fake_installed)

    provider = create_session_provider(target="multi", registry=fake_registry)
    assert isinstance(provider, MultiSessionProvider)
    metas = provider.list_sessions()
    assert len(metas) == 1
    assert metas[0].session_id == "windsurf:w1"


def test_create_session_provider_multi_factory_returns_none_skipped(
    tmp_path: Path,
):
    """adapter_factory 对所有软件返回 None 时，结果为空 MultiSessionProvider。"""
    fake_installed = [
        InstalledSoftware(
            name="claude_code",
            install_path=tmp_path,
            provider_name="ClaudeCodeAdapter",
        ),
        InstalledSoftware(
            name="unknown_sw",
            install_path=tmp_path,
            provider_name="UnknownAdapter",
        ),
    ]
    fake_registry = _FakeRegistry(fake_installed)

    def factory(sw: InstalledSoftware):
        return None

    provider = create_session_provider(
        target="multi",
        registry=fake_registry,
        adapter_factory=factory,
    )
    assert isinstance(provider, MultiSessionProvider)
    assert provider.list_sessions() == []


def test_create_session_provider_multi_factory_exception_skipped(tmp_path: Path):
    """adapter_factory 对某软件抛异常时跳过该软件，不阻断聚合。"""
    from server.coding_adapters import ClaudeCodeAdapter

    claude_root = tmp_path / "claude"
    claude_root.mkdir()
    _make_jsonl_session(
        claude_root, "c1",
        [{"role": "user", "content": "ok", "timestamp": "2026-08-07T10:00:00Z"}],
    )

    fake_installed = [
        InstalledSoftware(
            name="bad_sw",
            install_path=tmp_path,
            provider_name="BadAdapter",
        ),
        InstalledSoftware(
            name="claude_code",
            install_path=claude_root,
            provider_name="ClaudeCodeAdapter",
        ),
    ]
    fake_registry = _FakeRegistry(fake_installed)

    call_count = {"n": 0}

    def factory(sw: InstalledSoftware):
        call_count["n"] += 1
        if sw.name == "bad_sw":
            raise RuntimeError("factory boom")
        return ClaudeCodeAdapter(projects_root=claude_root)

    provider = create_session_provider(
        target="multi",
        registry=fake_registry,
        adapter_factory=factory,
    )
    assert isinstance(provider, MultiSessionProvider)
    metas = provider.list_sessions()
    assert len(metas) == 1
    assert metas[0].session_id == "claude_code:c1"
    # factory 被调用了两次（bad_sw 抛异常后继续处理 claude_code）
    assert call_count["n"] == 2
