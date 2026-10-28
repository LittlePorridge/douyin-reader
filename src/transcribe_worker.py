from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .config import Config
from .db import connect, now_ts, TickResult
from .asr_client import load_asr_provider, load_asr_provider_from_dict, LocalWhisperProvider


def _claim(conn: sqlite3.Connection, aweme_id: str, from_status: str, to_status: str) -> bool:
    """原子地抢状态，防止并发重复处理"""
    cur = conn.execute(
        "UPDATE videos SET status=? WHERE aweme_id=? AND status=?",
        (to_status, aweme_id, from_status),
    )
    return cur.rowcount == 1


def _mark_failed(conn: sqlite3.Connection, aweme_id: str, msg: str, to_status: str) -> None:
    conn.execute(
        "UPDATE videos SET status=?, status_message=?, retry_count=retry_count+1 WHERE aweme_id=?",
        (to_status, msg[:500], aweme_id),
    )


def _load_whisper(model_size: str = "large-v3", device: str = "cpu",
                  compute_type: str = "int8"):
    from faster_whisper import WhisperModel
    cache_dir = str(Path.home() / ".cache" / "huggingface" / "hub")
    return WhisperModel(
        model_size_or_path=model_size,
        device=device,
        compute_type=compute_type,
        download_root=cache_dir,
    )


_whisper_model = None


def _get_whisper():
    global _whisper_model
    if _whisper_model is None:
        print("[transcribe] loading faster-whisper large-v3 (首次会下载模型 ~3G)")
        t0 = time.time()
        _whisper_model = _load_whisper()
        print(f"[transcribe] model loaded in {time.time()-t0:.1f}s")
    return _whisper_model


def transcribe_one(cfg: Config, aweme_id: str, audio_path: Path | None = None) -> bool:
    """处理单条：new → downloaded → transcribing → transcribed。
    返回 True 表示真正执行了转写；False 表示前置条件不满足被跳过。
    失败时抛出异常（状态已写为 transcribe_failed）。"""
    paths = cfg.paths_for(aweme_id)
    video_path = paths["video"]
    transcript_path = paths["transcript"]
    audio_path = audio_path or paths["audio"]

    with connect(cfg) as conn:
        row = conn.execute(
            "SELECT status FROM videos WHERE aweme_id=?", (aweme_id,)
        ).fetchone()
        if row is None:
            print(f"[transcribe] {aweme_id} not in db, skip")
            return False
        status = row["status"]

        # 如果是 new，先确认 mp4 落地后改 downloaded
        if status == "new":
            if not video_path.exists():
                print(f"[transcribe] {aweme_id} mp4 missing, skip (status stays new)")
                return False
            conn.execute(
                "UPDATE videos SET status='downloaded', video_path=?, downloaded_at=? WHERE aweme_id=?",
                (str(video_path.relative_to(cfg.project_root)), now_ts(), aweme_id),
            )
            status = "downloaded"

        if status != "downloaded":
            return False

        # downloaded 但 mp4 已不存在（被误删或清理）：回退到 new，等待下次抓取重新下载，
        # 不能让 ASR 去读一个不存在的文件后报出含糊的错误
        if not video_path.exists():
            conn.execute(
                "UPDATE videos SET status='new', status_message=? WHERE aweme_id=? AND status='downloaded'",
                ("mp4 缺失，等待重新下载", aweme_id),
            )
            print(f"[transcribe] {aweme_id} mp4 missing, reverted to new")
            return False

        # 抢占状态
        if not _claim(conn, aweme_id, "downloaded", "transcribing"):
            return False

    print(f"[transcribe] {aweme_id} start")
    print(f"[transcribe] ▶ NOW 正在处理: {aweme_id}")
    try:
        # 抽音频：优先 ffmpeg，失败则让 ASR 直接读 mp4
        audio_for_asr = audio_path
        if _try_extract_audio(video_path, audio_path):
            print(f"[transcribe] audio extracted to {audio_path.name}")
        else:
            print("[transcribe] ffmpeg unavailable, ASR reads mp4 directly")
            audio_for_asr = video_path

        # 加载 ASR provider
        asr_cfg = cfg.data_dir / "asr_config.yaml"
        # 支持环境变量覆盖
        override = os.environ.get("ASR_PROVIDER_OVERRIDE", "")
        if override:
            # 临时修改 yaml 的 active_provider
            if asr_cfg.exists():
                import yaml
                with asr_cfg.open("r") as f:
                    ycfg = yaml.safe_load(f)
                ycfg["active_provider"] = override
                provider = load_asr_provider_from_dict(ycfg)
            elif override == "local_whisper":
                provider = LocalWhisperProvider()
            else:
                raise RuntimeError(f"ASR provider {override} needs asr_config.yaml")
        elif asr_cfg.exists():
            provider = load_asr_provider(asr_cfg)
        else:
            provider = LocalWhisperProvider()
        print(f"[transcribe] using ASR provider: {provider.name}")

        t0 = time.time()
        transcript_text, audio_duration = provider.transcribe(audio_for_asr)
        duration = time.time() - t0
        print(f"[transcribe] {aweme_id} transcribed in {duration:.1f}s, audio_dur={audio_duration:.1f}s chars={len(transcript_text)}")

        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        transcript_path.write_text(transcript_text, encoding="utf-8")

        rel_audio = str(audio_for_asr.relative_to(cfg.project_root)) if audio_for_asr != video_path else None
        with connect(cfg) as conn:
            conn.execute(
                """UPDATE videos SET
                   status='transcribed',
                   audio_path=COALESCE(?, audio_path),
                   transcript_path=?,
                   transcribed_at=?,
                   video_duration=?,
                   transcribe_duration=?,
                   status_message=NULL
                   WHERE aweme_id=?""",
                (rel_audio, str(transcript_path.relative_to(cfg.project_root)),
                 now_ts(), audio_duration, duration, aweme_id),
            )
        print(f"[transcribe] {aweme_id} done -> transcribed")
        return True
    except Exception as e:
        print(f"[transcribe] {aweme_id} failed: {e!r}")
        with connect(cfg) as conn:
            _mark_failed(conn, aweme_id, str(e), "transcribe_failed")
        # 重新抛出：让上层编排器/子进程拿到非零退出码，
        # 否则后台任务追踪只看 returncode 会误报"成功"
        raise


