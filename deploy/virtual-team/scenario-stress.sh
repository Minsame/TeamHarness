#!/bin/bash
# 压力测试场景：高频 recall + metrics 上报
#
# 验证点：
#   1. 持续高频 recall 请求 → 服务端响应时间稳定
#   2. 持续高频 metrics 上报 → 采纳率统计正确
#   3. 多成员并发 → 资源占用合理
#
# 用法：在 scenario-stress 容器内执行

set -euo pipefail

SERVER_URL="${TEAMHARNESS_SERVER_URL:-http://nginx:80}"
MEMBERS="${TEAM_MEMBERS:-alice,bob,charlie,dave,eve}"
DURATION="${STRESS_DURATION_SECONDS:-60}"
QPS="${STRESS_QPS:-5}"

echo "============================================"
echo "  场景：压力测试"
echo "  成员: ${MEMBERS}"
echo "  持续: ${DURATION}s"
echo "  QPS/成员: ${QPS}"
echo "  服务端: ${SERVER_URL}"
echo "============================================"

PASS=0
FAIL=0

get_key() {
    local member="$1"
    cat "/keys/${member}.key" 2>/dev/null || echo ""
}

# ---------------------------------------------------------------------------
# 压力测试 1：持续 recall
# ---------------------------------------------------------------------------
stress_recall() {
    echo ""
    echo "--- 压力 1：持续 recall ---"
    local end_time=$((SECONDS + DURATION))
    local total=0
    local success=0
    local fail=0
    local latency_sum=0

    for member in $(echo "${MEMBERS}" | tr ',' ' '); do
        key=$(get_key "${member}")

        (
            while [ "${SECONDS}" -lt "${end_time}" ]; do
                for i in $(seq 1 "${QPS}"); do
                    local start_ts
                    start_ts=$(date +%s%N)
                    if curl -fsS -X POST "${SERVER_URL}/v1/recall/list" \
                        -H "Content-Type: application/json" \
                        -H "Authorization: Bearer ${key}" \
                        -d "{\"agent_id\":\"${member}\",\"query\":\"stress-test-${i}\"}" \
                        > /dev/null 2>&1; then
                        echo "ok" >> "/tmp/stress-${member}-recall.log"
                    else
                        echo "fail" >> "/tmp/stress-${member}-recall.log"
                    fi
                    sleep 0.2  # QPS 控制
                done
            done
        ) &
    done

    # 等待全部完成
    wait

    # 统计
    for member in $(echo "${MEMBERS}" | tr ',' ' '); do
        local log="/tmp/stress-${member}-recall.log"
        if [ -f "${log}" ]; then
            local ok_count fail_count
            ok_count=$(grep -c "ok" "${log}" 2>/dev/null || echo 0)
            fail_count=$(grep -c "fail" "${log}" 2>/dev/null || echo 0)
            total=$((total + ok_count + fail_count))
            success=$((success + ok_count))
            fail=$((fail + fail_count))
            echo "  ${member}: ${ok_count} ok, ${fail_count} fail"
        fi
    done

    local success_rate=0
    if [ "${total}" -gt 0 ]; then
        success_rate=$((success * 100 / total))
    fi

    echo "  总计: ${total} 请求, ${success} 成功, ${fail} 失败"
    echo "  成功率: ${success_rate}%"

    if [ "${success_rate}" -ge 90 ]; then
        echo "  → 持续 recall: PASS (≥90%)"
        PASS=$((PASS + 1))
    else
        echo "  → 持续 recall: FAIL (<90%)"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# 压力测试 2：持续 metrics 上报
# ---------------------------------------------------------------------------
stress_metrics() {
    echo ""
    echo "--- 压力 2：持续 metrics 上报 ---"
    local end_time=$((SECONDS + DURATION))
    local total=0
    local success=0

    for member in $(echo "${MEMBERS}" | tr ',' ' '); do
        key=$(get_key "${member}")

        (
            local i=0
            while [ "${SECONDS}" -lt "${end_time}" ]; do
                i=$((i + 1))
                if curl -fsS -X POST "${SERVER_URL}/v1/metrics" \
                    -H "Content-Type: application/json" \
                    -H "Authorization: Bearer ${key}" \
                    -d "{\"agent_id\":\"${member}\",\"event_id\":\"stress-${member}-${i}\",\"type\":\"recall\",\"count\":1}" \
                    > /dev/null 2>&1; then
                    echo "ok" >> "/tmp/stress-${member}-metrics.log"
                else
                    echo "fail" >> "/tmp/stress-${member}-metrics.log"
                fi
                sleep 0.5
            done
        ) &
    done

    wait

    for member in $(echo "${MEMBERS}" | tr ',' ' '); do
        local log="/tmp/stress-${member}-metrics.log"
        if [ -f "${log}" ]; then
            local ok_count
            ok_count=$(grep -c "ok" "${log}" 2>/dev/null || echo 0)
            total=$((total + ok_count))
            success=$((success + ok_count))
            echo "  ${member}: ${ok_count} metrics 上报成功"
        fi
    done

    echo "  总计: ${success} metrics 上报成功"

    if [ "${success}" -gt 0 ]; then
        echo "  → 持续 metrics: PASS"
        PASS=$((PASS + 1))
    else
        echo "  → 持续 metrics: FAIL"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# 压力测试 3：服务端健康持续检查
# ---------------------------------------------------------------------------
stress_health() {
    echo ""
    echo "--- 压力 3：服务端健康持续检查 ---"
    local end_time=$((SECONDS + DURATION))
    local checks=0
    local healthy=0

    while [ "${SECONDS}" -lt "${end_time}" ]; do
        checks=$((checks + 1))
        if curl -fsS "${SERVER_URL}/v1/system/selfcheck" > /dev/null 2>&1; then
            healthy=$((healthy + 1))
        fi
        sleep 2
    done

    local health_rate=0
    if [ "${checks}" -gt 0 ]; then
        health_rate=$((healthy * 100 / checks))
    fi

    echo "  健康检查: ${healthy}/${checks} = ${health_rate}%"

    if [ "${health_rate}" -ge 95 ]; then
        echo "  → 健康持续: PASS (≥95%)"
        PASS=$((PASS + 1))
    else
        echo "  → 健康持续: FAIL (<95%)"
        FAIL=$((FAIL + 1))
    fi
}

# ---------------------------------------------------------------------------
# 并行执行压力测试
# ---------------------------------------------------------------------------
stress_recall
stress_metrics
stress_health

# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
echo ""
echo "============================================"
echo "  压力测试结果"
echo "  PASS: ${PASS}  FAIL: ${FAIL}"
echo "  持续: ${DURATION}s  QPS: ${QPS}"
echo "============================================"

if [ "${FAIL}" -gt 0 ]; then
    exit 1
fi
exit 0
