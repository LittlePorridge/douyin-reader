"""测试公共夹具：在临时目录里构造独立的 Config 与 SQLite，不触碰真实 data/。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.config import Config  # noqa: E402
from src.db import connect, init_db  # noqa: E402

SEC = "MS4wLjABAAAAtestcreator000000000000000000000000000000000000000000"


def make_cfg(root: Path) -> Config:
    data = root / "data"
    mc = root / "MediaCrawler"
    cfg = Config(
        project_root=root,
        data_dir=data,
        mediacrawler_dir=mc,
        db_path=data / "test.db",
        llm_providers_path=data / "llm_providers.yaml",
        audio_dir=data / "audio",
        text_dir=data / "text",
        summary_dir=data / "summaries",
        jsonl_dir=mc / "data" / "douyin" / "jsonl",
        videos_dir=mc / "data" / "douyin" / "videos",
    )
    cfg.ensure_dirs()
    init_db(cfg)
    return cfg


@pytest.fixture
def cfg(tmp_path: Path) -> Config:
    c = make_cfg(tmp_path)
    with connect(c) as conn:
        conn.execute(
            "INSERT INTO creators (sec_user_id, homepage_url, first_seen_at) VALUES (?, ?, 0)",
            (SEC, "https://www.douyin.com/user/" + SEC),
        )
    return c


def insert_video(cfg: Config, aweme_id: str, status: str = "new", retry_count: int = 0,
                 create_time: int = 1_700_000_000) -> None:
    with connect(cfg) as conn:
        conn.execute(
            """INSERT INTO videos (aweme_id, sec_user_id, title, create_time, status, retry_count,
                                   fetched_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 0, 0)""",
            (aweme_id, SEC, f"title {aweme_id}", create_time, status, retry_count),
        )


def get_status(cfg: Config, aweme_id: str) -> str:
    with connect(cfg) as conn:
        return conn.execute("SELECT status FROM videos WHERE aweme_id=?", (aweme_id,)).fetchone()["status"]