def _try_extract_audio(video_path: Path, audio_out: Path) -> bool:
    """抽音频：优先 ffmpeg，失败用 PyAV"""
    import shutil
    import subprocess

    # 如果已经是 wav，直接返回
    if video_path.suffix.lower() in (".wav", ".mp3", ".m4a", ".flac"):
        return True

    audio_out.parent.mkdir(parents=True, exist_ok=True)

    # 方案1: ffmpeg
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg:
        try:
            result = subprocess.run(
                [ffmpeg, "-y", "-i", str(video_path),
                 "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
                 str(audio_out)],
                capture_output=True, timeout=600,
            )
            if result.returncode == 0 and audio_out.exists() and audio_out.stat().st_size > 1000:
                return True
        except Exception:
            pass

    # 方案2: PyAV 抽音频（无需 ffmpeg）
    try:
        import av
        container = av.open(str(video_path))
        audio_stream = next((s for s in container.streams if s.type == 'audio'), None)
        if audio_stream is None:
            return False
        resampler = av.AudioResampler(format='s16', layout='mono', rate=16000)
        with av.open(str(audio_out), 'w', format='wav') as out:
            out_stream = out.add_stream('pcm_s16le', rate=16000)
            out_stream.layout = 'mono'
            for frame in container.decode(audio_stream):
                for resampled in resampler.resample(frame):
                    for packet in out_stream.encode(resampled):
                        out.mux(packet)
            for packet in out_stream.encode(None):
                out.mux(packet)
        container.close()
        if audio_out.exists() and audio_out.stat().st_size > 1000:
            return True
    except Exception as e:
        print(f"[transcribe] PyAV extract failed: {e}")

    return False


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def tick(cfg: Config, max_process: int = 5) -> TickResult:
    """处理一批待转写视频。单条失败只记录，不中断本批次。"""
    res = TickResult()
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT aweme_id FROM videos WHERE status IN ('new', 'downloaded') "
            "ORDER BY create_time DESC",
        ).fetchall()
    for row in rows:
        # 跳过的条目不计入批次配额，避免 mp4 缺失的记录堵住后面的队列
        if res.attempted >= max_process:
            break
        try:
            if transcribe_one(cfg, row["aweme_id"]):
                res.ok += 1
            else:
                res.skipped += 1
        except Exception as e:
            res.failed += 1
            print(f"[transcribe] {row['aweme_id']} failed, continue batch: {e!r}")
    return res


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.transcribe_worker <aweme_id>")
        sys.exit(1)
    from .config import load_config
    cfg = load_config()
    transcribe_one(cfg, sys.argv[1])