"""并发冲突场景测试 — 多成员同时操作。

验证点：
1. 并发 recall 请求 → mock 服务端线程安全
2. 并发 metrics 上报（含重复 event_id）→ 幂等去重
3. 并发 webhook 触发 → 服务端不崩溃
4. 并发 API Key 颁发 → 幂等
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import as_completed

import pytest

from tests.virtual_team.conftest import VirtualMember


class TestConcurrentRecall:
    """并发召回测试。"""

    def test_concurrent_recall_thread_safety(
        self,
        mock_server,
        team_3,
        thread_pool,
    ):
        """3 成员同时发起 recall 请求，验证 mock 服务端线程安全。"""
        members = team_3
        futures = []

        for member in members:
            future = thread_pool.submit(
                mock_server.recall_list,
                agent_id=member.agent_id,
                query="lint rule",
            )
            futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        # 全部返回有效结果
        assert len(results) == len(members)
        for r in results:
            assert "assets" in r
            assert "total" in r

        # 服务端记录了全部请求
        assert len(mock_server.recall_requests) == len(members)
        agent_ids = {req["agent_id"] for req in mock_server.recall_requests}
        assert agent_ids == {m.agent_id for m in members}

    def test_concurrent_recall_high_volume(self, mock_server, team_5, thread_pool):
        """5 成员各发 20 次 recall，验证高并发下不丢请求。"""
        total_per_member = 20
        expected_total = len(team_5) * total_per_member
        futures = []

        for member in team_5:
            for i in range(total_per_member):
                future = thread_pool.submit(
                    mock_server.recall_list,
                    agent_id=member.agent_id,
                    query=f"query-{i}",
                )
                futures.append(future)

        for f in as_completed(futures):
            f.result()  # 确保不抛异常

        assert len(mock_server.recall_requests) == expected_total

    def test_concurrent_read(self, mock_server, team_3, thread_pool):
        """多成员同时 read 同一资产。"""
        asset_id = "shared-asset-001"
        futures = []

        for member in team_3:
            for _ in range(5):
                future = thread_pool.submit(
                    mock_server.recall_read,
                    agent_id=member.agent_id,
                    asset_id=asset_id,
                )
                futures.append(future)

        results = [f.result() for f in as_completed(futures)]
        assert len(results) == len(team_3) * 5
        assert len(mock_server.read_requests) == len(team_3) * 5

        # 全部 read 的是同一资产
        assert all(req["asset_id"] == asset_id for req in mock_server.read_requests)


class TestConcurrentMetrics:
    """并发 metrics 上报 + 幂等去重测试。"""

    def test_concurrent_metrics_with_duplicate_event_id(self, mock_server, team_3, thread_pool):
        """多成员同时发送相同 event_id 的 metrics → 幂等去重。"""
        shared_event_id = "evt-shared-dup"
        futures = []

        for member in team_3:
            for _ in range(3):
                future = thread_pool.submit(
                    mock_server.metrics_batch,
                    agent_id=member.agent_id,
                    event_id=shared_event_id,
                    type="recall",
                    count=1,
                )
                futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        # 全部发送 9 次（3 成员 × 3 次）
        assert len(results) == 9

        # 只有第一次 is_duplicate=False，其余都是 True
        non_dup_count = sum(1 for r in results if not r["is_duplicate"])
        assert non_dup_count == 1

        # mock 服务端记录了全部 9 次（但去重标记不同）
        assert len(mock_server.metrics_events) == 9
        dup_count = sum(1 for e in mock_server.metrics_events if e["is_duplicate"])
        assert dup_count == 8

    def test_concurrent_metrics_distinct_event_ids(self, mock_server, team_5, thread_pool):
        """每个成员发不同 event_id 的 metrics → 全部不重复。"""
        futures = []

        for member in team_5:
            for i in range(5):
                future = thread_pool.submit(
                    mock_server.metrics_batch,
                    agent_id=member.agent_id,
                    event_id=f"evt-{member.member_id}-{i}",
                    type="recall",
                    count=i + 1,
                )
                futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        # 全部 25 次，无一重复
        assert len(results) == 25
        assert all(not r["is_duplicate"] for r in results)


class TestConcurrentWebhook:
    """并发 webhook 触发测试。"""

    def test_concurrent_webhook_no_crash(self, mock_server, team_3, thread_pool):
        """多成员同时触发 webhook → 服务端不崩溃。"""
        futures = []

        for member in team_3:
            future = thread_pool.submit(
                mock_server.webhook,
                ref=f"refs/heads/{member.personal_branch}",
                commits=[{"id": f"commit-{member.member_id}", "message": "test"}],
            )
            futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        assert len(results) == len(team_3)
        assert all(r["status"] == "accepted" for r in results)
        assert len(mock_server.webhook_events) == len(team_3)

    def test_concurrent_webhook_same_ref(self, mock_server, team_3, thread_pool):
        """多成员同时 push 同一分支 → 全部被记录。"""
        shared_ref = "refs/heads/members/shared"
        futures = []

        for member in team_3:
            for i in range(3):
                future = thread_pool.submit(
                    mock_server.webhook,
                    ref=shared_ref,
                    commits=[{"id": f"commit-{member.member_id}-{i}"}],
                )
                futures.append(future)

        for f in as_completed(futures):
            f.result()

        assert len(mock_server.webhook_events) == len(team_3) * 3
        assert all(e["ref"] == shared_ref for e in mock_server.webhook_events)


class TestConcurrentApiKey:
    """并发 API Key 颁发测试。"""

    def test_concurrent_apikey_same_member(self, mock_server, thread_pool):
        """同时为同一成员颁发多次 API Key → 幂等（不崩溃）。"""
        futures = []

        for _ in range(10):
            future = thread_pool.submit(
                mock_server.issue_apikey,
                member_id="alice",
            )
            futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        # 全部返回有效结果
        assert len(results) == 10
        assert all("api_key" in r for r in results)
        assert len(mock_server.apikey_issues) == 10

    def test_concurrent_apikey_different_members(self, mock_server, team_5, thread_pool):
        """同时为 5 个不同成员颁发 API Key → 全部成功。"""
        futures = []

        for member in team_5:
            future = thread_pool.submit(
                mock_server.issue_apikey,
                member_id=member.member_id,
            )
            futures.append(future)

        results = [f.result() for f in as_completed(futures)]

        assert len(results) == len(team_5)
        member_ids_issued = {req["member_id"] for req in mock_server.apikey_issues}
        assert member_ids_issued == {m.member_id for m in team_5}


class TestMixedConcurrent:
    """混合并发操作测试。"""

    def test_mixed_operations_concurrent(self, mock_server, team_3, thread_pool):
        """同时执行 recall + metrics + webhook + dashboard → 全部成功。"""
        futures = []

        for member in team_3:
            # recall
            futures.append(thread_pool.submit(
                mock_server.recall_list, agent_id=member.agent_id, query="test"
            ))
            # metrics
            futures.append(thread_pool.submit(
                mock_server.metrics_batch,
                agent_id=member.agent_id,
                event_id=f"evt-mixed-{member.member_id}",
                type="recall",
            ))
            # webhook
            futures.append(thread_pool.submit(
                mock_server.webhook, ref=f"refs/heads/{member.personal_branch}"
            ))
            # dashboard
            futures.append(thread_pool.submit(mock_server.dashboard))
            # dedup
            futures.append(thread_pool.submit(
                mock_server.dedup, pr_id=f"pr-{member.member_id}"
            ))

        results = [f.result() for f in as_completed(futures)]

        # 全部操作完成
        assert len(results) == len(team_3) * 5

        # 各类请求都有记录
        assert len(mock_server.recall_requests) == len(team_3)
        assert len(mock_server.metrics_events) == len(team_3)
        assert len(mock_server.webhook_events) == len(team_3)
        assert len(mock_server.dashboard_requests) == len(team_3)
        assert len(mock_server.dedup_requests) == len(team_3)

    def test_selfcheck_always_healthy_under_load(self, mock_server, team_3, thread_pool):
        """持续负载下 selfcheck 始终健康。"""
        futures = []

        # 发送负载
        for member in team_3:
            for _ in range(10):
                futures.append(thread_pool.submit(
                    mock_server.recall_list, agent_id=member.agent_id, query="load"
                ))

        # 同时持续检查健康
        for _ in range(5):
            futures.append(thread_pool.submit(mock_server.selfcheck))

        results = [f.result() for f in as_completed(futures)]

        # 全部完成
        assert len(results) == len(team_3) * 10 + 5

        # selfcheck 全部 OK
        checks = [r for r in results if isinstance(r, dict) and r.get("status") == "ok"]
        assert len(checks) == 5
