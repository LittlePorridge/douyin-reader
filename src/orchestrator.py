from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from .config import Config, load_config
from .db import connect, init_db, add_creator, get_creator, list_creators, update_creator, now_ts
from .importer import import_latest
from .transcribe_worker import tick as transcribe_tick
from .summarize_worker import tick as summarize_tick
from .llm_client import load_provider_from_yaml


SEC_USER_ID_PATTERN = re.compile(r"MS4wLjAB[A-Za-z0-9_\-]+")

# LLM 单次跑摘要的批次上限（避免一次烧太多钱）
LLM_BATCH_LIMIT = 10
# ASR 单次跑转写的批次上限（避免 CPU 长时间占满）
ASR_BATCH_LIMIT = 20


def _extract_sec_user_id(url: str) -> str | None:
    m = SEC_USER_ID_PATTERN.search(url)
    return m.group(0) if m else None


def _gen_dy_config(creator_url: str) -> str:
    return f'''# -*- coding: utf-8 -*-
# 自动生成 - do not edit
PUBLISH_TIME_TYPE = 0
DY_SPECIFIED_ID_LIST = []
DY_CREATOR_ID_LIST = [
    "{creator_url}",
]
'''


def _run_mediacrawler(cfg: Config, sec_user_id: str, login_type: str = "cookie",
                      max_videos: int = 0) -> bool:
    """调 MediaCrawler 子进程。max_videos=0 表示全量"""
    dy_config_path = cfg.mediacrawler_dir / "config" / "dy_config.py"
    backup_path = dy_config_path.with_suffix(".py.bak")
    if dy_config_path.exists() and not backup_path.exists():
        backup_path.write_text(dy_config_path.read_text(encoding="utf-8"), encoding="utf-8")
    creator_url = f"https://www.douyin.com/user/{sec_user_id}"
    dy_config_path.write_text(_gen_dy_config(creator_url), encoding="utf-8")

    env = os.environ.copy()
    env["DY_MAX_POSTS"] = str(max_videos) if max_videos > 0 else "0"

    try:
        print(f"[orchestrator] running MediaCrawler for {sec_user_id} (max_videos={max_videos or 'unlimited'})")
        cmd = ["uv", "run", "main.py", "--platform", "dy", "--lt", login_type, "--type", "creator"]
        t0 = time.time()
        r = subprocess.run(cmd, cwd=str(cfg.mediacrawler_dir), env=env)
        dt = time.time() - t0
        print(f"[orchestrator] MediaCrawler exit={r.returncode} dt={dt:.1f}s")
        return r.returncode == 0
    finally:
        if backup_path.exists():
            dy_config_path.write_text(backup_path.read_text(encoding="utf-8"), encoding="utf-8")


