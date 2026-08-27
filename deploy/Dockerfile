# TeamHarness 服务端镜像（SubTask 3.2）
# 三服务（asset / recall / distill）共用同一镜像，启动时通过 --role 切换。

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
# - libgit2 与 pygit2 编译需要 libgit2-dev / cmake / gcc
# - sqlite3 命令行供备份脚本兜底
# - curl 供 healthcheck
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
        cmake \
        pkg-config \
        libgit2-dev \
        libssl-dev \
        libffi-dev \
        sqlite3 \
        curl \
        ca-certificates \
        tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# 先复制依赖清单，利用 Docker 层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -i https://pypi.tuna.tsinghua.edu.cn/simple \
    --trusted-host pypi.tuna.tsinghua.edu.cn \
    -r requirements.txt

# 复制应用代码
COPY server/ ./server/
COPY deploy/ ./deploy/

# 创建非 root 用户运行
RUN useradd --create-home --shell /bin/bash teamharness \
    && mkdir -p /var/log/teamharness /var/lib/teamharness \
    && chown -R teamharness:teamharness /app /var/log/teamharness /var/lib/teamharness

USER teamharness

# 默认启动 asset-service，docker-compose 通过 command 覆盖
EXPOSE 8080 8081 8082

# 健康检查（默认探 8080；docker-compose 已为每个服务覆写）
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s --retries=5 \
    CMD curl -fsS http://localhost:8080/v1/system/selfcheck || exit 1

ENTRYPOINT ["python", "-m", "server.app"]
CMD ["--role", "asset", "--host", "0.0.0.0", "--port", "8080"]
