#!/bin/bash
# TeamHarness 客户端容器入口脚本
#
# 模式：
#   client-entrypoint.sh init      → 克隆仓库 + 写 .teamharness/config.yaml 后退出
#   client-entrypoint.sh daemon    → 启动 ClientDaemon.run_forever()（常驻）
#   client-entrypoint.sh <cmd> ... → 执行 teamharness <cmd>（sync/pr/recall/...）后退出
#
# 必需环境变量：
#   TEAMHARNESS_SERVER_URL  服务端基址（如 http://nginx:80 或 http://asset-service:8080）
#   TEAMHARNESS_API_KEY     成员 API Key（由 init-team.sh 颁发后注入）
#   TEAMHARNESS_MEMBER_ID   成员标识（如 alice）
#   GIT_REPO_URL            gitea 仓库克隆 URL（如 http://gitea:3000/teamharness/teamharness-shared.git）
#   GIT_REPO_BRANCH         初始分支（默认 main）
#
# 可选环境变量：
#   TEAMHARNESS_AGENT_ID    Agent 标识（缺省由服务端从 API Key 反查）
#   TEAMHARNESS_PERSONAL_BRANCH  个人分支名（默认 members/<member_id>）
#   TEAMHARNESS_SYNC_STRATEGY    同步策略（manual/auto，默认 manual）

set -euo pipefail

MODE="${1:-daemon}"
shift || true  # 移除第一个参数，剩余 $@ 传给 CLI

REPO_ROOT="${TEAMHARNESS_REPO_ROOT:-/home/teamharness/workspace}"
CONFIG_DIR="${TEAMHARNESS_CONFIG_DIR:-/home/teamharness/.teamharness}"
CONFIG_FILE="${CONFIG_DIR}/config.yaml"

# 必需环境变量校验
: "${TEAMHARNESS_SERVER_URL:?TEAMHARNESS_SERVER_URL 未设置}"
: "${TEAMHARNESS_MEMBER_ID:?TEAMHARNESS_MEMBER_ID 未设置}"

PERSONAL_BRANCH="${TEAMHARNESS_PERSONAL_BRANCH:-members/${TEAMHARNESS_MEMBER_ID}}"

# ---------------------------------------------------------------------------
# 写 .teamharness/config.yaml
# ---------------------------------------------------------------------------
write_config() {
    mkdir -p "${CONFIG_DIR}" "${REPO_ROOT}"
    cat > "${CONFIG_FILE}" <<EOF
# 由 client-entrypoint.sh 自动生成，勿手动编辑
server_url: "${TEAMHARNESS_SERVER_URL}"
api_key: "${TEAMHARNESS_API_KEY:-}"
agent_id: "${TEAMHARNESS_AGENT_ID:-}"
member_id: "${TEAMHARNESS_MEMBER_ID}"
repo_root: "${REPO_ROOT}"
mapping_path: "${CONFIG_DIR}/mapping.yaml"
manifest_path: "${CONFIG_DIR}/manifest.json"
private_dir: "${CONFIG_DIR}/private"
sync_strategy: "${TEAMHARNESS_SYNC_STRATEGY:-manual}"
personal_branch: "${PERSONAL_BRANCH}"
target_branch: "main"
distill_schedule_cron: "0 2 * * *"
network_check_interval_seconds: 60
adoption_flush_interval_seconds: 300
request_timeout_seconds: 15
offline_recall_local_only: true
EOF
    echo "[client-entrypoint] 配置已写入 ${CONFIG_FILE}"
}

# ---------------------------------------------------------------------------
# init 模式：克隆仓库 + 写配置
# ---------------------------------------------------------------------------
do_init() {
    : "${GIT_REPO_URL:?GIT_REPO_URL 未设置（init 模式必需）}"
    local branch="${GIT_REPO_BRANCH:-main}"

    write_config

    # 克隆仓库（若已存在则 pull）
    if [ -d "${REPO_ROOT}/.git" ]; then
        echo "[client-entrypoint] 仓库已存在，执行 pull"
        cd "${REPO_ROOT}"
        git fetch origin
        git checkout "${branch}"
        git pull --rebase origin "${branch}" || true
    else
        echo "[client-entrypoint] 克隆仓库 ${GIT_REPO_URL} → ${REPO_ROOT}"
        git clone --branch "${branch}" "${GIT_REPO_URL}" "${REPO_ROOT}" \
            || git clone "${GIT_REPO_URL}" "${REPO_ROOT}"
        cd "${REPO_ROOT}"
    fi

    # 创建个人分支（若不存在）
    if ! git show-ref --verify --quiet "refs/heads/${PERSONAL_BRANCH}"; then
        git checkout -b "${PERSONAL_BRANCH}" || true
    else
        git checkout "${PERSONAL_BRANCH}" || true
    fi

    # 配置 git user（提交需要）
    git config user.name "${TEAMHARNESS_MEMBER_ID}"
    git config user.email "${TEAMHARNESS_MEMBER_ID}@teamharness.local"

    echo "[client-entrypoint] init 完成"
}

# ---------------------------------------------------------------------------
# daemon 模式：启动守护进程
# ---------------------------------------------------------------------------
do_daemon() {
    if [ ! -f "${CONFIG_FILE}" ]; then
        echo "[client-entrypoint] 配置不存在，先执行 init" >&2
        exit 1
    fi
    echo "[client-entrypoint] 启动守护进程（member=${TEAMHARNESS_MEMBER_ID}）"
    exec python -c "
import sys
sys.path.insert(0, '/app')
from server.client.daemon import ClientDaemon
from server.client.config import load_client_config
config = load_client_config()
daemon = ClientDaemon(config, foreground=True)
daemon.run_forever()
"
}

# ---------------------------------------------------------------------------
# CLI 模式：执行 teamharness <cmd>
# ---------------------------------------------------------------------------
do_cli() {
    if [ ! -f "${CONFIG_FILE}" ]; then
        echo "[client-entrypoint] 配置不存在，先执行 init" >&2
        exit 1
    fi
    echo "[client-entrypoint] 执行 CLI: teamharness $*"
    exec python -c "
import sys
sys.path.insert(0, '/app')
from server.client.cli import main
sys.exit(main())
" "$@"
}

# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
case "${MODE}" in
    init)
        do_init
        ;;
    daemon)
        do_daemon
        ;;
    sync|pr|recall|category-suggest|cost-estimate|index-reconcile)
        do_cli "${MODE}" "$@"
        ;;
    *)
        echo "[client-entrypoint] 未知模式: ${MODE}" >&2
        echo "用法: client-entrypoint.sh [init|daemon|sync|pr|recall|...]" >&2
        exit 1
        ;;
esac
