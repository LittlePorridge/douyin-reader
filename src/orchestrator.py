from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
import time

from .config import Config, load_config
from .db import (connect, init_db, add_creator, get_creator, list_creators, update_creator, now_ts,
                 TickResult, requeue_failed, recover_stale)
from .importer import import_since, jsonl_offsets, ImportResult
from .transcribe_worker import tick as transcribe_tick
from .summarize_worker import tick as summarize_tick
from .llm_client import load_provider_from_yaml
from .lock import LockBusy, pipeline_lock


class CrawlError(RuntimeError):
    """MediaCrawler 抓取失败：此时不导入任何数据，避免导入旧的/别人的记录"""


SEC_USER_ID_PATTERN = re.compile(r"MS4wLjAB[A-Za-z0-9_\-]+")

# LLM 单次跑摘要的批次上限（避免一次烧太多钱）
LLM_BATCH_LIMIT = 10
# ASR 单次跑转写的批次上限（避免 CPU 长时间占满）
ASR_BATCH_LIMIT = 20


def _extract_sec_user_id(url: str) -> str | None:
    m = SEC_USER_ID_PATTERN.search(url)
    return m.group(0) if m else None


def _run_mediacrawler(cfg: Config, sec_user_id: str, login_type: str = "cookie",
                      max_videos: int = 0) -> bool:
    """调 MediaCrawler 子进程（经 scripts/mediacrawler_launcher.py 启动）。
    不改写 MediaCrawler/ 下的任何文件；max_videos=0 表示全量。"""
    launcher = cfg.project_root / "scripts" / "mediacrawler_launcher.py"
    cmd = ["uv", "run", "python", str(launcher),
           "--sec-user-id", sec_user_id, "--login-type", login_type,
           "--max-videos", str(max_videos)]
    print(f"[orchestrator] running MediaCrawler for {sec_user_id[:12]}... (max_videos={max_videos or 'unlimited'})")
    t0 = time.time()
    r = subprocess.run(cmd, cwd=str(cfg.mediacrawler_dir))
    print(f"[orchestrator] MediaCrawler exit={r.returncode} dt={time.time() - t0:.1f}s")
    return r.returncode == 0


def _fetch_creator_info(cfg: Config, sec_user_id: str) -> dict | None:
    # 脚本保留在本项目 scripts/ 下，以 cwd=MediaCrawler 运行（它依赖 MediaCrawler 的相对路径与模块），
    # 不再把脚本复制进 MediaCrawler/，遵守“不修改 MediaCrawler 目录”的约束
    script = cfg.project_root / "scripts" / "fetch_creator_info.py"
    if not script.exists():
        return None
    cmd = ["uv", "run", "python", str(script), sec_user_id]
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


def _do_crawl(cfg: Config, sec_user_id: str, login_type: str, max_videos: int) -> ImportResult:
    """阶段 1：抓取 + 导入（只导入本次抓取新增的行）+ 获取博主信息。
    抓取失败抛出 CrawlError，且不导入任何数据。"""
    offsets = jsonl_offsets(cfg)
    ok = _run_mediacrawler(cfg, sec_user_id, login_type=login_type, max_videos=max_videos)
    if not ok:
        raise CrawlError(f"MediaCrawler 抓取失败 sec_user_id={sec_user_id[:12]}...，已跳过导入")
    r = import_since(cfg, sec_user_id, offsets)
    print(f"  import: new={r.new_count} existing={r.existing_count}")
    info = _fetch_creator_info(cfg, sec_user_id)
    if info and "error" not in info:
        _update_creator_info(cfg, sec_user_id, info)
        print(f"  creator info: {info.get('nickname')}")
    return r


def _log_requeue(cfg: Config) -> None:
    changed = requeue_failed(cfg)
    if any(changed.values()):
        print(f"[retry] {changed}")


