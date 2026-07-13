"""在 MediaCrawler 环境里跑，获取博主信息并输出 JSON。
用法: uv run python fetch_creator_info.py <sec_user_id>
输出: {"nickname": "...", "avatar_url": "...", "intro": "...", "follower_count": 123}
"""
import asyncio
import json
import os
import sys

sys.path.insert(0, ".")


async def main():
    if len(sys.argv) < 2:
        print(json.dumps({"error": "missing sec_user_id"}))
        sys.exit(1)
    sec_user_id = sys.argv[1]

    from media_platform.douyin.client import DouYinClient
    from playwright.async_api import async_playwright
    from tools import utils

    async with async_playwright() as p:
        user_data = os.path.abspath("browser_data/dy_user_data_dir")
        browser = await p.chromium.launch_persistent_context(
            user_data_dir=user_data, headless=True,
            viewport={"width": 1920, "height": 1080},
        )
        page = await browser.new_page()
        await page.goto("https://www.douyin.com")
        cookie_str, cookie_dict = await utils.convert_browser_context_cookies(
            browser, urls=["https://www.douyin.com"]
        )
        client = DouYinClient(
            headers={
                "User-Agent": await page.evaluate("() => navigator.userAgent"),
                "Cookie": cookie_str,
                "Host": "www.douyin.com",
                "Origin": "https://www.douyin.com/",
                "Referer": "https://www.douyin.com/",
                "Content-Type": "application/json;charset=UTF-8",
            },
            playwright_page=page, cookie_dict=cookie_dict,
        )
        try:
            info = await client.get_user_info(sec_user_id)
            user = info.get("user", {})
            result = {
                "nickname": user.get("nickname", ""),
                "avatar_url": (user.get("avatar_168x168") or user.get("avatar_thumb") or {}).get("url_list", [""])[0],
                "intro": user.get("signature", ""),
                "follower_count": user.get("follower_count", 0),
                "following_count": user.get("following_count", 0),
                "aweme_count": user.get("aweme_count", 0),
                "sec_user_id": sec_user_id,
            }
            print(json.dumps(result, ensure_ascii=False))
        except Exception as e:
            print(json.dumps({"error": str(e), "sec_user_id": sec_user_id}, ensure_ascii=False))
        finally:
            await browser.close()


asyncio.run(main())