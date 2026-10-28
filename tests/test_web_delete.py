"""删除 mp4/wav 只能作用于 done 视频，未完成视频的文件必须保留。"""
import importlib
import sys

import pytest

from tests.conftest import insert_video, ROOT, SEC


@pytest.fixture
def app_mod(cfg, monkeypatch):
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    mod = importlib.import_module("web.app")
    monkeypatch.setattr(mod, "cfg", cfg)

    def _connect():
        import sqlite3
        c = sqlite3.connect(cfg.db_path, timeout=10.0)
        c.row_factory = sqlite3.Row
        return c

    monkeypatch.setattr(mod, "_connect", _connect)
    return mod


def _mk_video_file(cfg, aweme_id):
    d = cfg.videos_dir / aweme_id
    d.mkdir(parents=True, exist_ok=True)
    (d / "video.mp4").write_bytes(b"x" * 10)
    cfg.audio_dir.mkdir(parents=True, exist_ok=True)
    (cfg.audio_dir / f"{aweme_id}.wav").write_bytes(b"y" * 10)


def test_only_done_videos_files_deleted(cfg, app_mod):
    insert_video(cfg, "900", status="done")
    insert_video(cfg, "901", status="downloaded")
    _mk_video_file(cfg, "900")
    _mk_video_file(cfg, "901")
    res = app_mod.api_delete_creator_files(SEC, {"what": "all"})
    assert res["deleted"]["mp4"] == 1 and res["deleted"]["wav"] == 1
    assert not (cfg.videos_dir / "900").exists()
    assert (cfg.videos_dir / "901" / "video.mp4").exists()
    assert (cfg.audio_dir / "901.wav").exists()


def test_invalid_what_rejected(cfg, app_mod):
    from fastapi import HTTPException
    with pytest.raises(HTTPException):
        app_mod.api_delete_creator_files(SEC, {"what": "../etc"})
