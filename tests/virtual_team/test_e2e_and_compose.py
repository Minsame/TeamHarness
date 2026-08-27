"""端到端流程 + 编排逻辑测试。

验证点：
1. 端到端流程顺序正确（apikey → recall → read → metrics → dashboard → dedup）
2. docker-compose.clients.yaml 编排文件语法正确
3. client.Dockerfile 构建逻辑正确
4. 场景脚本文件存在且可执行
"""

from __future__ import annotations

import os
import time
from concurrent.futures import as_completed
from pathlib import Path

import pytest
import yaml

from tests.virtual_team.conftest import VirtualMember


# ---------------------------------------------------------------------------
# 端到端流程测试
# ---------------------------------------------------------------------------


class TestE2EFlow:
    """端到端流程顺序测试（单进程模拟）。"""

    def test_full_e2e_flow(self, mock_server, team_3):
        """完整流程：apikey → write → recall → read → metrics → dashboard → dedup。"""
        alice = team_3[0]

        # 步骤 1：颁发 API Key
        key_result = mock_server.issue_apikey(alice.member_id)
        assert key_result["api_key"] == f"key-{alice.member_id}"
        assert len(mock_server.apikey_issues) == 1

        # 步骤 2：模拟写入资产（webhook）
        webhook_result = mock_server.webhook(
            ref=f"refs/heads/{alice.personal_branch}",
            commits=[{"id": "e2e-001", "message": "feat: add asset"}],
        )
        assert webhook_result["status"] == "accepted"
        assert len(mock_server.webhook_events) == 1

        # 步骤 3：召回资产
        recall_result = mock_server.recall_list(
            agent_id=alice.agent_id, query="lint rule"
        )
        assert "assets" in recall_result
        assert len(mock_server.recall_requests) == 1

        # 步骤 4：读取资产（采纳）
        read_result = mock_server.recall_read(
            agent_id=alice.agent_id, asset_id="e2e-001"
        )
        assert read_result["asset_id"] == "e2e-001"
        assert len(mock_server.read_requests) == 1

        # 步骤 5：上报 metrics
        metrics_result = mock_server.metrics_batch(
            agent_id=alice.agent_id,
            event_id="evt-e2e-001",
            type="recall",
            count=1,
        )
        assert metrics_result["acknowledged"] is True
        assert not metrics_result["is_duplicate"]
        assert len(mock_server.metrics_events) == 1

        # 步骤 6：查看看板
        dashboard_result = mock_server.dashboard()
        assert "modules" in dashboard_result
        assert len(mock_server.dashboard_requests) == 1

        # 步骤 7：PR Review 去重
        dedup_result = mock_server.dedup(
            pr_id="e2e-pr-001",
            assets=[{"path": "modules/backend/lint.md", "content": "# Lint"}],
        )
        assert dedup_result["pr_id"] == "e2e-pr-001"
        assert len(mock_server.dedup_requests) == 1

        # 步骤 8：selfcheck
        health = mock_server.selfcheck()
        assert health["status"] == "ok"

    def test_e2e_multi_member_sequential(self, mock_server, team_3):
        """多成员顺序执行完整流程。"""
        for member in team_3:
            # 每个成员执行完整流程
            mock_server.issue_apikey(member.member_id)
            mock_server.webhook(ref=f"refs/heads/{member.personal_branch}")
            mock_server.recall_list(agent_id=member.agent_id, query="test")
            mock_server.recall_read(agent_id=member.agent_id, asset_id=f"asset-{member.member_id}")
            mock_server.metrics_batch(
                agent_id=member.agent_id,
                event_id=f"evt-{member.member_id}",
                type="recall",
            )
            mock_server.dashboard()

        # 验证全部记录
        assert len(mock_server.apikey_issues) == len(team_3)
        assert len(mock_server.webhook_events) == len(team_3)
        assert len(mock_server.recall_requests) == len(team_3)
        assert len(mock_server.read_requests) == len(team_3)
        assert len(mock_server.metrics_events) == len(team_3)
        assert len(mock_server.dashboard_requests) == len(team_3)

    def test_e2e_multi_member_concurrent(self, mock_server, team_3, thread_pool):
        """多成员并发执行完整流程。"""
        def full_flow(member: VirtualMember) -> None:
            mock_server.issue_apikey(member.member_id)
            mock_server.webhook(ref=f"refs/heads/{member.personal_branch}")
            mock_server.recall_list(agent_id=member.agent_id, query="test")
            mock_server.recall_read(agent_id=member.agent_id, asset_id=f"asset-{member.member_id}")
            mock_server.metrics_batch(
                agent_id=member.agent_id,
                event_id=f"evt-concurrent-{member.member_id}",
                type="recall",
            )
            mock_server.dashboard()
            mock_server.dedup(pr_id=f"pr-{member.member_id}")

        futures = [thread_pool.submit(full_flow, m) for m in team_3]
        for f in as_completed(futures):
            f.result()  # 确保不抛异常

        # 验证全部记录
        assert len(mock_server.apikey_issues) == len(team_3)
        assert len(mock_server.webhook_events) == len(team_3)
        assert len(mock_server.recall_requests) == len(team_3)
        assert len(mock_server.read_requests) == len(team_3)
        assert len(mock_server.metrics_events) == len(team_3)
        assert len(mock_server.dashboard_requests) == len(team_3)
        assert len(mock_server.dedup_requests) == len(team_3)


