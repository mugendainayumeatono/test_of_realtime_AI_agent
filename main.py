#!/usr/bin/env python3
"""
main.py - 根目录统一入口，代理至 src/main.py
"""
import os
import sys
from pathlib import Path

# 将 src/ 加入 sys.path
src_dir = Path(__file__).resolve().parent / "src"
sys.path.insert(0, str(src_dir))

from main import main

if __name__ == "__main__":
    main()
