#!/usr/bin/env python3
"""【已废弃】原先用于给 MediaCrawler 源码打补丁。

现在 MediaCrawler/ 目录保持只读，相关能力由 scripts/mediacrawler_launcher.py
在运行时包装实现（条数上限、跳过已下载视频）。保留本文件仅为防止旧命令静默执行。
"""
import sys

if __name__ == "__main__":
    print("patch_mediacrawler.py 已废弃：请直接使用 scripts/mediacrawler_launcher.py，不再修改 MediaCrawler 源码。")
    sys.exit(1)
