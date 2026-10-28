import json

from src.importer import jsonl_offsets, import_since
from tests.conftest import SEC, get_status
from src.db import connect


def _rec(aweme_id: str) -> dict:
    return {"aweme_id": aweme_id, "title": "t", "create_time": 1_700_000_000,
            "desc": "d", "liked_count": "1"}


def _append(path, recs):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        for r in recs:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def test_only_lines_appended_after_snapshot_are_imported(cfg):
    f = cfg.jsonl_dir / "creator_contents_2026-01-01.jsonl"
    _append(f, [_rec("100")])          # 其他博主的旧记录
    offsets = jsonl_offsets(cfg)
    _append(f, [_rec("200"), _rec("201")])  # 本次抓取新增
    r = import_since(cfg, SEC, offsets)
    assert r.new_count == 2
    with connect(cfg) as conn:
        rows = conn.execute("SELECT aweme_id, sec_user_id, status FROM videos ORDER BY aweme_id").fetchall()
    assert [row["aweme_id"] for row in rows] == ["200", "201"]
    assert all(row["sec_user_id"] == SEC for row in rows)
    assert get_status(cfg, "200") == "new"


def test_existing_video_status_not_reset(cfg):
    f = cfg.jsonl_dir / "creator_contents_2026-01-02.jsonl"
    _append(f, [_rec("300")])
    offsets = jsonl_offsets(cfg)
    _append(f, [_rec("300")])
    import_since(cfg, SEC, offsets)
    with connect(cfg) as conn:
        n = conn.execute("SELECT count(*) AS n FROM videos WHERE aweme_id='300'").fetchone()["n"]
    assert n == 1


def test_no_new_lines_imports_nothing(cfg):
    f = cfg.jsonl_dir / "creator_contents_2026-01-03.jsonl"
    _append(f, [_rec("400")])
    r = import_since(cfg, SEC, jsonl_offsets(cfg))
    assert r.new_count == 0
