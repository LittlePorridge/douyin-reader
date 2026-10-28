from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .db import connect, upsert_video
from sqlite3 import Row as sqlite3_Row_like


JSONL_GLOB = "creator_contents_*.jsonl"


@dataclass
class ImportResult:
    new_count: int = 0
    existing_count: int = 0
    failed_count: int = 0


def jsonl_offsets(cfg: Config) -> dict[str, int]:
    """抓取前记录每个 jsonl 的当前大小。抓取后据此只导入本次新追加的行。
    MediaCrawler 按日期命名、同一天多次抓取会追加到同一文件，
    所以不能只取“最新文件”，否则会把其他博主的记录导入到当前博主名下。"""
    if not cfg.jsonl_dir.exists():
        return {}
    return {str(p): p.stat().st_size for p in cfg.jsonl_dir.glob(JSONL_GLOB)}


def _read_appended_lines(path: Path, start: int) -> list[str]:
    size = path.stat().st_size
    # 文件被截断/轮转（size < start）时从头读，避免漏数据
    if size < start:
        start = 0
    if size == start:
        return []
    with path.open("rb") as f:
        f.seek(start)
        data = f.read(size - start)
    return data.decode("utf-8", errors="replace").splitlines()


def _parse_lines(lines: list[str], source: str) -> list[dict]:
    records: list[dict] = []
    for line_no, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError as e:
            print(f"[importer] skip bad line {source}:{line_no}: {e}")
    return records


def import_since(cfg: Config, sec_user_id: str, start_offsets: dict[str, int],
                 max_new: int = 0) -> ImportResult:
    """导入自 start_offsets 以来新追加的 jsonl 记录，归属到 sec_user_id。
    - max_new > 0 时最多新增 max_new 条 new 视频
    - 已存在的 aweme_id 只更新统计字段，不改 status
    """
    result = ImportResult()
    if not cfg.jsonl_dir.exists():
        print(f"[importer] jsonl dir missing: {cfg.jsonl_dir}")
        return result

    records: list[dict] = []
    for path in sorted(cfg.jsonl_dir.glob(JSONL_GLOB)):
        lines = _read_appended_lines(path, start_offsets.get(str(path), 0))
        if lines:
            print(f"[importer] {path.name}: {len(lines)} new lines")
        records.extend(_parse_lines(lines, path.name))
    print(f"[importer] parsed {len(records)} new records for {sec_user_id[:12]}...")

    new_added = 0
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
            new_added += 1
            if max_new > 0 and new_added >= max_new:
                print(f"[importer] reached max_new={max_new}, stopping early")
                break
        else:
            result.existing_count += 1

    print(f"[importer] done: new={result.new_count} existing={result.existing_count} failed={result.failed_count}")
    return result


def ensure_video_paths(cfg: Config, aweme_id: str) -> None:
    paths = cfg.paths_for(aweme_id)
    paths["video"].parent.mkdir(parents=True, exist_ok=True)


def list_pending_download(cfg: Config, limit: int = 50) -> list[sqlite3_Row_like]:
    """status='new' 的视频（mp4 是否落地由 transcribe/promote 阶段确认）"""
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT * FROM videos WHERE status='new' ORDER BY create_time DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return rows
