from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import connect, upsert_video, now_ts
from sqlite3 import Row as sqlite3_Row_like


@dataclass
class ImportResult:
    new_count: int = 0
    existing_count: int = 0
    failed_count: int = 0


def _parse_jsonl(jsonl_path: Path) -> list[dict]:
    records: list[dict] = []
    with jsonl_path.open("r", encoding="utf-8") as f:
        for line_no, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as e:
                print(f"[importer] skip bad line {line_no}: {e}")
    return records


def _find_latest_jsonl(cfg: Config, sec_user_id: str) -> Path | None:
    pattern = "creator_contents_*.jsonl"
    files = sorted(cfg.jsonl_dir.glob(pattern), key=lambda p: p.stat().st_mtime)
    if not files:
        return None
    return files[-1]


def import_latest(cfg: Config, sec_user_id: str, max_new: int = 0) -> ImportResult:
    jsonl_path = _find_latest_jsonl(cfg, sec_user_id)
    if jsonl_path is None:
        print(f"[importer] no jsonl found under {cfg.jsonl_dir}")
        return ImportResult()

    print(f"[importer] reading {jsonl_path.name}")
    records = _parse_jsonl(jsonl_path)
    print(f"[importer] parsed {len(records)} records")

    result = ImportResult()
    newAdded = 0
    for rec in records:
        aweme_id = rec.get("aweme_id")
        if not aweme_id:
            result.failed_count += 1
            continue
        rec["sec_user_id"] = sec_user_id
        rec.setdefault("aweme_url", f"https://www.douyin.com/video/{aweme_id}")
        is_new = upsert_video(cfg, rec)
        if is_new:
            result.new_count += 1
            newAdded += 1
            if max_new > 0 and newAdded >= max_new:
                print(f"[importer] reached max_new={max_new}, stopping early")
                break
        else:
            result.existing_count += 1

    print(f"[importer] done: new={result.new_count} existing={result.existing_count} failed={result.failed_count}")
    return result


def ensure_video_paths(cfg: Config, aweme_id: str) -> None:
    paths = cfg.paths_for(aweme_id)
    paths["video"].parent.mkdir(parents=True, exist_ok=True)


def mark_video_downloaded(cfg: Config, aweme_id: str, video_path: Path) -> None:
    with connect(cfg) as conn:
        conn.execute(
            """UPDATE videos SET status='downloaded', video_path=?, downloaded_at=?
               WHERE aweme_id=? AND status='new'""",
            (str(video_path.relative_to(cfg.data_dir)), now_ts(), aweme_id),
        )


def list_pending_download(cfg: Config, limit: int = 50) -> list[sqlite3_Row_like]:
    """status='new' 且 mp4 文件已落地的视频才转 downloaded"""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE status='new' ORDER BY create_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows


if __name__ == "__main__":
    from .config import load_config
    cfg = load_config()
    sec = "MS4wLjABAAAAxIFxCc7GbORVk16hLf77GZxEW4l7KseaCFKEPEcNOwXpPPtKt4kO2mMUjF44jOBf"
    r = import_latest(cfg, sec)
    print(r)