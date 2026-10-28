"""通过运行时包装启动 MediaCrawler 的抖音博主抓取。

用法（由 orchestrator 调用，cwd 为 MediaCrawler/）：
    uv run python <project>/scripts/mediacrawler_launcher.py \
        --sec-user-id <id> [--login-type cookie] [--max-videos N]

设计目的：
- 不改写 MediaCrawler/config/dy_config.py（博主列表通过 --creator_id 传入）
- 不对 MediaCrawler 源码打补丁（条数上限、跳过已下载视频在进程内包装实现）
- MediaCrawler 目录保持只读
"""
from __future__ import annotations

import argparse
import os
import runpy
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parent.parent
MC_DIR = PROJECT / "MediaCrawler"


def _parse() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--sec-user-id", required=True)
    p.add_argument("--login-type", default="cookie", choices=["cookie", "qrcode"])
    p.add_argument("--max-videos", type=int, default=0, help="0 = 不限制")
    return p.parse_args()


def main() -> None:
    args = _parse()
    if not MC_DIR.exists():
        print(f"MediaCrawler 目录不存在: {MC_DIR}，请先运行 scripts/setup.sh")
        sys.exit(2)

    sys.path.insert(0, str(PROJECT))
    sys.path.insert(0, str(MC_DIR))
    os.chdir(MC_DIR)

    from src.mc_runtime import PostsLimiter, should_skip_existing
    from media_platform.douyin import client as dy_client
    from store.douyin import douyin_store_media as dy_store

    # 1) 条数上限：包装分页接口
    limiter = PostsLimiter(args.max_videos)
    orig_page = dy_client.DouYinClient.get_user_aweme_posts

    async def limited_page(self, sec_user_id, max_cursor=""):
        page = await orig_page(self, sec_user_id, max_cursor)
        return limiter.apply(page)

    dy_client.DouYinClient.get_user_aweme_posts = limited_page

    # 2) 跳过已下载视频：包装落盘逻辑（与原补丁行为一致，网络下载仍会发生）
    orig_save = dy_store.DouYinVideo.save_video

    async def save_skip_existing(self, aweme_id, video_content, extension_file_name):
        path = self.make_save_file_name(aweme_id, extension_file_name)
        if should_skip_existing(path):
            print(f"[launcher] SKIP_EXISTING {path}")
            return
        return await orig_save(self, aweme_id, video_content, extension_file_name)

    dy_store.DouYinVideo.save_video = save_skip_existing

    # 3) 以 MediaCrawler 自身的 CLI 入口运行，博主通过 --creator_id 指定
    creator_url = f"https://www.douyin.com/user/{args.sec_user_id}"
    sys.argv = ["main.py", "--platform", "dy", "--lt", args.login_type,
                "--type", "creator", "--creator_id", creator_url]
    try:
        runpy.run_path(str(MC_DIR / "main.py"), run_name="__main__")
    except SystemExit as e:
        sys.exit(e.code)


if __name__ == "__main__":
    main()
