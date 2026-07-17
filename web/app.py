from __future__ import annotations

import datetime
import json
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from jinja2 import Environment, FileSystemLoader, select_autoescape
from pydantic import BaseModel

import sys
import os
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import load_config
from src.db import connect, add_creator, update_creator, now_ts
from src.orchestrator import _extract_sec_user_id, _fetch_creator_info, _update_creator_info

cfg = load_config()
TEMPLATES_DIR = Path(__file__).parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "xml"]),
)

app = FastAPI(title="douyin-reader")

# 后台任务列表（支持多任务并行）
_tasks: list[dict] = []


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(cfg.db_path, timeout=10.0)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")


def _fmt_create_time(ts: int | None) -> str:
    if not ts:
        return ""
    return datetime.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


def _fmt_duration(seconds) -> str:
    if seconds is None or seconds == 0:
        return "-"
    seconds = float(seconds)
    if seconds < 60:
        return f"{seconds:.1f}秒"
    m, s = divmod(seconds, 60)
    if m < 60:
        return f"{int(m)}分{int(s)}秒"
    h, m = divmod(m, 60)
    return f"{int(h)}时{int(m)}分{int(s)}秒"


env.filters["fmt_ts"] = _fmt_ts
env.filters["fmt_create_time"] = _fmt_create_time
env.filters["fmt_duration"] = _fmt_duration

STATUS_ZH = {
    "new": "待处理",
    "downloaded": "待转写",
    "transcribing": "转写中",
    "transcribed": "待摘要",
    "summarizing": "摘要中",
    "done": "已完成",
    "transcribe_failed": "转写失败",
    "summarize_failed": "摘要失败",
    "manual_failed": "人工处理",
}


def _status_zh(status: str) -> str:
    return STATUS_ZH.get(status, status)


env.filters["status_zh"] = _status_zh


@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    """博主卡片首页"""
    with _connect() as conn:
        creators = conn.execute(
            """SELECT c.sec_user_id, c.nickname, c.homepage_url, c.last_crawled_at,
                      c.avatar_url, c.intro, c.category, c.note,
                      (SELECT count(*) FROM videos v WHERE v.sec_user_id=c.sec_user_id) as video_count,
                      (SELECT count(*) FROM videos v WHERE v.sec_user_id=c.sec_user_id AND v.status='done') as done_count,
                      (SELECT count(*) FROM videos v WHERE v.sec_user_id=c.sec_user_id AND v.status NOT IN ('done')) as pending_count,
                      (SELECT max(v.create_time) FROM videos v WHERE v.sec_user_id=c.sec_user_id) as latest_video_time
               FROM creators c
               ORDER BY video_count DESC, c.first_seen_at"""
        ).fetchall()
    tmpl = env.get_template("home.html")
    return tmpl.render(request=request, creators=creators)


@app.get("/creator/{sec_user_id}", response_class=HTMLResponse)
def creator_view(sec_user_id: str, request: Request, status: str | None = None, q: str | None = None):
    """博主详情页：该博主的视频列表"""
    with _connect() as conn:
        creator = conn.execute(
            "SELECT * FROM creators WHERE sec_user_id=?", (sec_user_id,)
        ).fetchone()
        if creator is None:
            raise HTTPException(404, "creator not found")

        sql = """SELECT aweme_id, title, status, create_time, liked_count,
                        summary, video_duration, transcribe_duration,
                        (SELECT summarize_duration FROM llm_summaries s WHERE s.aweme_id=v.aweme_id AND s.is_primary=1) as summarize_duration
                 FROM videos v WHERE sec_user_id=?"""
        params: list = [sec_user_id]
        if status:
            sql += " AND status=?"
            params.append(status)
        if q:
            sql += " AND (title LIKE ? OR summary LIKE ? OR desc_text LIKE ?)"
            params.extend([f"%{q}%", f"%{q}%", f"%{q}%"])
        sql += " ORDER BY create_time DESC"
        rows = conn.execute(sql, params).fetchall()

        status_counts = {}
        for r in conn.execute(
            "SELECT status, count(*) as n FROM videos WHERE sec_user_id=? GROUP BY status", (sec_user_id,)
        ).fetchall():
            status_counts[r["status"]] = r["n"]

    tmpl = env.get_template("creator.html")
    return tmpl.render(
        request=request, creator=creator, rows=rows,
        status_counts=status_counts, current_status=status, current_q=q or "",
    )


