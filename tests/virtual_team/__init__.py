# tests/virtual_team/ — 虚拟团队测试
#
# 本包的测试不依赖 Docker 运行环境，而是在单进程内用 mock 服务端 + 多线程模拟多成员并发，
# 验证虚拟团队编排逻辑的正确性（CI 回归用）。
#
# 完整的 Docker 化虚拟团队测试由 deploy/virtual-team/ 下的 shell 脚本 + docker-compose 驱动。
