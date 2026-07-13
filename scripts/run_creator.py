#!/usr/bin/env python3
"""一键启动：传入博主主页 URL，自动跑完整流程
用法:
  python scripts/run_creator.py "https://www.douyin.com/user/MS4w..."
  python scripts/run_creator.py "https://www.douyin.com/user/MS4w..." --nickname "清华学霸"
  python scripts/run_creator.py --all          # 跑所有已添加的博主
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help"):
        print(__doc__)
        return

    if args[0] == "--all":
        cmd = [sys.executable, "-m", "src.orchestrator", "run-all"] + args[1:]
    else:
        url = args[0]
        rest = args[1:]
        cmd = [sys.executable, "-m", "src.orchestrator", "run", "--url", url] + rest

    subprocess.run(cmd, cwd=str(ROOT))


if __name__ == "__main__":
    main()