@app.get("/video/{aweme_id}", response_class=HTMLResponse)
def detail_view(aweme_id: str, request: Request, provider: str | None = None):
    with _connect() as conn:
        row = conn.execute(
            "SELECT * FROM videos WHERE aweme_id=?", (aweme_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(404, "video not found")
        creator = conn.execute(
            "SELECT * FROM creators WHERE sec_user_id=?", (row["sec_user_id"],)
        ).fetchone()

        all_summaries = conn.execute(
            "SELECT id, provider, model, is_primary, length(article) as article_len, "
            "length(summary) as summary_len, summarize_duration "
            "FROM llm_summaries WHERE aweme_id=? ORDER BY is_primary DESC, id",
            (aweme_id,),
        ).fetchall()

        chosen = None
        if provider:
            chosen = conn.execute(
                "SELECT * FROM llm_summaries WHERE aweme_id=? AND provider=?",
                (aweme_id, provider),
            ).fetchone()
        if chosen is None:
            chosen = conn.execute(
                "SELECT * FROM llm_summaries WHERE aweme_id=? AND is_primary=1",
                (aweme_id,),
            ).fetchone()
        if chosen is None and all_summaries:
            chosen = conn.execute(
                "SELECT * FROM llm_summaries WHERE id=?", (all_summaries[0]["id"],)
            ).fetchone()

    summarize_duration = chosen["summarize_duration"] if chosen else None

    transcript = ""
    if row["transcript_path"]:
        tp = cfg.project_root / row["transcript_path"]
        if tp.exists():
            transcript = tp.read_text(encoding="utf-8")

    key_points = json.loads(chosen["key_points"]) if chosen and chosen["key_points"] else []
    knowledge_points = json.loads(chosen["knowledge_points"]) if chosen and chosen["knowledge_points"] else []
    article = chosen["article"] if chosen else None
    summary = chosen["summary"] if chosen else None

    tmpl = env.get_template("detail.html")
    return tmpl.render(
        request=request, row=row, creator=creator,
        transcript=transcript, article=article, summary=summary,
        key_points=key_points, knowledge_points=knowledge_points,
        all_summaries=all_summaries,
        current_provider=chosen["provider"] if chosen else None,
        current_model=chosen["model"] if chosen else None,
        summarize_duration=summarize_duration,
    )


@app.post("/video/{aweme_id}/set-primary/{summary_id}")
def set_primary(aweme_id: str, summary_id: int):
    from src.summarize_worker import set_primary_summary
    set_primary_summary(cfg, aweme_id, summary_id)
    return {"ok": True}


class AddCreatorRequest(BaseModel):
    url: str
    nickname: str | None = None
    category: str | None = None
    note: str | None = None


class UpdateCreatorRequest(BaseModel):
    nickname: str | None = None
    category: str | None = None
    note: str | None = None
    enabled: int | None = None


class RunRequest(BaseModel):
    sec_user_id: str
    login_type: str = "cookie"
    max_videos: int = 0
    stages: str = "crawl,transcribe,summarize"
    batch: int = 5
    transcribe_limit: int = 0
    summarize_limit: int = 0
    asr_provider: str = ""  # local_whisper / groq_whisper / 空则用配置文件


def _run_background_task(task_type: str, cmd: list[str], cwd: str = None) -> None:
    """在后台线程里跑 subprocess，支持多任务并行"""
    task = {
        "type": task_type, "message": "running",
        "started_at": time.time(), "log": [],
        "pid": 0, "process": None, "done": False,
    }
    _tasks.append(task)

    def _worker():
        try:
            env = os.environ.copy()
            env["PYTHONUNBUFFERED"] = "1"
            process = subprocess.Popen(
                cmd, cwd=cwd or str(cfg.project_root),
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, env=env,
            )
            task["pid"] = process.pid
            task["process"] = process
            for line in process.stdout:
                task["log"].append(line.rstrip())
                if len(task["log"]) > 300:
                    task["log"] = task["log"][-200:]
            process.wait()
            task["message"] = f"exit={process.returncode}"
        except Exception as e:
            task["message"] = f"error: {e}"
        finally:
            task["done"] = True
            task["process"] = None
    threading.Thread(target=_worker, daemon=True).start()


@app.post("/api/creator/add")
def api_add_creator(req: AddCreatorRequest):
    sec = _extract_sec_user_id(req.url)
    if not sec:
        raise HTTPException(400, "cannot parse sec_user_id from URL")
    add_creator(cfg, sec_user_id=sec, homepage_url=req.url,
                nickname=req.nickname, category=req.category, note=req.note)
    # 后台获取博主信息
    info = _fetch_creator_info(cfg, sec)
    if info and "error" not in info:
        _update_creator_info(cfg, sec, info)
    return {"ok": True, "sec_user_id": sec, "info": info}


@app.post("/api/creator/{sec_user_id}/update")
def api_update_creator(sec_user_id: str, req: UpdateCreatorRequest):
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    if not fields:
        raise HTTPException(400, "no fields to update")
    update_creator(cfg, sec_user_id, **fields)
    return {"ok": True}


@app.post("/api/creator/{sec_user_id}/refresh-info")
def api_refresh_info(sec_user_id: str):
    info = _fetch_creator_info(cfg, sec_user_id)
    if info and "error" not in info:
        _update_creator_info(cfg, sec_user_id, info)
        return {"ok": True, "info": info}
    raise HTTPException(500, f"fetch failed: {info}")


@app.post("/api/creator/{sec_user_id}/run")
def api_run_creator(sec_user_id: str, req: RunRequest):
    cmd = [sys.executable, "-m", "src.orchestrator", "run",
           "--sec-user-id", sec_user_id, "--login-type", req.login_type,
           "--stages", req.stages, "--batch", str(req.batch)]
    if req.max_videos > 0:
        cmd.extend(["--max-videos", str(req.max_videos)])
    if req.transcribe_limit > 0:
        cmd.extend(["--transcribe-limit", str(req.transcribe_limit)])
    if req.summarize_limit > 0:
        cmd.extend(["--summarize-limit", str(req.summarize_limit)])
    if req.asr_provider:
        cmd.extend(["--asr-provider", req.asr_provider])
    stage_label = req.stages.replace(",", "+")
    _run_background_task(f"run:{stage_label}:{sec_user_id[:12]}", cmd)
    return {"ok": True, "message": "task started"}


@app.post("/api/run-all")
def api_run_all():
    cmd = [sys.executable, "-m", "src.orchestrator", "run-all"]
    _run_background_task("run-all", cmd)
    return {"ok": True, "message": "task started"}


@app.get("/api/status")
def api_status():
    """全局状态概览"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, count(*) as n FROM videos GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "downloaded": counts.get("downloaded", 0) + counts.get("new", 0),
        "transcribed": counts.get("transcribed", 0),
        "failed": counts.get("transcribe_failed", 0) + counts.get("summarize_failed", 0),
        "done": counts.get("done", 0),
        "task_running": any(not t["done"] for t in _tasks),
        "task_type": ", ".join(t["type"] for t in _tasks if not t["done"]),
        "status_detail": {k: {"count": v, "label": STATUS_ZH.get(k, k)} for k, v in counts.items()},
    }


@app.post("/api/reauth")
def api_reauth():
    import shutil
    browser_data = cfg.mediacrawler_dir / "browser_data" / "dy_user_data_dir"
    if browser_data.exists():
        shutil.rmtree(browser_data)
        return {"ok": True, "message": "login state cleared, use --login-type qrcode next time"}
    return {"ok": True, "message": "no login state found"}


@app.post("/api/task-stop")
def api_task_stop():
    """停止当前后台任务"""
    # 停止所有运行中的任务
    stopped = 0
    for t in _tasks:
        if not t["done"]:
            process = t.get("process")
            if process:
                try:
                    import subprocess as sp
                    sp.run(["pkill", "-P", str(process.pid)], capture_output=True)
                    process.terminate()
                    process.wait(timeout=5)
                except Exception:
                    try: process.kill()
                    except: pass
            t["done"] = True
            t["message"] = "stopped by user"
            stopped += 1
    if stopped == 0:
        return {"ok": True, "message": "no task running"}
    return {"ok": True, "message": f"stopped {stopped} task(s)"}

# 旧代码不会执行到这里
    if process:
        # 杀进程树（含子进程）
        import signal
        try:
            # 先杀子进程再杀主进程
            import subprocess as sp
            sp.run(["pkill", "-P", str(process.pid)], capture_output=True)
            process.terminate()
            process.wait(timeout=5)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
    return {"ok": True, "message": f"stopped {stopped} task(s)"}


@app.get("/api/task-status")
def api_task_status():
    """任务状态 + 全局进度"""
    with _connect() as conn:
        rows = conn.execute(
            "SELECT status, count(*) as n FROM videos GROUP BY status"
        ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    running_tasks = [t for t in _tasks if not t["done"]]
    # 清理已完成超过5分钟的任务
    import time as _t
    _tasks[:] = [t for t in _tasks if not t["done"] or (_t.time() - t["started_at"] - 300 < 0)]

    # 合并所有运行中任务的日志
    all_logs = []
    for t in running_tasks:
        all_logs.append(f"[{t['type']}] {t['log'][-3:][-1]}" if t['log'] else "")

    # 优先显示运行中的任务，否则显示最近完成的
    active = running_tasks[0] if running_tasks else (_tasks[-1] if _tasks else None)
    return {
        "running": len(running_tasks) > 0,
        "type": active["type"] if active else "",
        "message": active["message"] if active else "",
        "elapsed": time.time() - active["started_at"] if active else 0,
        "log_tail": (active["log"][-20:] if active else []),
        "tasks_running": [{"type": t["type"], "message": t["message"]} for t in running_tasks],
        "progress": {
            "downloaded": counts.get("downloaded", 0) + counts.get("new", 0),
            "transcribed": counts.get("transcribed", 0),
            "summarizing": counts.get("summarizing", 0),
            "transcribing": counts.get("transcribing", 0),
            "done": counts.get("done", 0),
            "failed": counts.get("transcribe_failed", 0) + counts.get("summarize_failed", 0),
            "total": sum(counts.values()),
        },
    }


@app.get("/health")
def health():
    return {"status": "ok"}


# ============ 单条视频操作 ============

class VideoActionRequest(BaseModel):
    asr_provider: str = ""
    llm_provider: str = ""


@app.post("/api/video/{aweme_id}/transcribe")
def api_transcribe_one(aweme_id: str, req: VideoActionRequest):
    """单条视频重新转写"""
    cmd = [sys.executable, "-m", "src.orchestrator", "transcribe-one",
           "--aweme-id", aweme_id]
    if req.asr_provider:
        cmd.extend(["--asr-provider", req.asr_provider])
    _run_background_task(f"转写:{aweme_id[:12]}", cmd)
    return {"ok": True, "message": "转写任务已启动"}


@app.post("/api/video/{aweme_id}/summarize")
def api_summarize_one(aweme_id: str, req: VideoActionRequest):
    """单条视频重新摘要"""
    cmd = [sys.executable, "-m", "src.orchestrator", "summarize-one",
           "--aweme-id", aweme_id]
    if req.llm_provider:
        cmd.extend(["--llm-provider", req.llm_provider])
    _run_background_task(f"摘要:{aweme_id[:12]}", cmd)
    return {"ok": True, "message": "摘要任务已启动"}


@app.post("/api/video/{aweme_id}/reset")
def api_reset_video(aweme_id: str, req: dict):
    """重置视频状态"""
    status = req.get("status", "downloaded")
    with _connect() as conn:
        n = conn.execute(
            "UPDATE videos SET status=?, status_message=NULL, retry_count=0 WHERE aweme_id=?",
            (status, aweme_id),
        ).rowcount
    if n == 0:
        raise HTTPException(404, "video not found")
    return {"ok": True, "message": f"状态已重置为 {status}"}


@app.delete("/api/video/{aweme_id}")
def api_delete_video(aweme_id: str):
    """删除视频（仅从 DB 删除，不删文件）"""
    with _connect() as conn:
        n = conn.execute("DELETE FROM videos WHERE aweme_id=?", (aweme_id,)).rowcount
        conn.execute("DELETE FROM llm_summaries WHERE aweme_id=?", (aweme_id,))
    if n == 0:
        raise HTTPException(404, "video not found")
    return {"ok": True, "message": "视频已删除"}


# ============ 批量操作 ============

class BatchActionRequest(BaseModel):
    aweme_ids: list[str]
    action: str  # transcribe | summarize | reset | delete
    asr_provider: str = ""
    llm_provider: str = ""
    status: str = "downloaded"


@app.post("/api/video/batch")
def api_batch_action(req: BatchActionRequest):
    """批量操作视频"""
    if not req.aweme_ids:
        raise HTTPException(400, "未选择视频")
    if req.action == "transcribe":
        cmd = [sys.executable, "-m", "src.orchestrator", "transcribe-batch",
               "--aweme-ids", ",".join(req.aweme_ids)]
        if req.asr_provider:
            cmd.extend(["--asr-provider", req.asr_provider])
        _run_background_task(f"批量转写:{len(req.aweme_ids)}条", cmd)
    elif req.action == "summarize":
        cmd = [sys.executable, "-m", "src.orchestrator", "summarize-batch",
               "--aweme-ids", ",".join(req.aweme_ids)]
        if req.llm_provider:
            cmd.extend(["--llm-provider", req.llm_provider])
        _run_background_task(f"批量摘要:{len(req.aweme_ids)}条", cmd)
    elif req.action == "reset":
        with _connect() as conn:
            for aid in req.aweme_ids:
                conn.execute(
                    "UPDATE videos SET status=?, status_message=NULL, retry_count=0 WHERE aweme_id=?",
                    (req.status, aid),
                )
        return {"ok": True, "message": f"已重置 {len(req.aweme_ids)} 条"}
    elif req.action == "delete":
        with _connect() as conn:
            for aid in req.aweme_ids:
                conn.execute("DELETE FROM videos WHERE aweme_id=?", (aid,))
                conn.execute("DELETE FROM llm_summaries WHERE aweme_id=?", (aid,))
        return {"ok": True, "message": f"已删除 {len(req.aweme_ids)} 条"}
    else:
        raise HTTPException(400, f"未知操作: {req.action}")
    return {"ok": True, "message": f"任务已启动: {req.action} {len(req.aweme_ids)} 条"}


# ============ 磁盘监控 ============

@app.get("/api/disk-usage")
def api_disk_usage():
    """磁盘占用"""
    import shutil as _shutil
    total, used, free = _shutil.disk_usage(str(cfg.project_root))

    def _dir_size(path: Path) -> int:
        if not path.exists():
            return 0
        total_size = 0
        for f in path.rglob("*"):
            if f.is_file():
                try:
                    total_size += f.stat().st_size
                except OSError:
                    pass
        return total_size

    mp4_size = _dir_size(cfg.videos_dir)
    wav_size = _dir_size(cfg.audio_dir)
    text_size = _dir_size(cfg.text_dir)
    summary_size = _dir_size(cfg.summary_dir)
    db_size = cfg.db_path.stat().st_size if cfg.db_path.exists() else 0

    return {
        "disk": {
            "total_gb": round(total / 1024**3, 1),
            "used_gb": round(used / 1024**3, 1),
            "free_gb": round(free / 1024**3, 1),
        },
        "project": {
            "mp4_mb": round(mp4_size / 1024**2, 1),
            "wav_mb": round(wav_size / 1024**2, 1),
            "text_mb": round(text_size / 1024**2, 2),
            "summary_mb": round(summary_size / 1024**2, 2),
            "db_mb": round(db_size / 1024**2, 2),
            "total_mb": round((mp4_size + wav_size + text_size + summary_size + db_size) / 1024**2, 1),
        },
    }


@app.post("/api/cleanup")
def api_cleanup(req: dict):
    """清理临时文件（异步任务，有进度反馈）"""
    what = req.get("what", "wav")
    label = "wav 音频" if what == "wav" else "已完成视频的 mp4" if what == "mp4" else "wav+mp4"

    cmd = [sys.executable, "-m", "src.orchestrator", "cleanup",
           "--stats"]
    if what in ("wav", "all"):
        cmd.append("--remove-wav")
    if what in ("mp4", "all"):
        cmd.append("--remove-mp4")
        cmd.append("--done-only")
    _run_background_task(f"清理{label}", cmd)
    return {"ok": True, "message": f"正在清理{label}，请查看进度"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=8000)