def _do_transcribe(cfg: Config, batch: int, limit: int = 0, asr_provider: str = "") -> TickResult:
    """阶段 2：ASR 转写。单条失败不中断批次，返回汇总结果"""
    if asr_provider:
        os.environ["ASR_PROVIDER_OVERRIDE"] = asr_provider
    _log_requeue(cfg)
    _promote_new_to_downloaded(cfg)
    total = TickResult()
    effective_limit = limit if limit > 0 else ASR_BATCH_LIMIT
    while True:
        remaining = effective_limit - total.attempted
        if remaining <= 0:
            print(f"[transcribe] reached batch limit ({effective_limit}), stopping")
            break
        r = transcribe_tick(cfg, max_process=min(batch, remaining))
        if r.attempted == 0:
            break
        total.ok += r.ok
        total.failed += r.failed
        total.skipped += r.skipped
        print(f"[transcribe] ok={total.ok} failed={total.failed} skipped={total.skipped}")
    return total


def _do_summarize(cfg: Config, batch: int, limit: int = 0) -> TickResult:
    """阶段 3：LLM 摘要。单条失败不中断批次，返回汇总结果"""
    _log_requeue(cfg)
    provider = load_provider_from_yaml(cfg.llm_providers_path)
    total = TickResult()
    effective_limit = limit if limit > 0 else LLM_BATCH_LIMIT
    while True:
        remaining = effective_limit - total.attempted
        if remaining <= 0:
            print(f"[summarize] reached batch limit ({effective_limit}), stopping")
            break
        r = summarize_tick(cfg, provider, max_process=min(batch, remaining))
        if r.attempted == 0:
            break
        total.ok += r.ok
        total.failed += r.failed
        total.skipped += r.skipped
        print(f"[summarize] ok={total.ok} failed={total.failed} skipped={total.skipped}")
    return total


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


def _report_and_exit(failed: int, extra: str = "") -> None:
    """统一退出码：有失败则非零，供 Web 后台任务 banner 正确报红"""
    if failed:
        print(f"\n=== finished with {failed} failure(s) {extra}===")
        sys.exit(1)


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
    failed = 0
    total_transcribed = 0
    total_summarized = 0

    if "crawl" in stages:
        print(f"=== [crawl] max_videos={max_videos or 'all'} ===")
        try:
            _do_crawl(cfg, sec, args.login_type, max_videos)
        except CrawlError as e:
            # 抓取失败不影响处理已入库但未完成的视频
            print(f"[crawl] {e}")
            failed += 1

    if "transcribe" in stages:
        pending = _get_pending_counts(cfg, sec)
        print(f"=== [transcribe] pending: {pending['downloaded']} downloaded, {pending['failed']} failed ===")
        if pending["downloaded"] + pending["failed"] == 0:
            print("  nothing to transcribe")
        else:
            r = _do_transcribe(cfg, args.batch, limit=args.transcribe_limit, asr_provider=args.asr_provider)
            total_transcribed = r.ok
            failed += r.failed

    if "summarize" in stages:
        pending = _get_pending_counts(cfg, sec)
        print(f"=== [summarize] pending: {pending['transcribed']} transcribed ===")
        if pending["transcribed"] == 0:
            print("  nothing to summarize")
        else:
            r = _do_summarize(cfg, args.batch, limit=args.summarize_limit)
            total_summarized = r.ok
            failed += r.failed

    print(f"\n=== done: transcribed={total_transcribed} summarized={total_summarized} failed={failed} ===")
    with connect(cfg) as conn:
        stats = conn.execute(
            "SELECT status, count(*) as n FROM videos WHERE sec_user_id=? GROUP BY status", (sec,)
        ).fetchall()
        for s in stats:
            print(f"  {s['status']}: {s['n']}")
    _report_and_exit(failed)


def cmd_run_all(cfg: Config, args: argparse.Namespace) -> None:
    init_db(cfg)
    creators = list_creators(cfg, only_enabled=True)
    if not creators:
        print("no enabled creators. add one: python -m src.orchestrator add-creator --url ...")
        return
    print(f"found {len(creators)} enabled creators")
    failed = 0
    for i, c in enumerate(creators, 1):
        sec = c["sec_user_id"]
        print(f"\n=== [{i}/{len(creators)}] {c['nickname'] or sec[:20]} ===")
        try:
            _do_crawl(cfg, sec, args.login_type, args.max_videos or 0)
        except CrawlError as e:
            # 一个博主抓取失败不影响其他博主
            print(f"[crawl] {e}")
            failed += 1
        r = _do_transcribe(cfg, args.batch, limit=args.transcribe_limit)
        failed += r.failed
        r = _do_summarize(cfg, args.batch, limit=args.summarize_limit)
        failed += r.failed
    print(f"\n=== all {len(creators)} creators done ===")
    _report_and_exit(failed)