def _fetch_creator_info(cfg: Config, sec_user_id: str) -> dict | None:
    script = cfg.mediacrawler_dir / "fetch_creator_info.py"
    if not script.exists():
        src_script = cfg.project_root / "scripts" / "fetch_creator_info.py"
        if src_script.exists():
            import shutil
            shutil.copy(str(src_script), str(script))
    if not script.exists():
        return None
    cmd = ["uv", "run", "python", "fetch_creator_info.py", sec_user_id]
    try:
        r = subprocess.run(cmd, cwd=str(cfg.mediacrawler_dir),
                           capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            return None
        for line in r.stdout.strip().split("\n"):
            if line.startswith("{"):
                import json
                return json.loads(line)
        return None
    except Exception:
        return None


def _update_creator_info(cfg: Config, sec_user_id: str, info: dict) -> None:
    update_creator(cfg, sec_user_id,
                   nickname=info.get("nickname"),
                   avatar_url=info.get("avatar_url"),
                   intro=info.get("intro"))


def _promote_new_to_downloaded(cfg: Config) -> int:
    with connect(cfg) as conn:
        rows = conn.execute("SELECT aweme_id FROM videos WHERE status='new'").fetchall()
    promoted = 0
    for row in rows:
        paths = cfg.paths_for(row["aweme_id"])
        if paths["video"].exists():
            with connect(cfg) as conn:
                conn.execute(
                    "UPDATE videos SET status='downloaded', video_path=?, downloaded_at=? WHERE aweme_id=? AND status='new'",
                    (str(paths["video"].relative_to(cfg.project_root)), now_ts(), row["aweme_id"]),
                )
            promoted += 1
    if promoted:
        print(f"[orchestrator] promoted {promoted} videos new→downloaded")
    return promoted


def _do_crawl(cfg: Config, sec_user_id: str, login_type: str, max_videos: int) -> None:
    """阶段 1：抓取 + 导入 + 获取博主信息"""
    _run_mediacrawler(cfg, sec_user_id, login_type=login_type, max_videos=max_videos)
    r = import_latest(cfg, sec_user_id)
    print(f"  import: new={r.new_count} existing={r.existing_count}")
    info = _fetch_creator_info(cfg, sec_user_id)
    if info and "error" not in info:
        _update_creator_info(cfg, sec_user_id, info)
        print(f"  creator info: {info.get('nickname')}")


def _do_transcribe(cfg: Config, batch: int, limit: int = 0, asr_provider: str = "") -> int:
    """阶段 2：ASR 转写。返回处理条数"""
    if asr_provider:
        os.environ["ASR_PROVIDER_OVERRIDE"] = asr_provider
    _promote_new_to_downloaded(cfg)
    processed = 0
    effective_limit = limit if limit > 0 else ASR_BATCH_LIMIT
    while True:
        remaining = effective_limit - processed if effective_limit > 0 else batch
        if remaining <= 0:
            print(f"[transcribe] reached batch limit ({effective_limit}), stopping")
            break
        n = transcribe_tick(cfg, max_process=min(batch, remaining))
        if n == 0:
            break
        processed += n
        print(f"[transcribe] processed {processed} videos so far")
    return processed


def _do_summarize(cfg: Config, batch: int, limit: int = 0) -> int:
    """阶段 3：LLM 摘要。返回处理条数"""
    provider = load_provider_from_yaml(cfg.llm_providers_path)
    processed = 0
    effective_limit = limit if limit > 0 else LLM_BATCH_LIMIT
    while True:
        remaining = effective_limit - processed if effective_limit > 0 else batch
        if remaining <= 0:
            print(f"[summarize] reached batch limit ({effective_limit}), stopping")
            break
        n = summarize_tick(cfg, provider, max_process=min(batch, remaining))
        if n == 0:
            break
        processed += n
        print(f"[summarize] processed {processed} videos so far")
    return processed


def _get_pending_counts(cfg: Config, sec_user_id: str | None = None) -> dict:
    """获取各阶段待处理数量"""
    with connect(cfg) as conn:
        if sec_user_id:
            sql = "SELECT status, count(*) as n FROM videos WHERE sec_user_id=? GROUP BY status"
            rows = conn.execute(sql, (sec_user_id,)).fetchall()
        else:
            rows = conn.execute(
                "SELECT status, count(*) as n FROM videos GROUP BY status"
            ).fetchall()
    counts = {r["status"]: r["n"] for r in rows}
    return {
        "downloaded": counts.get("downloaded", 0) + counts.get("new", 0),
        "transcribed": counts.get("transcribed", 0),
        "failed": counts.get("transcribe_failed", 0) + counts.get("summarize_failed", 0),
        "done": counts.get("done", 0),
    }


# ============ CLI 命令 ============

def cmd_add_creator(cfg: Config, args: argparse.Namespace) -> None:
    sec = _extract_sec_user_id(args.url)
    if not sec:
        print(f"cannot parse sec_user_id from {args.url}")
        sys.exit(1)
    add_creator(cfg, sec_user_id=sec, homepage_url=args.url,
                nickname=args.nickname, category=args.category, note=args.note)
    print(f"creator added: {sec} nickname={args.nickname}")


def cmd_run(cfg: Config, args: argparse.Namespace) -> None:
    """完整流程，可选择执行哪些阶段"""
    init_db(cfg)
    if args.url:
        sec = _extract_sec_user_id(args.url)
        if not sec:
            print(f"cannot parse sec_user_id from {args.url}")
            sys.exit(1)
        if get_creator(cfg, sec) is None:
            add_creator(cfg, sec_user_id=sec, homepage_url=args.url,
                        nickname=args.nickname, category=args.category, note=args.note)
    else:
        sec = args.sec_user_id
        if not sec:
            print("--url or --sec-user-id required")
            sys.exit(1)

    stages = args.stages.split(",") if args.stages else ["crawl", "transcribe", "summarize"]
    max_videos = args.max_videos or 0
    total_transcribed = 0
    total_summarized = 0

    if "crawl" in stages:
        print(f"=== [crawl] max_videos={max_videos or 'all'} ===")
        _do_crawl(cfg, sec, args.login_type, max_videos)

    if "transcribe" in stages:
        pending = _get_pending_counts(cfg, sec)
        print(f"=== [transcribe] pending: {pending['downloaded']} downloaded, {pending['failed']} failed ===")
        if pending["downloaded"] + pending["failed"] == 0:
            print("  nothing to transcribe")
        else:
            total_transcribed = _do_transcribe(cfg, args.batch, limit=args.transcribe_limit, asr_provider=args.asr_provider)

    if "summarize" in stages:
        pending = _get_pending_counts(cfg, sec)
        print(f"=== [summarize] pending: {pending['transcribed']} transcribed ===")
        if pending["transcribed"] == 0:
            print("  nothing to summarize")
        else:
            total_summarized = _do_summarize(cfg, args.batch, limit=args.summarize_limit)

    print(f"\n=== done: transcribed={total_transcribed} summarized={total_summarized} ===")
    with connect(cfg) as conn:
        stats = conn.execute(
            "SELECT status, count(*) as n FROM videos WHERE sec_user_id=? GROUP BY status", (sec,)
        ).fetchall()
        for s in stats:
            print(f"  {s['status']}: {s['n']}")


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> None:
    init_db(cfg)
    creators = list_creators(cfg, only_enabled=True)
    if not creators:
        print("no enabled creators. add one: python -m src.orchestrator add-creator --url ...")
        return
    print(f"found {len(creators)} enabled creators")
    for i, c in enumerate(creators, 1):
        sec = c["sec_user_id"]
        print(f"\n=== [{i}/{len(creators)}] {c['nickname'] or sec[:20]} ===")
        _do_crawl(cfg, sec, args.login_type, args.max_videos or 0)
        _do_transcribe(cfg, args.batch, limit=args.transcribe_limit)
        _do_summarize(cfg, args.batch, limit=args.summarize_limit)
    print(f"\n=== all {len(creators)} creators done ===")


def cmd_transcribe(cfg: Config, args: argparse.Namespace) -> None:
    """只跑 ASR"""
    init_db(cfg)
    pending = _get_pending_counts(cfg)
    print(f"pending: {pending['downloaded']} downloaded, {pending['failed']} failed")
    n = _do_transcribe(cfg, args.batch, limit=args.limit)
    print(f"transcribed {n} videos")


def cmd_summarize(cfg: Config, args: argparse.Namespace) -> None:
    """只跑 LLM 摘要"""
    init_db(cfg)
    pending = _get_pending_counts(cfg)
    print(f"pending: {pending['transcribed']} transcribed")
    if pending["transcribed"] == 0:
        print("nothing to summarize")
        return
    if pending["transcribed"] > LLM_BATCH_LIMIT and not args.yes:
        print(f"warning: {pending['transcribed']} videos pending, will process up to {LLM_BATCH_LIMIT}")
        if not input(f"continue? (y/n) ").strip().lower().startswith("y"):
            print("aborted")
            return
    n = _do_summarize(cfg, args.batch, limit=args.limit)
    print(f"summarized {n} videos")


def cmd_crawl(cfg: Config, args: argparse.Namespace) -> None:
    init_db(cfg)
    _do_crawl(cfg, args.sec_user_id, args.login_type, args.max_videos or 0)


def cmd_list_creators(cfg: Config, args: argparse.Namespace) -> None:
    creators = list_creators(cfg, only_enabled=False)
    if not creators:
        print("no creators in db")
        return
    for c in creators:
        with connect(cfg) as conn:
            n = conn.execute(
                "SELECT count(*) as n FROM videos WHERE sec_user_id=?", (c["sec_user_id"],)
            ).fetchone()["n"]
            done = conn.execute(
                "SELECT count(*) as n FROM videos WHERE sec_user_id=? AND status='done'",
                (c["sec_user_id"],),
            ).fetchone()["n"]
        cat = f" [{c['category']}]" if c["category"] else ""
        print(f"  {c['nickname'] or '(no nickname)':20s}{cat:12s}  videos={n:4d} done={done:4d}")


def cmd_edit_creator(cfg: Config, args: argparse.Namespace) -> None:
    fields = {}
    if args.nickname is not None: fields["nickname"] = args.nickname
    if args.category is not None: fields["category"] = args.category
    if args.note is not None: fields["note"] = args.note
    if args.enabled is not None: fields["enabled"] = 1 if args.enabled else 0
    if not fields:
        print("no fields to update")
        return
    update_creator(cfg, args.sec_user_id, **fields)
    print(f"updated: {fields}")


def cmd_reauth(cfg: Config, args: argparse.Namespace) -> None:
    browser_data = cfg.mediacrawler_dir / "browser_data" / "dy_user_data_dir"
    if browser_data.exists():
        import shutil
        shutil.rmtree(browser_data)
        print(f"cleared login state")
    else:
        print("no login state found")


def cmd_refresh_creator_info(cfg: Config, args: argparse.Namespace) -> None:
    info = _fetch_creator_info(cfg, args.sec_user_id)
    if info and "error" not in info:
        _update_creator_info(cfg, args.sec_user_id, info)
        print(f"updated: {info.get('nickname')}")
    else:
        print(f"failed: {info}")


def cmd_reset(cfg: Config, args: argparse.Namespace) -> None:
    with connect(cfg) as conn:
        n = conn.execute(
            "UPDATE videos SET status=?, status_message=NULL, retry_count=0 WHERE aweme_id=?",
            (args.status, args.aweme_id),
        ).rowcount
    print(f"reset {n} row(s)")


def cmd_transcribe_one(cfg: Config, args: argparse.Namespace) -> None:
    """单条视频转写"""
    init_db(cfg)
    from .transcribe_worker import transcribe_one
    # 先重置为 downloaded
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE videos SET status='downloaded', status_message=NULL, retry_count=0 WHERE aweme_id=?",
            (args.aweme_id,),
        )
    if args.asr_provider:
        os.environ["ASR_PROVIDER_OVERRIDE"] = args.asr_provider
    transcribe_one(cfg, args.aweme_id)
    print(f"transcribe done: {args.aweme_id}")