# ---------------------------------------------------------------------------
# Docker Compose 编排文件验证
# ---------------------------------------------------------------------------


class TestComposeFile:
    """docker-compose.clients.yaml 编排文件验证。"""

    @pytest.fixture
    def compose_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "deploy" / "docker-compose.clients.yaml"

    @pytest.fixture
    def compose_content(self, compose_path) -> dict:
        assert compose_path.exists(), f"compose 文件不存在: {compose_path}"
        with open(compose_path, "r", encoding="utf-8") as f:
            return yaml.safe_load(f)

    def test_compose_file_exists(self, compose_path):
        """compose 文件存在。"""
        assert compose_path.exists()

    def test_compose_valid_yaml(self, compose_content):
        """compose 文件是合法 YAML。"""
        assert isinstance(compose_content, dict)
        assert "services" in compose_content

    def test_has_init_team_service(self, compose_content):
        """有 init-team 服务。"""
        assert "init-team" in compose_content["services"]

    def test_has_client_services(self, compose_content):
        """有 alice/bob/charlie 三个客户端服务。"""
        services = compose_content["services"]
        assert "client-alice" in services
        assert "client-bob" in services
        assert "client-charlie" in services

    def test_has_5_member_services(self, compose_content):
        """有 dave/eve 五人团队服务。"""
        services = compose_content["services"]
        assert "client-dave" in services
        assert "client-eve" in services

    def test_has_scenario_services(self, compose_content):
        """有 scenario-concurrent/e2e/stress 场景服务。"""
        services = compose_content["services"]
        assert "scenario-concurrent" in services
        assert "scenario-e2e" in services
        assert "scenario-stress" in services

    def test_team_3_profile(self, compose_content):
        """alice/bob/charlie 属于 team-3 profile。"""
        services = compose_content["services"]
        for name in ("client-alice", "client-bob", "client-charlie"):
            profiles = services[name].get("profiles", [])
            assert "team-3" in profiles, f"{name} 缺少 team-3 profile"

    def test_team_5_profile(self, compose_content):
        """dave/eve 属于 team-5 profile。"""
        services = compose_content["services"]
        for name in ("client-dave", "client-eve"):
            profiles = services[name].get("profiles", [])
            assert "team-5" in profiles, f"{name} 缺少 team-5 profile"

    def test_ci_profile(self, compose_content):
        """ci profile 包含 team-3 服务。"""
        services = compose_content["services"]
        for name in ("client-alice", "client-bob", "client-charlie",
                      "scenario-concurrent", "scenario-e2e"):
            profiles = services[name].get("profiles", [])
            assert "ci" in profiles, f"{name} 缺少 ci profile"

    def test_client_services_use_client_dockerfile(self, compose_content):
        """客户端服务使用 client.Dockerfile。"""
        services = compose_content["services"]
        for name in ("client-alice", "client-bob", "client-charlie"):
            build = services[name].get("build", {})
            dockerfile = build.get("dockerfile", "")
            assert "client.Dockerfile" in dockerfile, f"{name} 未使用 client.Dockerfile"

    def test_has_volumes(self, compose_content):
        """有各成员的 workspace/config 卷 + team-keys 共享卷。"""
        volumes = compose_content.get("volumes", {})
        for name in ("team-keys", "alice-workspace", "alice-config",
                      "bob-workspace", "bob-config"):
            assert name in volumes, f"缺少卷: {name}"

    def test_uses_external_network(self, compose_content):
        """使用外部网络（与服务端 compose 共享）。"""
        networks = compose_content.get("networks", {})
        assert "teamharness-net" in networks
        assert networks["teamharness-net"].get("external") is True


