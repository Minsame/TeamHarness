# TeamHarness 客户端镜像（虚拟团队测试用）
#
# 用途：在 docker-compose 中模拟多个团队成员，每个容器一个独立客户端环境
# （独立 git working copy + 独立 API Key + 独立守护进程）。
#
# 启动模式：
#   1. 守护进程常驻（默认）：docker run teamharness/client
#      → 启动 ClientDaemon.run_forever()，定时一级提炼 + 网络检测 + 采纳率上报
#   2. CLI 单命令：docker run --rm teamharness/client sync
#      → 执行 teamharness sync 后退出
#   3. 初始化：docker run --rm teamharness/client init
#      → 克隆仓库 + 写入 .teamharness/config.yaml 后退出
#
# 与服务端 Dockerfile 的区别：
#   - 不需要 libgit2-dev 编译链（client 用 subprocess git + HTTP API，pygit2 可选）
#   - 需要 git 命令行（subprocess 调用）
#   - 不暴露 HTTP 端口（client 是消费方）

FROM python:3.12-slim AS base

# 设置时区与语言
ENV TZ=Asia/Shanghai \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 系统依赖：
# - git：subprocess 调用（sync/pr/working_copy 都依赖）
# - openssh-client：git over SSH（如需，默认走 HTTP/HTTPS）
# - curl：健康检查 + init 脚本调 API
# - ca-certificates：HTTPS 证书
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
        openssh-client \
        curl \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && git --version

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    -r requirements.txt

# 复制应用代码（client 依赖 server.infra_git，需复制整个 server/）
COPY server/ ./server/
COPY deploy/ ./deploy/

# 入口脚本：在切换非 root 用户前复制并赋权
COPY deploy/client-entrypoint.sh /usr/local/bin/client-entrypoint.sh
RUN chmod +x /usr/local/bin/client-entrypoint.sh

# 创建非 root 用户运行（uid 1000，与服务端一致便于卷权限对齐）
RUN useradd --create-home --shell /bin/bash teamharness \
    && mkdir -p /home/teamharness/workspace /home/teamharness/.teamharness \
    && chown -R teamharness:teamharness /app /home/teamharness

USER teamharness
WORKDIR /home/teamharness/workspace

# 默认环境变量（可被 docker-compose env 覆盖）
ENV TEAMHARNESS_REPO_ROOT=/home/teamharness/workspace \
    TEAMHARNESS_CONFIG_DIR=/home/teamharness/.teamharness \
    TEAMHARNESS_SYNC_STRATEGY=manual

ENTRYPOINT ["client-entrypoint.sh"]
# 默认启动守护进程
CMD ["daemon"]