def cmd_summarize_one(cfg: Config, args: argparse.Namespace) -> None:
    """单条视频摘要"""
    init_db(cfg)
    if args.llm_provider:
        # 临时切换 active provider
        import yaml
        with cfg.llm_providers_path.open("r") as f:
            ycfg = yaml.safe_load(f)
        ycfg["active_provider"] = args.llm_provider
        with cfg.llm_providers_path.open("w") as f:
            yaml.safe_dump(ycfg, f, allow_unicode=True)
    from .summarize_worker import summarize_one
    from .llm_client import load_provider_from_yaml
    provider = load_provider_from_yaml(cfg.llm_providers_path)
    # 重置为 transcribed
    with connect(cfg) as conn:
        conn.execute(
            "UPDATE videos SET status='transcribed', status_message=NULL WHERE aweme_id=?",
            (args.aweme_id,),
        )
    summarize_one(cfg, args.aweme_id, provider)
    print(f"summarize done: {args.aweme_id}")


def cmd_transcribe_batch(cfg: Config, args: argparse.Namespace) -> None:
    """批量转写指定视频"""
    init_db(cfg)
    from .transcribe_worker import transcribe_one
    if args.asr_provider:
        os.environ["ASR_PROVIDER_OVERRIDE"] = args.asr_provider
    ids = args.aweme_ids.split(",")
    for i, aid in enumerate(ids):
        aid = aid.strip()
        if not aid:
            continue
        print(f"=== [{i+1}/{len(ids)}] {aid} ===")
        with connect(cfg) as conn:
            conn.execute(
                "UPDATE videos SET status='downloaded', status_message=NULL, retry_count=0 WHERE aweme_id=?",
                (aid,),
            )
        transcribe_one(cfg, aid)
    print(f"batch transcribe done: {len(ids)} videos")