def cmd_transcribe(cfg: Config, args: argparse.Namespace) -> None:
    """只跑 ASR"""
    init_db(cfg)
    pending = _get_pending_counts(cfg)
    print(f"pending: {pending['downloaded']} downloaded, {pending['failed']} failed")
    r = _do_transcribe(cfg, args.batch, limit=args.limit)
    print(f"transcribed {r.ok} videos, failed {r.failed}")
    _report_and_exit(r.failed)


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
        if not input("continue? (y/n) ").strip().lower().startswith("y"):
            print("aborted")
            return
    r = _do_summarize(cfg, args.batch, limit=args.limit)
    print(f"summarized {r.ok} videos, failed {r.failed}")
    _report_and_exit(r.failed)


def cmd_crawl(cfg: Config, args: argparse.Namespace) -> None:
    init_db(cfg)
    try:
        _do_crawl(cfg, args.sec_user_id, args.login_type, args.max_videos or 0)
    except CrawlError as e:
        print(f"[crawl] {e}")
        _report_and_exit(1)


def cmd_recover(cfg: Config, args: argparse.Namespace) -> None:
    """回退遗留的中间态（transcribing/summarizing）。
    仅在确认没有任何转写/摘要进程在运行时使用，否则会误伤正在处理的视频。"""
    init_db(cfg)
    if not args.yes:
        print("将把 transcribing→downloaded、summarizing→transcribed。请确认没有后台任务在运行，加 --yes 执行。")
        return
    print(recover_stale(cfg))
    print(requeue_failed(cfg))


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
        print("cleared login state")
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
    ids = [x.strip() for x in args.aweme_ids.split(",") if x.strip()]
    failed = 0
    for i, aid in enumerate(ids):
        print(f"=== [{i+1}/{len(ids)}] {aid} ===")
        with connect(cfg) as conn:
            conn.execute(
                "UPDATE videos SET status='downloaded', status_message=NULL, retry_count=0 WHERE aweme_id=?",
                (aid,),
            )
        try:
            transcribe_one(cfg, aid)
        except Exception as e:
            failed += 1
            print(f"[transcribe-batch] {aid} failed, continue: {e!r}")
    print(f"batch transcribe done: {len(ids)} videos, failed {failed}")
    _report_and_exit(failed)


def cmd_summarize_batch(cfg: Config, args: argparse.Namespace) -> None:
    """批量摘要指定视频"""
    init_db(cfg)
    from .summarize_worker import summarize_one
    from .llm_client import load_provider_from_yaml
    if args.llm_provider:
        import yaml
        with cfg.llm_providers_path.open("r") as f:
            ycfg = yaml.safe_load(f)
        ycfg["active_provider"] = args.llm_provider
        with cfg.llm_providers_path.open("w") as f:
            yaml.safe_dump(ycfg, f, allow_unicode=True)
    provider = load_provider_from_yaml(cfg.llm_providers_path)
    ids = [x.strip() for x in args.aweme_ids.split(",") if x.strip()]
    failed = 0
    for i, aid in enumerate(ids):
        print(f"=== [{i+1}/{len(ids)}] {aid} ===")
        with connect(cfg) as conn:
            conn.execute(
                "UPDATE videos SET status='transcribed', status_message=NULL WHERE aweme_id=?",
                (aid,),
            )
        try:
            summarize_one(cfg, aid, provider)
        except Exception as e:
            failed += 1
            print(f"[summarize-batch] {aid} failed, continue: {e!r}")
    print(f"batch summarize done: {len(ids)} videos, failed {failed}")
    _report_and_exit(failed)


