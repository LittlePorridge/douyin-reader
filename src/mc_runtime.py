"""MediaCrawler 运行时的纯逻辑（不依赖 MediaCrawler，便于单元测试）。

scripts/mediacrawler_launcher.py 用这些函数在运行时包装 MediaCrawler 的方法，
从而不需要修改 MediaCrawler/ 目录下的任何源码或配置文件。
"""
from __future__ import annotations

import os


class PostsLimiter:
    """限制一次抓取的视频条数（替代原先对 MediaCrawler 源码打的 DY_MAX_POSTS 补丁）。

    作用于分页接口的返回：累计条数达到上限后，截断当前页并把 has_more 置 0，
    MediaCrawler 的分页循环会自然结束。max_posts<=0 表示不限制。
    """

    def __init__(self, max_posts: int):
        self.max_posts = max_posts
        self.count = 0

    def apply(self, page: dict) -> dict:
        if self.max_posts <= 0:
            return page
        if self.count >= self.max_posts:
            return {**page, "aweme_list": [], "has_more": 0}
        items = page.get("aweme_list") or []
        remain = self.max_posts - self.count
        if len(items) >= remain:
            self.count = self.max_posts
            return {**page, "aweme_list": items[:remain], "has_more": 0}
        self.count += len(items)
        return page


def should_skip_existing(path: str, min_size: int = 1000) -> bool:
    """已下载过的视频文件判定（与原补丁一致：存在且大于 1KB）。"""
    return os.path.exists(path) and os.path.getsize(path) > min_size