def cmd_summarize_batch(cfg: Config, args: argparse.Namespace) -> None:
    """批量摘要指定视频"""
    init_db(cfg)
    from .summarize_worker import summarize_one
    from .llm_client import load_provider_from_yaml
    provider = load_provider_from_yaml(cfg.llm_providers_path)
    ids = args.aweme_ids.split(",")
    for i, aid in enumerate(ids):
        aid = aid.strip()
        if not aid:
            continue
        print(f"=== [{i+1}/{len(ids)}] {aid} ===")
        with connect(cfg) as conn:
            conn.execute(
                "UPDATE videos SET status='transcribed', status_message=NULL WHERE aweme_id=?",
                (aid,),
            )
        summarize_one(cfg, aid, provider)
    print(f"batch summarize done: {len(ids)} videos")


def cmd_cleanup(cfg: Config, args: argparse.Namespace) -> None:
    """清理临时文件"""
    import shutil as _shutil
    deleted_wav = 0
    deleted_mp4 = 0
    total_bytes = 0

    if args.remove_wav or args.all:
        for f in cfg.audio_dir.glob("*.wav"):
            sz = f.stat().st_size
            f.unlink()
            deleted_wav += 1
            total_bytes += sz
        print(f"deleted {deleted_wav} wav files")

    if args.remove_mp4 or args.all:
        with connect(cfg) as conn:
            if args.done_only:
                rows = conn.execute(
                    "SELECT aweme_id FROM videos WHERE status='done'"
                ).fetchall()
            else:
                rows = conn.execute("SELECT aweme_id FROM videos").fetchall()
        for row in rows:
            vdir = cfg.videos_dir / row["aweme_id"]
            if vdir.exists():
                for f in vdir.rglob("*"):
                    if f.is_file():
                        total_bytes += f.stat().st_size
                        deleted_mp4 += 1
                _shutil.rmtree(str(vdir), ignore_errors=True)
        print(f"deleted {deleted_mp4} mp4 files")

    if args.stats:
        total, used, free = _shutil.disk_usage(str(cfg.project_root))
        print(f"磁盘: 总 {total/1024**3:.1f}GB / 已用 {used/1024**3:.1f}GB / 剩余 {free/1024**3:.1f}GB")
        # 项目占用
        def _dir_size(path):
            if not path.exists():
                return 0
            s = 0
            for f in path.rglob("*"):
                if f.is_file():
                    try:
                        s += f.stat().st_size
                    except OSError:
                        pass
            return s
        print(f"mp4: {_dir_size(cfg.videos_dir)/1024**2:.1f}MB")
        print(f"wav: {_dir_size(cfg.audio_dir)/1024**2:.1f}MB")
        print(f"text: {_dir_size(cfg.text_dir)/1024**2:.2f}MB")
        print(f"summaries: {_dir_size(cfg.summary_dir)/1024**2:.2f}MB")

    mb = total_bytes / 1024**2
    print(f"共清理 {mb:.1f}MB")


