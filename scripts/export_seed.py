#!/usr/bin/env python3
"""导出数据库为脱敏的 SQL 种子文件，可安全推到 GitHub。
- creators: 保留 sec_user_id/nickname/avatar_url/intro/category/note，去掉 homepage_url 中的跟踪参数
- videos: 保留全部元数据和摘要，去掉 video_download_url（含临时签名）
- llm_summaries: 保留全部
- crawl_runs: 不导出（运维数据）
"""
import sqlite3
import sys
from pathlib import Path

DB = Path(__file__).resolve().parent.parent / "data" / "douyin-reader.db"
OUT = Path(__file__).resolve().parent.parent / "seed" / "seed.sql"


def main():
    if not DB.exists():
        print(f"database not found: {DB}")
        sys.exit(1)

    conn = sqlite3.connect(str(DB))
    conn.row_factory = sqlite3.Row
    lines = []

    # Header
    lines.append("-- douyin-reader 种子数据")
    lines.append("-- 由 scripts/export_seed.py 自动生成，可安全推到 GitHub")
    lines.append("-- 导入方法: sqlite3 data/douyin-reader.db < data/seed.sql")
    lines.append("")

    # creators
    lines.append("-- creators")
    for r in conn.execute("SELECT * FROM creators").fetchall():
        # 清理 homepage_url 的跟踪参数
        url = r["homepage_url"].split("?")[0]
        cols = ["sec_user_id", "nickname", "homepage_url", "avatar_url", "intro",
                "category", "note", "first_seen_at", "last_crawled_at",
                "crawl_interval_hours", "enabled"]
        vals = []
        for c in cols:
            v = r[c]
            if c == "homepage_url":
                v = url
            if v is None:
                vals.append("NULL")
            elif isinstance(v, int):
                vals.append(str(v))
            else:
                escaped = str(v).replace("'", "''")
                vals.append(f"'{escaped}'")
        lines.append(f"INSERT OR IGNORE INTO creators ({', '.join(cols)}) VALUES ({', '.join(vals)});")

    # videos
    lines.append("\n-- videos")
    for r in conn.execute("SELECT * FROM videos").fetchall():
        cols = ["aweme_id", "sec_user_id", "title", "desc_text", "create_time",
                "aweme_url", "cover_url", "liked_count", "collected_count",
                "comment_count", "share_count", "status", "retry_count",
                "transcript_path", "summary_path", "summary", "key_points",
                "knowledge_points", "llm_provider", "llm_model",
                "downloaded_at", "transcribed_at", "summarized_at",
                "video_duration", "transcribe_duration",
                "fetched_at", "updated_at"]
        vals = []
        for c in cols:
            v = r[c]
            if v is None:
                vals.append("NULL")
            elif isinstance(v, int):
                vals.append(str(v))
            else:
                escaped = str(v).replace("'", "''")
                vals.append(f"'{escaped}'")
        lines.append(f"INSERT OR IGNORE INTO videos ({', '.join(cols)}) VALUES ({', '.join(vals)});")

    # llm_summaries
    lines.append("\n-- llm_summaries")
    for r in conn.execute("SELECT * FROM llm_summaries").fetchall():
        cols = ["aweme_id", "provider", "model", "summary", "article",
                "key_points", "knowledge_points", "created_at", "is_primary",
                "summarize_duration"]
        vals = []
        for c in cols:
            v = r[c]
            if v is None:
                vals.append("NULL")
            elif isinstance(v, int):
                vals.append(str(v))
            else:
                escaped = str(v).replace("'", "''")
                vals.append(f"'{escaped}'")
        lines.append(f"INSERT OR IGNORE INTO llm_summaries ({', '.join(cols)}) VALUES ({', '.join(vals)});")

    conn.close()

    OUT.write_text("\n".join(lines), encoding="utf-8")
    print(f"exported {len(lines)} lines to {OUT}")
    print(f"  creators: {sum(1 for l in lines if l.startswith('INSERT') and 'creators' in l)} rows")
    print(f"  videos: {sum(1 for l in lines if l.startswith('INSERT') and 'videos' in l)} rows")
    print(f"  llm_summaries: {sum(1 for l in lines if l.startswith('INSERT') and 'llm_summaries' in l)} rows")


if __name__ == "__main__":
    main()