#!/usr/bin/env python3
"""
src/platform/main.py

向后兼容入口：原 Platform 进程中的线程已重构为独立子进程（Webview 进程与 Airplane Simulator 进程）。
由 Master 进程统一拉起与管理。执行本脚本将自动启动系统 Master 进程。
"""
import sys
from pathlib import Path

# 将 src/ 加入 sys.path
src_dir = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(src_dir))

from main import main

if __name__ == "__main__":
    main()
