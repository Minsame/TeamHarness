"""All-in-One 单二进制启动入口（PyInstaller spec 入口）。

对应 SubTask 3.1：5 人团队下载单二进制后无外部依赖即可启动。
本文件被 deploy/all_in_one.spec 引用为 PyInstaller 入口，
启动内嵌运行时与 FastAPI 应用。

命令行参数：
    teamharness-all-in-one [--host HOST] [--port PORT] [--data-dir DIR]
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="teamharness-all-in-one",
        description="TeamHarness All-in-One 单二进制（内嵌 SQLite + sqlite-vec + libgit2）",
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8080, help="监听端口（默认 8080）")
    parser.add_argument(
        "--data-dir",
        default=None,
        help="数据目录（默认 ~/.teamharness/data）",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
        help="日志级别",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )

    # 标记 All-in-One 模式（DeployConfig 探测依据）
    os.environ["TEAMHARNESS_ALL_IN_ONE"] = "true"

    # 启动内嵌运行时
    from server.deploy.all_in_one import run_all_in_one

    runtime = run_all_in_one(data_path=args.data_dir, host=args.host, port=args.port)

    # 启动 FastAPI 应用（uvicorn）
    try:
        import uvicorn

        # server.app 由 Agent 4-9 完成后存在；缺失时仅启动 system info 端点
        try:
            from server.app import build_app  # type: ignore[import-not-found]

            app = build_app()
        except ImportError:
            # 兜底：仅暴露 system info 的最小 FastAPI 应用
            from fastapi import FastAPI

            from server.deploy.api_versioning import build_system_info_router

            app = FastAPI(title="TeamHarness All-in-One (minimal)")
            app.include_router(build_system_info_router())

        uvicorn.run(app, host=args.host, port=args.port, log_level=args.log_level.lower())
    except KeyboardInterrupt:
        pass
    finally:
        runtime.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