def cmd_status(cfg: Config, args: argparse.Namespace) -> None:
    """查看处理状态概览"""
    init_db(cfg)
    pending = _get_pending_counts(cfg)
    print("=== 全局状态 ===")
    print(f"  downloaded (待转写):  {pending['downloaded']}")
    print(f"  transcribed (待摘要): {pending['transcribed']}")
    print(f"  failed:               {pending['failed']}")
    print(f"  done:                 {pending['done']}")
    if args.sec_user_id:
        pending = _get_pending_counts(cfg, args.sec_user_id)
        print(f"\n=== {args.sec_user_id[:20]}... ===")
        print(f"  downloaded: {pending['downloaded']}  transcribed: {pending['transcribed']}  done: {pending['done']}")


def main() -> None:
    cfg = load_config()
    cfg.ensure_dirs()

    parser = argparse.ArgumentParser(prog="douyin-reader", description="抖音视频阅读器")
    sub = parser.add_subparsers(dest="cmd", required=True)

    # add-creator
    p_add = sub.add_parser("add-creator", help="添加博主")
    p_add.add_argument("--url", required=True)
    p_add.add_argument("--nickname", default=None)
    p_add.add_argument("--category", default=None)
    p_add.add_argument("--note", default=None)
    p_add.set_defaults(func=lambda a: cmd_add_creator(cfg, a))

    # run (完整流程，可选阶段)
    p_run = sub.add_parser("run", help="跑流程（可选阶段）")
    p_run.add_argument("--url", help="博主主页 URL")
    p_run.add_argument("--sec-user-id", help="或 sec_user_id")
    p_run.add_argument("--nickname", default=None)
    p_run.add_argument("--category", default=None)
    p_run.add_argument("--note", default=None)
    p_run.add_argument("--login-type", default="cookie", choices=["cookie", "qrcode"])
    p_run.add_argument("--max-videos", type=int, default=0, help="最多抓取多少条视频（0=全部）")
    p_run.add_argument("--stages", default=None, help="执行阶段，逗号分隔：crawl,transcribe,summarize（默认全部）")
    p_run.add_argument("--batch", type=int, default=5, help="每轮处理条数")
    p_run.add_argument("--transcribe-limit", type=int, default=0, help="转写上限（0=用默认20）")
    p_run.add_argument("--summarize-limit", type=int, default=0, help="摘要上限（0=用默认10）")
    p_run.add_argument("--asr-provider", default="", help="ASR provider: local_whisper / groq_whisper")
    p_run.set_defaults(func=lambda a: cmd_run(cfg, a))

    # run-all
    p_runall = sub.add_parser("run-all", help="跑所有博主")
    p_runall.add_argument("--login-type", default="cookie")
    p_runall.add_argument("--max-videos", type=int, default=0)
    p_runall.add_argument("--batch", type=int, default=5)
    p_runall.add_argument("--transcribe-limit", type=int, default=0)
    p_runall.add_argument("--summarize-limit", type=int, default=0)
    p_runall.set_defaults(func=lambda a: cmd_run_all(cfg, a))

    # crawl (只抓取)
    p_crawl = sub.add_parser("crawl", help="只抓取+导入")
    p_crawl.add_argument("--sec-user-id", required=True)
    p_crawl.add_argument("--login-type", default="cookie")
    p_crawl.add_argument("--max-videos", type=int, default=0)
    p_crawl.set_defaults(func=lambda a: cmd_crawl(cfg, a))

    # transcribe (只转写)
    p_tr = sub.add_parser("transcribe", help="只跑 ASR 转写")
    p_tr.add_argument("--batch", type=int, default=5)
    p_tr.add_argument("--limit", type=int, default=0, help="上限（0=默认20）")
    p_tr.set_defaults(func=lambda a: cmd_transcribe(cfg, a))

    # summarize (只摘要)
    p_sm = sub.add_parser("summarize", help="只跑 LLM 摘要")
    p_sm.add_argument("--batch", type=int, default=5)
    p_sm.add_argument("--limit", type=int, default=0, help="上限（0=默认10）")
    p_sm.add_argument("--yes", "-y", action="store_true", help="跳过确认")
    p_sm.set_defaults(func=lambda a: cmd_summarize(cfg, a))

    # status
    p_status = sub.add_parser("status", help="查看处理状态")
    p_status.add_argument("--sec-user-id", default=None)
    p_status.set_defaults(func=lambda a: cmd_status(cfg, a))

    # list-creators
    p_list = sub.add_parser("list-creators", help="列出博主")
    p_list.set_defaults(func=lambda a: cmd_list_creators(cfg, a))

    # edit-creator
    p_edit = sub.add_parser("edit-creator", help="编辑博主")
    p_edit.add_argument("--sec-user-id", required=True)
    p_edit.add_argument("--nickname", default=None)
    p_edit.add_argument("--category", default=None)
    p_edit.add_argument("--note", default=None)
    p_edit.add_argument("--enabled", type=int, default=None, choices=[0, 1])
    p_edit.set_defaults(func=lambda a: cmd_edit_creator(cfg, a))

    # reauth
    p_reauth = sub.add_parser("reauth", help="清除登录态")
    p_reauth.set_defaults(func=lambda a: cmd_reauth(cfg, a))

    # refresh-info
    p_refresh = sub.add_parser("refresh-info", help="刷新博主信息")
    p_refresh.add_argument("--sec-user-id", required=True)
    p_refresh.set_defaults(func=lambda a: cmd_refresh_creator_info(cfg, a))

    # reset
    p_reset = sub.add_parser("reset", help="重置视频状态")
    p_reset.add_argument("--aweme-id", required=True)
    p_reset.add_argument("--status", required=True)
    p_reset.set_defaults(func=lambda a: cmd_reset(cfg, a))

    p_tr1 = sub.add_parser("transcribe-one", help="单条视频转写")
    p_tr1.add_argument("--aweme-id", required=True)
    p_tr1.add_argument("--asr-provider", default="")
    p_tr1.set_defaults(func=lambda a: cmd_transcribe_one(cfg, a))

    p_sm1 = sub.add_parser("summarize-one", help="单条视频摘要")
    p_sm1.add_argument("--aweme-id", required=True)
    p_sm1.add_argument("--llm-provider", default="")
    p_sm1.set_defaults(func=lambda a: cmd_summarize_one(cfg, a))

    p_trb = sub.add_parser("transcribe-batch", help="批量转写指定视频")
    p_trb.add_argument("--aweme-ids", required=True, help="逗号分隔的 aweme_id")
    p_trb.add_argument("--asr-provider", default="")
    p_trb.set_defaults(func=lambda a: cmd_transcribe_batch(cfg, a))

    p_smb = sub.add_parser("summarize-batch", help="批量摘要指定视频")
    p_smb.add_argument("--aweme-ids", required=True, help="逗号分隔的 aweme_id")
    p_smb.set_defaults(func=lambda a: cmd_summarize_batch(cfg, a))

    p_clean = sub.add_parser("cleanup", help="清理临时文件")
    p_clean.add_argument("--remove-wav", action="store_true")
    p_clean.add_argument("--remove-mp4", action="store_true")
    p_clean.add_argument("--all", action="store_true")
    p_clean.add_argument("--done-only", action="store_true", help="只删已完成视频的 mp4")
    p_clean.add_argument("--stats", action="store_true", help="查看磁盘占用")
    p_clean.set_defaults(func=lambda a: cmd_cleanup(cfg, a))

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()