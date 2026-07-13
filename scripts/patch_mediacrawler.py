#!/usr/bin/env python3
"""给 MediaCrawler 打补丁：
1. client.py: 给 get_all_user_aweme_posts 加 max_posts 参数
2. core.py: 从环境变量 DY_MAX_POSTS 读取限制条数
"""
import re
from pathlib import Path

MC = Path(__file__).resolve().parent.parent / "MediaCrawler"


def patch_client():
    path = MC / "media_platform" / "douyin" / "client.py"
    if not path.exists():
        print(f"  skip: {path} not found")
        return
    content = path.read_text(encoding="utf-8")
    if "max_posts" in content:
        print("  client.py already patched, skip")
        return

    # 替换方法签名
    content = content.replace(
        "async def get_all_user_aweme_posts(self, sec_user_id: str, callback: Optional[Callable] = None):",
        "async def get_all_user_aweme_posts(self, sec_user_id: str, callback: Optional[Callable] = None, max_posts: int = 0):",
    )
    # 在 result.extend(aweme_list) 后加 max_posts 检查
    content = content.replace(
        "result.extend(aweme_list)\n        return result",
        """            result.extend(aweme_list)
            if max_posts > 0 and len(result) >= max_posts:
                utils.logger.info(f"[DouYinClient.get_all_user_aweme_posts] reached max_posts limit ({max_posts}), stopping")
                break
        return result""",
    )
    path.write_text(content, encoding="utf-8")
    print("  client.py patched")


def patch_core():
    path = MC / "media_platform" / "douyin" / "core.py"
    if not path.exists():
        print(f"  skip: {path} not found")
        return
    content = path.read_text(encoding="utf-8")
    if "DY_MAX_POSTS" in content:
        print("  core.py already patched, skip")
        return

    # 在调用 get_all_user_aweme_posts 前加 env var 读取
    old = "all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail"
    new = """import os as _os
            _max_posts = int(_os.environ.get("DY_MAX_POSTS", "0"))
            all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail, max_posts=_max_posts"""
    # 使用更精确的匹配（前面有大括号标记的 max_posts=5 那段）
    # 先看一下实际内容
    if "max_posts=5" in content:
        content = content.replace(
            'all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail, max_posts=5)',
            'import os as _os\n            _max_posts = int(_os.environ.get("DY_MAX_POSTS", "0"))\n            all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail, max_posts=_max_posts)',
        )
    elif "max_posts" not in content:
        content = content.replace(
            "all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail)",
            'import os as _os\n            _max_posts = int(_os.environ.get("DY_MAX_POSTS", "0"))\n            all_video_list = await self.dy_client.get_all_user_aweme_posts(sec_user_id=user_id, callback=self.fetch_creator_video_detail, max_posts=_max_posts)',
        )
    else:
        print("  core.py: unexpected format, manual fix needed")
        return
    path.write_text(content, encoding="utf-8")
    print("  core.py patched")


if __name__ == "__main__":
    print("[patch] MediaCrawler patches:")
    patch_client()
    patch_core()
    print("[patch] done")