# ---------------------------------------------------------------------------
# Dockerfile 验证
# ---------------------------------------------------------------------------


class TestClientDockerfile:
    """client.Dockerfile 验证。"""

    @pytest.fixture
    def dockerfile_path(self) -> Path:
        return Path(__file__).parent.parent.parent / "deploy" / "client.Dockerfile"

    @pytest.fixture
    def dockerfile_content(self, dockerfile_path) -> str:
        assert dockerfile_path.exists(), f"Dockerfile 不存在: {dockerfile_path}"
        with open(dockerfile_path, "r", encoding="utf-8") as f:
            return f.read()

    def test_dockerfile_exists(self, dockerfile_path):
        assert dockerfile_path.exists()

    def test_uses_python_base(self, dockerfile_content):
        """使用 python:3.12-slim 基础镜像。"""
        assert "python:3.12-slim" in dockerfile_content

    def test_installs_git(self, dockerfile_content):
        """安装 git（subprocess 调用需要）。"""
        assert "git" in dockerfile_content

    def test_has_entrypoint(self, dockerfile_content):
        """有 entrypoint 脚本。"""
        assert "client-entrypoint.sh" in dockerfile_content

    def test_has_non_root_user(self, dockerfile_content):
        """使用非 root 用户。"""
        assert "teamharness" in dockerfile_content
        assert "USER teamharness" in dockerfile_content

    def test_copies_server_code(self, dockerfile_content):
        """复制 server/ 代码（client 依赖 server.infra_git）。"""
        assert "COPY server/" in dockerfile_content


# ---------------------------------------------------------------------------
# 场景脚本验证
# ---------------------------------------------------------------------------


class TestScenarioScripts:
    """场景脚本文件验证。"""

    @pytest.fixture
    def scripts_dir(self) -> Path:
        return Path(__file__).parent.parent.parent / "deploy" / "virtual-team"

    def test_concurrent_script_exists(self, scripts_dir):
        assert (scripts_dir / "scenario-concurrent.sh").exists()

    def test_e2e_script_exists(self, scripts_dir):
        assert (scripts_dir / "scenario-e2e.sh").exists()

    def test_stress_script_exists(self, scripts_dir):
        assert (scripts_dir / "scenario-stress.sh").exists()

    def test_concurrent_script_has_tests(self, scripts_dir):
        """并发场景脚本含 recall/metrics/webhook/apikey 测试。"""
        content = (scripts_dir / "scenario-concurrent.sh").read_text(encoding="utf-8")
        assert "recall" in content
        assert "metrics" in content
        assert "webhook" in content
        assert "apikey" in content

    def test_e2e_script_has_steps(self, scripts_dir):
        """端到端脚本含 health/recall/dashboard/dedup 步骤。"""
        content = (scripts_dir / "scenario-e2e.sh").read_text(encoding="utf-8")
        assert "selfcheck" in content
        assert "recall" in content
        assert "dashboard" in content
        assert "dedup" in content

    def test_stress_script_has_duration(self, scripts_dir):
        """压力测试脚本含 DURATION 和 QPS 参数。"""
        content = (scripts_dir / "scenario-stress.sh").read_text(encoding="utf-8")
        assert "DURATION" in content
        assert "QPS" in content
        assert "selfcheck" in content
