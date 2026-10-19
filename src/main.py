#!/usr/bin/env python3
"""
src/main.py

系统统一启动与多进程管理入口 (Master Launcher)。
架构说明：
启动后，拉起 Master 进程，由 Master 进程通过 Python multiprocessing 启动并管理四大核心子进程：
- 飞行模拟器 (airplane_simulator)
- Web 界面与 API 网关 (webview)
- 进近空管智能体 (agent_controller)
- 机载飞行员智能体 (agent_pilot)

模式支持：
1. 守护进程模式 (start / 默认)：拉起 Master 后转为后台守护进程，启动进程退出。
2. 前台运行模式 (--foreground / -f / run)：Master 在前台保持运行，适合 Docker 容器作为 PID 1 运行。
3. 服务状态与停止 (status / stop / restart)。
"""

import argparse
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from master import (
    MasterProcess,
    daemonize_master,
    stop_master,
    status_master,
    get_master_pid,
)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Realtime Aviation Multi-Agent System - Master Launcher",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "action",
        nargs="?",
        default="start",
        choices=["start", "stop", "restart", "status", "run"],
        help="Action to perform (default: start)",
    )
    parser.add_argument(
        "-f", "--foreground",
        action="store_true",
        help="Run master process in foreground (do not daemonize, ideal for Docker containers)",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("PLATFORM_HOST", "0.0.0.0"),
        help="Host address for webview service (default: 0.0.0.0)",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PLATFORM_PORT", 8000)),
        help="Port for webview service (default: 8000)",
    )

    args = parser.parse_args(argv)


    action = args.action.lower()

    if action == "stop":
        stop_master()
    elif action == "status":
        status_master()
    elif action == "restart":
        print("Restarting Master process...", flush=True)
        stop_master()
        if args.foreground:
            master = MasterProcess(host=args.host, port=args.port)
            master.run()
        else:
            daemonize_master(host=args.host, port=args.port)
    elif action == "run" or args.foreground:
        # 前台运行模式（供 Docker 等容器环境）
        print(f"[Master Launcher] Running Master process in foreground on {args.host}:{args.port}...", flush=True)
        master = MasterProcess(host=args.host, port=args.port)
        master.run()
    elif action == "start":
        # 守护进程模式：启动 master 进程后，启动进程退出
        daemonize_master(host=args.host, port=args.port)


if __name__ == "__main__":
    main()
