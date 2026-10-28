"""启动器包装逻辑 + orchestrator 调用 MediaCrawler 的方式（不实际联网）。"""
from src.mc_runtime import PostsLimiter, should_skip_existing
from src import orchestrator


def _page(n, has_more=1):
    return {"aweme_list": [{"aweme_id": str(i)} for i in range(n)], "has_more": has_more,
            "max_cursor": "c"}


def test_limiter_unlimited_passthrough():
    lim = PostsLimiter(0)
    p = _page(5)
    assert lim.apply(p) is p


def test_limiter_truncates_and_stops_pagination():
    lim = PostsLimiter(7)
    first = lim.apply(_page(5))
    assert len(first["aweme_list"]) == 5 and first["has_more"] == 1
    second = lim.apply(_page(5))
    assert len(second["aweme_list"]) == 2 and second["has_more"] == 0
    third = lim.apply(_page(5))
    assert third["aweme_list"] == [] and third["has_more"] == 0


def test_should_skip_existing(tmp_path):
    f = tmp_path / "v.mp4"
    assert not should_skip_existing(str(f))
    f.write_bytes(b"x" * 10)
    assert not should_skip_existing(str(f))  # 小于 1KB 视为未完整下载
    f.write_bytes(b"x" * 5000)
    assert should_skip_existing(str(f))


def test_run_mediacrawler_does_not_touch_mc_config(cfg, monkeypatch):
    dy_cfg = cfg.mediacrawler_dir / "config" / "dy_config.py"
    dy_cfg.parent.mkdir(parents=True)
    dy_cfg.write_text("ORIGINAL = 1\n", encoding="utf-8")
    captured = {}

    class R:
        returncode = 0

    def fake_run(cmd, cwd=None, **kw):
        captured["cmd"] = cmd
        captured["cwd"] = cwd
        return R()

    monkeypatch.setattr(orchestrator.subprocess, "run", fake_run)
    ok = orchestrator._run_mediacrawler(cfg, "MS4wTEST", login_type="cookie", max_videos=3)
    assert ok is True
    assert dy_cfg.read_text(encoding="utf-8") == "ORIGINAL = 1\n"
    assert not (dy_cfg.parent / "dy_config.py.bak").exists()
    assert "mediacrawler_launcher.py" in " ".join(captured["cmd"])
    assert captured["cmd"][-2:] == ["--max-videos", "3"]
    assert str(cfg.mediacrawler_dir) == captured["cwd"]


def test_run_mediacrawler_failure_returns_false(cfg, monkeypatch):
    class R:
        returncode = 1
    monkeypatch.setattr(orchestrator.subprocess, "run", lambda *a, **k: R())
    assert orchestrator._run_mediacrawler(cfg, "MS4wTEST") is False