def cmd_cleanup(cfg: Config, args: argparse.Namespace) -> None:
    """清理临时文件"""
    import shutil as _shutil
    deleted_wav = 0
    deleted_mp4 = 0
    total_bytes = 0

    if args.remove_wav or args.all:
        wavs = list(cfg.audio_dir.glob("*.wav"))
        total_wav = len(wavs)
        for i, f in enumerate(wavs, 1):
            sz = f.stat().st_size
            f.unlink()
            deleted_wav += 1
            total_bytes += sz
            print(f"[cleanup] wav {i}/{total_wav} deleted ({sz/1024/1024:.1f}MB)")
        print(f"[cleanup] deleted {deleted_wav} wav files")

    if args.remove_mp4 or args.all:
        with connect(cfg) as conn:
            if args.done_only:
                rows = conn.execute(
                    "SELECT aweme_id FROM videos WHERE status='done'"
                ).fetchall()
                print(f"[cleanup] found {len(rows)} done videos to clean")
            else:
                rows = conn.execute("SELECT aweme_id FROM videos").fetchall()
        total_mp4 = len(rows)
        for i, row in enumerate(rows, 1):
            vdir = cfg.videos_dir / row["aweme_id"]
            if vdir.exists():
                dir_bytes = 0
                for f in vdir.rglob("*"):
                    if f.is_file():
                        dir_bytes += f.stat().st_size
                        deleted_mp4 += 1
                total_bytes += dir_bytes
                _shutil.rmtree(str(vdir), ignore_errors=True)
                print(f"[cleanup] mp4 {i}/{total_mp4} {row['aweme_id']} deleted ({dir_bytes/1024/1024:.1f}MB)")
            else:
                print(f"[cleanup] mp4 {i}/{total_mp4} {row['aweme_id']} already gone")
        print(f"[cleanup] deleted {deleted_mp4} mp4 files")

    if args.stats:
        total, used, free = _shutil.disk_usage(str(cfg.project_root))
        print(f"[cleanup] 磁盘: 总 {total/1024**3:.1f}GB / 已用 {used/1024**3:.1f}GB / 剩余 {free/1024**3:.1f}GB")
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
        print(f"[cleanup] mp4: {_dir_size(cfg.videos_dir)/1024**2:.1f}MB")
        print(f"[cleanup] wav: {_dir_size(cfg.audio_dir)/1024**2:.1f}MB")
        print(f"[cleanup] text: {_dir_size(cfg.text_dir)/1024**2:.2f}MB")
        print(f"[cleanup] summaries: {_dir_size(cfg.summary_dir)/1024**2:.2f}MB")

    mb = total_bytes / 1024**2
    print(f"[cleanup] 共清理 {mb:.1f}MB")
    print(f"[cleanup] ✅ 完成! 删除 wav={deleted_wav} mp4={deleted_mp4} 释放 {mb:.1f}MB")


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


# 会修改视频状态或文件的命令：必须在流水线锁内执行
LOCKED_COMMANDS = {
    "run", "run-all", "crawl", "transcribe", "summarize", "recover", "reset",
    "transcribe-one", "summarize-one", "transcribe-batch", "summarize-batch", "cleanup",
}


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

    # recover
    p_rec = sub.add_parser("recover", help="回退遗留的 transcribing/summarizing 中间态（无任务运行时使用）")
    p_rec.add_argument("--yes", action="store_true", help="确认执行（流水线命令会在持锁后自动执行回退）")
    p_rec.set_defaults(func=lambda a: cmd_recover(cfg, a))

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
    p_smb.add_argument("--llm-provider", default="")
    p_smb.set_defaults(func=lambda a: cmd_summarize_batch(cfg, a))

    p_clean = sub.add_parser("cleanup", help="清理临时文件")
    p_clean.add_argument("--remove-wav", action="store_true")
    p_clean.add_argument("--remove-mp4", action="store_true")
    p_clean.add_argument("--all", action="store_true")
    p_clean.add_argument("--done-only", action="store_true", help="只删已完成视频的 mp4")
    p_clean.add_argument("--stats", action="store_true", help="查看磁盘占用")
    p_clean.set_defaults(func=lambda a: cmd_cleanup(cfg, a))

    args = parser.parse_args()
    if args.cmd not in LOCKED_COMMANDS:
        args.func(args)
        return
    try:
        with pipeline_lock(cfg):
            # 持锁期间不可能有其他流水线进程，遗留的中间态一定是崩溃残留，可安全回退
            init_db(cfg)
            if args.cmd != "recover":
                changed = recover_stale(cfg)
                if any(changed.values()):
                    print(f"[recover] 回退遗留中间态: {changed}")
            args.func(args)
    except LockBusy as e:
        print(e)
        sys.exit(3)


if __name__ == "__main__":
    main()