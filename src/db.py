from __future__ import annotations

import sqlite3
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from .config import Config


SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# 单条视频自动重试上限（对应 CLAUDE.md 的失败处理策略）
MAX_RETRY = 3


@dataclass
class TickResult:
    """一次 worker tick 的结果：ok=成功处理，skipped=跳过（前置条件不满足），failed=失败"""
    ok: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def attempted(self) -> int:
        return self.ok + self.failed


def init_db(cfg: Config) -> None:
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    schema = SCHEMA_PATH.read_text(encoding="utf-8")
    with sqlite3.connect(cfg.db_path) as conn:
        conn.executescript(schema)
        conn.commit()


@contextmanager
def connect(cfg: Config) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(cfg.db_path, timeout=30.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        conn.execute("BEGIN")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


def now_ts() -> int:
    return int(time.time())


def add_creator(cfg: Config, *, sec_user_id: str, homepage_url: str,
                nickname: str | None = None, category: str | None = None,
                note: str | None = None) -> None:
    with connect(cfg) as conn:
        conn.execute(
            """INSERT INTO creators (sec_user_id, nickname, homepage_url, first_seen_at, category, note)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(sec_user_id) DO UPDATE SET
                 homepage_url=excluded.homepage_url,
                 nickname=COALESCE(excluded.nickname, creators.nickname),
                 category=COALESCE(excluded.category, creators.category),
                 note=COALESCE(excluded.note, creators.note)""",
            (sec_user_id, nickname, homepage_url, now_ts(), category, note),
        )


def update_creator(cfg: Config, sec_user_id: str, **fields) -> None:
    """更新博主字段：nickname/category/note/avatar_url/intro/enabled 等"""
    allowed = {"nickname", "category", "note", "avatar_url", "intro",
               "enabled", "last_crawled_at", "crawl_interval_hours"}
    updates = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if not updates:
        return
    set_clause = ", ".join(f"{k}=?" for k in updates)
    params = list(updates.values()) + [sec_user_id]
    with connect(cfg) as conn:
        conn.execute(
            f"UPDATE creators SET {set_clause} WHERE sec_user_id=?", params
        )


def get_creator(cfg: Config, sec_user_id: str) -> sqlite3.Row | None:
    with connect(cfg) as conn:
        return conn.execute(
            "SELECT * FROM creators WHERE sec_user_id=?", (sec_user_id,)
        ).fetchone()


def list_creators(cfg: Config, only_enabled: bool = True) -> list[sqlite3.Row]:
    with connect(cfg) as conn:
        sql = "SELECT * FROM creators"
        if only_enabled:
            sql += " WHERE enabled=1"
        return conn.execute(sql + " ORDER BY first_seen_at").fetchall()


def upsert_video(cfg: Config, record: dict) -> bool:
    """返回 True 表示新增，False 表示已存在"""
    ts = now_ts()
    aweme_id = record["aweme_id"]
    with connect(cfg) as conn:
        existing = conn.execute(
            "SELECT 1 FROM videos WHERE aweme_id=?", (aweme_id,)
        ).fetchone()
        conn.execute(
            """INSERT INTO videos (
                aweme_id, sec_user_id, title, desc_text, create_time,
                aweme_url, cover_url, video_download_url,
                liked_count, collected_count, comment_count, share_count,
                fetched_at, updated_at
              ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
              ON CONFLICT(aweme_id) DO UPDATE SET
                title=excluded.title,
                desc_text=excluded.desc_text,
                liked_count=excluded.liked_count,
                collected_count=excluded.collected_count,
                comment_count=excluded.comment_count,
                share_count=excluded.share_count,
                cover_url=excluded.cover_url,
                video_download_url=excluded.video_download_url,
                updated_at=excluded.updated_at""",
            (
                aweme_id, record["sec_user_id"],
                record["title"], record.get("desc", record.get("title")),
                int(record["create_time"]),
                record.get("aweme_url"), record.get("cover_url"),
                record.get("video_download_url"),
                int(record.get("liked_count") or 0),
                int(record.get("collected_count") or 0),
                int(record.get("comment_count") or 0),
                int(record.get("share_count") or 0),
                ts, ts,
            ),
        )
        return existing is None


def requeue_failed(cfg: Config) -> dict:
    """把可重试的失败记录放回待处理状态；重试次数耗尽的转 manual_failed。
    返回各类变更的行数，便于日志输出。"""
    with connect(cfg) as conn:
        exhausted = conn.execute(
            """UPDATE videos SET status='manual_failed'
               WHERE status IN ('transcribe_failed', 'summarize_failed') AND retry_count>=?""",
            (MAX_RETRY,),
        ).rowcount
        asr = conn.execute(
            """UPDATE videos SET status='downloaded'
               WHERE status='transcribe_failed' AND retry_count<?""",
            (MAX_RETRY,),
        ).rowcount
        llm = conn.execute(
            """UPDATE videos SET status='transcribed'
               WHERE status='summarize_failed' AND retry_count<?""",
            (MAX_RETRY,),
        ).rowcount
    return {"manual_failed": exhausted, "requeued_asr": asr, "requeued_llm": llm}


def recover_stale(cfg: Config) -> dict:
    """进程被杀后遗留的中间态（transcribing/summarizing）回退到上一个稳定状态。
    注意：只能在没有任何 worker 在运行时调用，否则会误回退正在处理的视频。"""
    with connect(cfg) as conn:
        t = conn.execute(
            "UPDATE videos SET status='downloaded' WHERE status='transcribing'"
        ).rowcount
        s = conn.execute(
            "UPDATE videos SET status='transcribed' WHERE status='summarizing'"
        ).rowcount
    return {"transcribing_reset": t, "summarizing_reset": s}
