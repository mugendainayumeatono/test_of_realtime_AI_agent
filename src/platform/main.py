"""
Platform 进程主入口 (main.py)

该进程负责数字孪生空域仿真、物理性能推算与系统协同。
原有 Webview 服务作为 Platform 进程中的一个独立线程 (WebviewThread) 运行。
"""

import os
import sys
import time
import signal
import threading
import logging
from pathlib import Path

# 设置日志
log_dir = Path("../../logs")
log_dir.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(log_dir / "platform.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger("platform")

# 引入子线程中的 webview 运行逻辑与 airplane_simulator 仿真实例
from webview import run_webview, stop_webview, app
from airplane_simulator import simulator as airplane_sim

HOST = os.environ.get("PLATFORM_HOST", "0.0.0.0")
PORT = int(os.environ.get("PLATFORM_PORT", 8000))

_shutdown_event = threading.Event()


def signal_handler(signum, frame):
    signame = signal.Signals(signum).name
    logger.info(f"Received signal {signame} ({signum}), initiating graceful shutdown for Platform process...")
    airplane_sim.stop()
    stop_webview()
    _shutdown_event.set()


def main():
    logger.info(f"=== Starting Platform Process (PID: {os.getpid()}) ===")
    
    # 注册系统停机信号
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # 创建并启动 Webview 子线程
    logger.info(f"Initializing Webview service as a thread within Platform process on {HOST}:{PORT}...")
    webview_thread = threading.Thread(
        target=run_webview,
        kwargs={"host": HOST, "port": PORT},
        name="WebviewThread",
        daemon=True
    )
    webview_thread.start()
    logger.info("Webview thread successfully launched and running.")

    # 主线程负责进程生命周期管理与健康监控
    try:
        while not _shutdown_event.is_set():
            if not webview_thread.is_alive():
                logger.error("Webview thread unexpectedly terminated! Exiting Platform process.")
                break
            time.sleep(1.0)
    except KeyboardInterrupt:
        logger.info("KeyboardInterrupt caught in main thread.")
        airplane_sim.stop()
        stop_webview()
    finally:
        airplane_sim.stop()
        logger.info("Waiting for Webview thread to finish...")
        webview_thread.join(timeout=3.0)
        logger.info("=== Platform Process Shutdown Completed ===")


if __name__ == "__main__":
    main()
