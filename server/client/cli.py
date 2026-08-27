"""CLI 9 命令（sync / pr / recall / category-suggest / cost-estimate / index-reconcile / ask-peer / peers / shadow-log）。

对应 SubTask 6.6 + 技术方案 3.1.3 客户端封装 + Task 21 CLI 子命令扩展：
- teamharness sync：一键 pull --rebase + push 个人分支
- teamharness pr：从个人分支向 main 发起 PR
- teamharness recall：调服务端召回 / 离线降级本地
- teamharness category-suggest：调 /v1/category/suggest 推荐分类
- teamharness cost-estimate：估算一级提炼 LLM 成本（依赖 Agent 7 LLMProvider，未就绪用占位）
- teamharness index-reconcile：重建本地 manifest + 触发服务端 reconciliation（占位）
- teamharness ask-peer：向 peer AI 提问（自动选择在线实时 / 离线影子）（Task 21）
- teamharness peers：列出已知 peer（Task 21）
- teamharness shadow-log：查看交流报告（含实时 + 影子）（Task 21）

实现策略：
- 使用 argparse（Python 标准库，无额外依赖）
- 每个子命令返回 dict（成功结果）或抛 CliError（失败带 message）
- 入口 ClientCLI.main(argv) 返回退出码（0=成功，1=错误）
- 不直接调 git/HTTP，所有副作用通过 GitSync / RecallClient / AdoptionReporter 等封装
- ask-peer / peers / shadow-log 复用 async_comm + transport 底层（与 MCP Server 共享）

注意：未经用户显式调用 sync/pr 子命令不执行 git 提交与推送
（rule: 未经允许不可执行 git 提交和推送）。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Sequence

from server.client.adoption import AdoptionEvent, AdoptionReporter
from server.client.config import ClientConfig, load_client_config
from server.client.git_sync import GitSync
from server.client.manifest import ManifestBuilder
from server.client.mapping import load_mapping
from server.client.module_path import infer_module_path
from server.client.placeholders import (
    mock_category_suggest,
    mock_issue_api_key,
)
from server.client.recall_client import RecallClient
from server.client.private_isolation import PrivateIsolation
from server.client.working_copy import WorkingCopy

# Task 21：ask-peer / peers / shadow-log 复用 async_comm + transport 底层
from server.async_comm import (
    ConversationLog,
    Mailbox,
    PeerComm,
    PeerSnapshotManager,
    ShadowComm,
)
from server.transport.central_transport import CentralSyncTransport
from server.transport.hybrid_transport import HybridSyncTransport
from server.transport.p2p_transport import P2PSyncTransport
from server.transport.protocol import (
    SyncTransport,
    TOPOLOGY_CENTRAL,
    TOPOLOGY_HYBRID,
    TOPOLOGY_P2P,
)


class CliError(Exception):
    """CLI 错误（exit code 1）。"""


@dataclass
class CliResult:
    """命令执行结果。"""

    command: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    warnings: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command": self.command,
            "success": self.success,
            "data": self.data,
            "message": self.message,
            "warnings": list(self.warnings),
        }


# ---------------------------------------------------------------------------
# ClientCLI
# ---------------------------------------------------------------------------


class ClientCLI:
    """teamharness CLI 入口。

    使用：
        cli = ClientCLI(config=cfg)
        rc = cli.run(["sync", "--branch", "members/alice"])
        sys.exit(rc)
    """

    PROG = "teamharness"
    DESCRIPTION = "TeamHarness 客户端命令行"

    def __init__(
        self,
        config: ClientConfig | None = None,
        *,
        stdout: Any = None,
        stderr: Any = None,
        transport: SyncTransport | None = None,
    ) -> None:
        self.config = config or load_client_config()
        self.stdout = stdout or sys.stdout
        self.stderr = stderr or sys.stderr
        # Task 21：允许外部注入 transport（测试用 Stub），未注入时按 config.topology 创建
        self._transport = transport

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------

    def build_parser(self) -> argparse.ArgumentParser:
        parser = argparse.ArgumentParser(
            prog=self.PROG,
            description=self.DESCRIPTION,
        )
        sub = parser.add_subparsers(dest="command", required=True)

        # sync
        p_sync = sub.add_parser("sync", help="一键 pull --rebase + push 个人分支")
        p_sync.add_argument("--branch", default=None, help="个人分支名（默认走 config.personal_branch）")
        p_sync.add_argument("--target", default=None, help="目标分支（默认 main）")
        p_sync.add_argument("--remote", default=None, help="远端名（默认 origin）")
        p_sync.add_argument("--dry-run", action="store_true", help="仅打印将执行的命令，不实际推送")

        # pr
        p_pr = sub.add_parser("pr", help="发起 Pull Request")
        p_pr.add_argument("--title", required=True, help="PR 标题")
        p_pr.add_argument("--body", default="", help="PR 描述")
        p_pr.add_argument("--branch", default=None, help="源分支（默认当前分支）")
        p_pr.add_argument("--target", default=None, help="目标分支（默认 main）")

        # recall
        p_recall = sub.add_parser("recall", help="召回资产")
        p_recall.add_argument("--query", "-q", default=None, help="召回查询")
        p_recall.add_argument("--module", default=None, help="显式 module_path")
        p_recall.add_argument("--agent-id", default=None, help="Agent ID（默认走 config）")
        p_recall.add_argument("--consistency", default="eventual", choices=["eventual", "strict"])
        p_recall.add_argument("--limit", type=int, default=10)
        p_recall.add_argument("--read", default=None, help="读取指定 asset_id 详情")
        p_recall.add_argument("--no-adopt-record", action="store_true", help="不记录采纳率事件")

        # category-suggest
        p_cat = sub.add_parser(
            "category-suggest", help="推荐资产分类（POST /v1/category/suggest）"
        )
        p_cat.add_argument("--content", required=True, help="资产正文")
        p_cat.add_argument("--module", default=None, help="module_path（可选）")

        # cost-estimate
        p_cost = sub.add_parser(
            "cost-estimate", help="估算一级提炼 LLM 成本（依赖 Agent 7 LLMProvider，未就绪用占位）"
        )
        p_cost.add_argument("--sessions", type=int, default=10, help="预估会话数")
        p_cost.add_argument("--avg-tokens", type=int, default=2000, help="单会话平均 token 数")
        p_cost.add_argument("--model", default="gpt-4o-mini", help="LLM 模型名")

        # index-reconcile
        p_idx = sub.add_parser(
            "index-reconcile", help="重建本地 manifest 索引 + 触发服务端 reconciliation（占位）"
        )
        p_idx.add_argument("--rebuild-manifest", action="store_true", help="强制重建本地 manifest")
        p_idx.add_argument("--trigger-remote", action="store_true", help="触发服务端 reconciliation")
        p_idx.add_argument("--check-gitignore", action="store_true", help="检查 .gitignore 私有资产隔离")

        # ask-peer（Task 21）
        p_ask = sub.add_parser(
            "ask-peer", help="向 peer AI 提问（自动选择在线实时 / 离线影子）"
        )
        p_ask.add_argument("--peer", required=True, help="目标 peer 的成员 ID")
        p_ask.add_argument("--question", "-q", required=True, help="提问内容")
        p_ask.add_argument("--in-reply-to", default=None, help="回复链（上一条事件 ID）")

        # peers（Task 21）
        p_peers = sub.add_parser("peers", help="列出已知 peer")
        p_peers.add_argument("--verbose", "-v", action="store_true", help="显示详细信息（含在线状态）")

        # shadow-log（Task 21）
        p_log = sub.add_parser(
            "shadow-log", help="查看交流报告（含实时 + 影子）"
        )
        p_log.add_argument("--peer", default=None, help="过滤指定 peer 的交流记录")
        p_log.add_argument("--limit", type=int, default=50, help="返回记录数上限")
        p_log.add_argument(
            "--type",
            default=None,
            choices=["ask", "realtime_answer", "simulated_answer", "confirmed", "revised", "needs_human_review"],
            help="按事件类型过滤",
        )

        return parser

    def run(self, argv: Sequence[str] | None = None) -> int:
        """执行命令，返回退出码。"""
        parser = self.build_parser()
        try:
            args = parser.parse_args(list(argv) if argv is not None else None)
        except SystemExit as exc:
            return int(exc.code) if exc.code is not None else 1

        try:
            result = self._dispatch(args)
            self._print(result)
            return 0 if result.success else 1
        except CliError as exc:
            self._err(str(exc))
            return 1
        except Exception as exc:  # noqa: BLE001
            self._err(f"未预期错误: {exc}")
            return 1

    # ------------------------------------------------------------------
    # 分发
    # ------------------------------------------------------------------

    def _dispatch(self, args: argparse.Namespace) -> CliResult:
        cmd = args.command
        if cmd == "sync":
            return self._cmd_sync(args)
        if cmd == "pr":
            return self._cmd_pr(args)
        if cmd == "recall":
            return self._cmd_recall(args)
        if cmd == "category-suggest":
            return self._cmd_category_suggest(args)
        if cmd == "cost-estimate":
            return self._cmd_cost_estimate(args)
        if cmd == "index-reconcile":
            return self._cmd_index_reconcile(args)
        if cmd == "ask-peer":
            return self._cmd_ask_peer(args)
        if cmd == "peers":
            return self._cmd_peers(args)
        if cmd == "shadow-log":
            return self._cmd_shadow_log(args)
        raise CliError(f"未知命令: {cmd}")

    # ------------------------------------------------------------------
    # sync
    # ------------------------------------------------------------------

    def _cmd_sync(self, args: argparse.Namespace) -> CliResult:
        """teamharness sync：pull --rebase + push。"""
        branch = args.branch or self.config.resolve_personal_branch()
        target = args.target or self.config.target_branch
        warnings: list[str] = []

        # 前置：私有资产隔离检查
        pi = PrivateIsolation(self.config.resolve_repo_root())
        gi_status = pi.check_gitignore()
        if not gi_status.ok:
            warnings.append(
                f".gitignore 缺少私有资产隔离规则: {gi_status.missing_rules}；建议先执行 index-reconcile --check-gitignore"
            )

        if args.dry_run:
            return CliResult(
                command="sync",
                success=True,
                data={
                    "dry_run": True,
                    "branch": branch,
                    "target": target,
                    "remote": args.remote or "origin",
                    "steps": [
                        f"git fetch {args.remote or 'origin'}",
                        f"git rebase {args.remote or 'origin'}/{target}",
                        f"git push -u {args.remote or 'origin'} {branch}",
                    ],
                },
                message="dry-run：未实际执行 git 操作",
                warnings=warnings,
            )

        sync = GitSync(
            self.config.resolve_repo_root(),
            default_remote=args.remote or "origin",
        )
        if not sync.has_git():
            raise CliError("当前目录不是 git 仓库（缺少 .git）")
        if sync.has_uncommitted_changes():
            warnings.append("存在未提交变更，sync 前请先 commit 或 stash")
        result = sync.sync(personal_branch=branch, target_branch=target, remote=args.remote)
        if not result.ok:
            return CliResult(
                command="sync",
                success=False,
                data={
                    "pulled": result.pulled,
                    "rebased": result.rebased,
                    "pushed": result.pushed,
                    "upstream_commit": result.upstream_commit,
                    "head_commit": result.head_commit,
                    "conflicts": result.conflicts,
                    "error": result.error,
                },
                message=result.error or "sync 失败",
                warnings=warnings,
            )
        return CliResult(
            command="sync",
            success=True,
            data={
                "pulled": result.pulled,
                "rebased": result.rebased,
                "pushed": result.pushed,
                "upstream_commit": result.upstream_commit,
                "head_commit": result.head_commit,
                "branch": branch,
                "target": target,
            },
            message="sync 完成",
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # pr
    # ------------------------------------------------------------------

    def _cmd_pr(self, args: argparse.Namespace) -> CliResult:
        """teamharness pr：发起 PR。"""
        # 前置：创建 GitProvider（可选，仅用于 HTTP API 创建 PR）
        from server.infra_git.git_provider import create_git_provider

        git_provider = None
        try:
            # 按 GIT_PROVIDER 环境变量创建；缺省返回 libgit2 不支持 HTTP PR（走降级）
            git_provider = create_git_provider()
        except Exception:  # noqa: BLE001
            git_provider = None

        sync = GitSync(
            self.config.resolve_repo_root(),
            git_provider=git_provider,
        )
        result = sync.create_pr(
            title=args.title,
            body=args.body,
            branch=args.branch,
            target=args.target or self.config.target_branch,
        )
        if result.created:
            return CliResult(
                command="pr",
                success=True,
                data={
                    "pr_id": result.pr_id,
                    "pr_url": result.pr_url,
                    "branch": result.branch,
                    "target": result.target,
                },
                message=f"PR #{result.pr_id} 已创建: {result.pr_url}",
            )
        # 降级路径
        if result.fallback_message:
            return CliResult(
                command="pr",
                success=False,
                data={"branch": result.branch, "target": result.target},
                message=result.fallback_message,
            )
        raise CliError(result.error or "PR 创建失败")

    # ------------------------------------------------------------------
    # recall
    # ------------------------------------------------------------------

    def _cmd_recall(self, args: argparse.Namespace) -> CliResult:
        """teamharness recall：召回或读取。"""
        client = RecallClient(self.config)
        agent_id = args.agent_id or self.config.agent_id
        if not agent_id:
            raise CliError("缺少 agent_id（请通过 --agent-id 或 config 指定）")

        if args.read:
            # recall --read <asset_id>
            result = client.recall_read(agent_id=agent_id, asset_id=args.read)
            if result.gone:
                return CliResult(
                    command="recall",
                    success=False,
                    data={"asset_id": args.read, "alternative_asset_ids": result.alternative_asset_ids},
                    message="资产已删除（410 Gone）",
                )
            # 记录采纳率事件
            if not args.no_adopt_record:
                reporter = AdoptionReporter(self.config)
                reporter.record_view(asset_id=args.read, agent_id=agent_id)
            return CliResult(
                command="recall",
                success=True,
                data={
                    "asset_id": args.read,
                    "content": result.content,
                    "frontmatter": result.frontmatter,
                },
                message="读取成功",
            )

        # recall --query ...
        result = client.recall_list(
            agent_id=agent_id,
            query=args.query,
            module_path=args.module,
            consistency=args.consistency,
            limit=args.limit,
        )
        items_data = [
            {
                "asset_id": it.asset_id,
                "type": it.type,
                "title": it.title,
                "tags": it.tags,
                "relevance_score": it.relevance_score,
                "git_path": it.git_path,
                "module_path": it.module_path,
            }
            for it in result.items
        ]
        # 记录召回事件
        if not args.no_adopt_record:
            reporter = AdoptionReporter(self.config)
            for it in result.items:
                reporter.record_recall(
                    asset_id=it.asset_id,
                    agent_id=agent_id,
                    module_path=it.module_path,
                    metadata={"relevance_score": it.relevance_score},
                )
        return CliResult(
            command="recall",
            success=True,
            data={
                "items": items_data,
                "count": len(items_data),
                "as_of_commit": result.as_of_commit,
                "sync_lag_seconds": result.sync_lag_seconds,
                "degraded": result.degraded,
            },
            message="召回完成" + ("（离线降级为本地模式）" if result.degraded else ""),
        )

    # ------------------------------------------------------------------
    # category-suggest
    # ------------------------------------------------------------------

    def _cmd_category_suggest(self, args: argparse.Namespace) -> CliResult:
        """teamharness category-suggest：调 /v1/category/suggest。

        Agent 5 未就绪时降级到 mock_category_suggest（返回空列表 + 提示）。
        """
        candidates: list[str] = []
        source = "mock"
        if self.config.server_url:
            try:
                import httpx

                client = httpx.Client()
                resp = client.post(
                    f"{self.config.server_url}/v1/category/suggest",
                    json={"content": args.content, "module_path": args.module},
                    headers=self._auth_headers(),
                    timeout=self.config.request_timeout_seconds,
                )
                if resp.status_code < 400:
                    data = resp.json()
                    candidates = list(data.get("candidates") or [])
                    source = "remote"
                else:
                    candidates = mock_category_suggest(args.content, args.module)
            except Exception:  # noqa: BLE001 - Agent 5 未就绪
                candidates = mock_category_suggest(args.content, args.module)
        else:
            candidates = mock_category_suggest(args.content, args.module)

        return CliResult(
            command="category-suggest",
            success=True,
            data={"candidates": candidates, "source": source, "count": len(candidates)},
            message=(
                "已获取候选分类"
                if source == "remote"
                else "装配服务未就绪（Agent 5 占位），返回空候选列表"
            ),
            warnings=[] if source == "remote" else ["当前为占位结果，Agent 5 就绪后切换真实调用"],
        )

    # ------------------------------------------------------------------
    # cost-estimate
    # ------------------------------------------------------------------

    def _cmd_cost_estimate(self, args: argparse.Namespace) -> CliResult:
        """teamharness cost-estimate：估算一级提炼 LLM 成本。

        Agent 7 LLMProvider 未就绪时用本地占位估算（粗略公式）。
        """
        # 简化估算：单会话 token * 会话数 * 三阶段系数 (Light 0.2 + REM 0.3 + Deep 1.5)
        # 实际成本由 Agent 7 通过 /v1/llm/budget 精确查询
        total_tokens = args.sessions * args.avg_tokens
        # 各阶段 token 系数
        light_tokens = int(total_tokens * 0.2)
        rem_tokens = int(total_tokens * 0.3)
        deep_tokens = int(total_tokens * 1.5)
        all_stages_tokens = light_tokens + rem_tokens + deep_tokens
        # 模型单价（占位：gpt-4o-mini $0.15/1M input, $0.60/1M output）
        # 真实单价由 Agent 7 LLMProvider 提供
        pricing = {
            "gpt-4o-mini": {"input": 0.15, "output": 0.60},
            "gpt-4o": {"input": 2.50, "output": 10.00},
            "claude-3-5-sonnet": {"input": 3.00, "output": 15.00},
        }
        unit = pricing.get(args.model, {"input": 1.0, "output": 5.0})
        # 假设 input:output = 3:1
        input_tokens = int(all_stages_tokens * 0.75)
        output_tokens = all_stages_tokens - input_tokens
        cost_usd = (
            input_tokens / 1_000_000 * unit["input"]
            + output_tokens / 1_000_000 * unit["output"]
        )
        return CliResult(
            command="cost-estimate",
            success=True,
            data={
                "model": args.model,
                "sessions": args.sessions,
                "avg_tokens_per_session": args.avg_tokens,
                "stages": {
                    "light": light_tokens,
                    "rem": rem_tokens,
                    "deep": deep_tokens,
                },
                "total_tokens": all_stages_tokens,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "estimated_cost_usd": round(cost_usd, 4),
                "pricing_per_million": unit,
                "source": "local-estimate",
            },
            message="成本估算完成（占位算法，Agent 7 LLMProvider 就绪后切换 /v1/llm/budget 精确查询）",
            warnings=["当前为本地占位估算，实际成本以服务端 LLMProvider 为准"],
        )

    # ------------------------------------------------------------------
    # index-reconcile
    # ------------------------------------------------------------------

    def _cmd_index_reconcile(self, args: argparse.Namespace) -> CliResult:
        """teamharness index-reconcile：重建本地 manifest + 触发服务端 reconciliation。"""
        warnings: list[str] = []
        repo_root = self.config.resolve_repo_root()

        # 1. 私有资产隔离检查（--check-gitignore 触发修复）
        pi = PrivateIsolation(repo_root)
        if args.check_gitignore:
            status = pi.ensure_gitignore(append=True)
            if status.fixed:
                warnings.append(f".gitignore 已自动追加缺失规则: {status.missing_rules}")
            elif not status.ok:
                warnings.append(f".gitignore 仍缺规则（append=False）: {status.missing_rules}")
        else:
            status = pi.check_gitignore()
            if not status.ok:
                warnings.append(
                    f".gitignore 缺少私有资产隔离规则: {status.missing_rules}；"
                    "建议追加 --check-gitignore 自动修复"
                )

        # 2. 重建本地 manifest
        builder = ManifestBuilder(repo_root)
        head_commit = ""
        try:
            from server.client.git_sync import GitSync
            head_commit = GitSync(repo_root).current_commit()
        except Exception:  # noqa: BLE001
            pass
        manifest = builder.build(head_commit=head_commit)
        if args.rebuild_manifest or not builder.manifest_path.is_file():
            builder.save(manifest)
        # 比对旧 manifest
        old = builder.load()
        diff = builder.diff(old, manifest) if old else {"added": [], "modified": [], "deleted": []}

        # 3. 触发服务端 reconciliation（占位，Agent 2 SyncService 提供）
        remote_triggered = False
        remote_error: str | None = None
        if args.trigger_remote and self.config.server_url:
            try:
                import httpx

                client = httpx.Client()
                resp = client.post(
                    f"{self.config.server_url}/v1/sync/reconcile",
                    headers=self._auth_headers(),
                    timeout=self.config.request_timeout_seconds,
                )
                remote_triggered = resp.status_code < 400
                if not remote_triggered:
                    remote_error = f"HTTP {resp.status_code}"
            except Exception as exc:  # noqa: BLE001
                remote_error = str(exc)

        return CliResult(
            command="index-reconcile",
            success=True,
            data={
                "head_commit": head_commit,
                "manifest_path": str(builder.manifest_path),
                "manifest": manifest.to_dict(),
                "diff": diff,
                "gitignore_ok": status.ok,
                "remote_triggered": remote_triggered,
                "remote_error": remote_error,
            },
            message="本地 manifest 已重建" + ("（已触发服务端 reconciliation）" if remote_triggered else ""),
            warnings=warnings,
        )

    # ------------------------------------------------------------------
    # ask-peer / peers / shadow-log（Task 21）
    # ------------------------------------------------------------------

    def _build_transport(self) -> SyncTransport:
        """根据 config.topology 创建 transport 实例。

        若构造时注入了 transport（用于测试），直接返回注入的实例；
        否则按 config.topology 选择实现：
        - central → CentralSyncTransport（复用 server_url / api_key）
        - p2p → P2PSyncTransport（复用 config.peers）
        - hybrid → HybridSyncTransport（组合 central + p2p）
        """
        if self._transport is not None:
            return self._transport
        topology = self.config.topology
        if topology == TOPOLOGY_P2P:
            return P2PSyncTransport(peers=list(self.config.peers))
        if topology == TOPOLOGY_HYBRID:
            central = CentralSyncTransport(
                server_url=self.config.server_url,
                api_key=self.config.api_key,
                timeout=self.config.request_timeout_seconds,
            )
            p2p = P2PSyncTransport(peers=list(self.config.peers))
            return HybridSyncTransport(central=central, p2p=p2p)
        # 默认 central
        return CentralSyncTransport(
            server_url=self.config.server_url,
            api_key=self.config.api_key,
            timeout=self.config.request_timeout_seconds,
        )

    def _build_peer_comm(self) -> PeerComm:
        """构建 PeerComm 实例（含 transport / mailbox / conversation_log / peer_snapshot_manager / shadow_comm）。

        所有持久化路径位于 ``.teamharness/async_comm/`` 下，与 MCP Server 共享同一套底层。
        """
        async_comm_dir = self.config.resolve_repo_root() / ".teamharness" / "async_comm"
        async_comm_dir.mkdir(parents=True, exist_ok=True)
        member_id = self.config.member_id or "default"

        transport = self._build_transport()
        mailbox = Mailbox(async_comm_dir, member_id)
        conversation_log = ConversationLog(async_comm_dir / "conversation.jsonl")
        peer_snapshot_manager = PeerSnapshotManager(async_comm_dir)

        async_cfg = self.config.resolve_async_comm_config()
        snapshot_ttl_days = int(async_cfg.get("snapshot_ttl_days", 30))
        shadow_comm = ShadowComm(
            mailbox=mailbox,
            peer_snapshot_manager=peer_snapshot_manager,
            conversation_log=conversation_log,
            member_id=member_id,
            snapshot_ttl_days=snapshot_ttl_days,
        )

        return PeerComm(
            transport=transport,
            mailbox=mailbox,
            conversation_log=conversation_log,
            peer_snapshot_manager=peer_snapshot_manager,
            member_id=member_id,
            network_check_interval_seconds=self.config.network_check_interval_seconds,
            shadow_comm=shadow_comm,
        )

    def _cmd_ask_peer(self, args: argparse.Namespace) -> CliResult:
        """teamharness ask-peer：向 peer AI 提问。

        自动选择在线实时 / 离线影子路径，返回回答事件与降级标记。
        """
        peer_comm = self._build_peer_comm()
        in_reply_to = args.in_reply_to or ""
        event = peer_comm.ask_peer(
            args.peer,
            args.question,
            in_reply_to=in_reply_to,
        )
        answer = str(event.payload.get("answer", ""))
        return CliResult(
            command="ask-peer",
            success=True,
            data={
                "peer_id": args.peer,
                "question": args.question,
                "answer": answer,
                "event_id": event.event_id,
                "degraded": event.degraded,
                "realtime": event.realtime,
                "based_on": event.based_on,
                "snapshot_stale": event.snapshot_stale,
            },
            message=(
                "已收到实时回答" if event.realtime
                else "peer 离线，已生成影子联络模拟回答"
            ),
        )

    def _cmd_peers(self, args: argparse.Namespace) -> CliResult:
        """teamharness peers：列出已知 peer。"""
        peer_comm = self._build_peer_comm()
        peers = peer_comm.list_peers()
        return CliResult(
            command="peers",
            success=True,
            data={"peers": list(peers), "count": len(peers)},
            message=f"发现 {len(peers)} 个 peer",
        )

    def _cmd_shadow_log(self, args: argparse.Namespace) -> CliResult:
        """teamharness shadow-log：查看交流报告。

        按 --peer / --type / --limit 过滤 ConversationLog 中的事件。
        """
        async_comm_dir = self.config.resolve_repo_root() / ".teamharness" / "async_comm"
        log = ConversationLog(async_comm_dir / "conversation.jsonl")

        if args.peer:
            events = log.load_by_peer(args.peer, limit=args.limit)
        else:
            events = log.load_all(limit=args.limit)

        if args.type:
            events = [e for e in events if e.event_type == args.type]

        events_data = [
            {
                "event_id": e.event_id,
                "event_type": e.event_type,
                "peer_id": e.peer_id,
                "timestamp": e.timestamp,
                "degraded": e.degraded,
                "realtime": e.realtime,
                "based_on": e.based_on,
                "snapshot_stale": e.snapshot_stale,
                "payload": dict(e.payload),
            }
            for e in events
        ]
        return CliResult(
            command="shadow-log",
            success=True,
            data={"events": events_data, "count": len(events_data)},
            message=f"返回 {len(events_data)} 条交流记录",
        )

    # ------------------------------------------------------------------
    # 工具
    # ------------------------------------------------------------------

    def _auth_headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.config.api_key:
            headers["Authorization"] = f"Bearer {self.config.api_key}"
        return headers

    def _print(self, result: CliResult) -> None:
        data = result.to_dict()
        try:
            print(json.dumps(data, ensure_ascii=False, indent=2), file=self.stdout)
        except (TypeError, ValueError):
            # 兜底：去掉不可序列化字段
            data["data"] = {"_serialize_error": "data 不可 JSON 序列化"}
            print(json.dumps(data, ensure_ascii=False, indent=2), file=self.stdout)

    def _err(self, msg: str) -> None:
        print(json.dumps({"error": msg}, ensure_ascii=False), file=self.stderr)


# ---------------------------------------------------------------------------
# 模块入口
# ---------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    """teamharness CLI 入口（pyproject.toml console_scripts 可注册）。"""
    cli = ClientCLI()
    return cli.run(argv)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())


__all__ = [
    "CliError",
    "CliResult",
    "ClientCLI",
    "main",
]
