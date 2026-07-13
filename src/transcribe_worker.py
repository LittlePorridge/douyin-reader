from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

from .config import Config
from .db import connect, now_ts
from .asr_client import load_asr_provider, load_asr_provider_from_dict, ASRProvider, LocalWhisperProvider


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


def transcribe_one(cfg: Config, aweme_id: str, audio_path: Path | None = None) -> None:
    """处理单条：new → downloaded → transcribing → transcribed"""
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
            return
        status = row["status"]

        # 如果是 new，先确认 mp4 落地后改 downloaded
        if status == "new":
            if not video_path.exists():
                print(f"[transcribe] {aweme_id} mp4 missing, skip (status stays new)")
                return
            conn.execute(
                "UPDATE videos SET status='downloaded', video_path=?, downloaded_at=? WHERE aweme_id=?",
                (str(video_path.relative_to(cfg.project_root)), now_ts(), aweme_id),
            )
            status = "downloaded"

        if status != "downloaded":
            return

        # 抢占状态
        if not _claim(conn, aweme_id, "downloaded", "transcribing"):
            return

    print(f"[transcribe] {aweme_id} start")
    try:
        # 抽音频：优先 ffmpeg，失败则让 ASR 直接读 mp4
        audio_for_asr = audio_path
        if _try_extract_audio(video_path, audio_path):
            print(f"[transcribe] audio extracted to {audio_path.name}")
        else:
            print(f"[transcribe] ffmpeg unavailable, ASR reads mp4 directly")
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
    except Exception as e:
        print(f"[transcribe] {aweme_id} failed: {e!r}")
        with connect(cfg) as conn:
            _mark_failed(conn, aweme_id, str(e), "transcribe_failed")


def _try_extract_audio(video_path: Path, audio_out: Path) -> bool:
    """有 ffmpeg 则抽音频；无则返回 False 让 whisper 直接吃 mp4"""
    import shutil
    import subprocess
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        return False
    audio_out.parent.mkdir(parents=True, exist_ok=True)
    try:
        result = subprocess.run(
            [ffmpeg, "-y", "-i", str(video_path),
             "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
             str(audio_out)],
            capture_output=True, timeout=600,
        )
        if result.returncode != 0 or not audio_out.exists():
            return False
        return True
    except Exception as e:
        print(f"[transcribe] ffmpeg failed: {e}")
        return False


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def tick(cfg: Config, max_process: int = 5) -> int:
    """处理一批：先确 status=new 的有人 mp4 转 downloaded，再处理 downloaded 的"""
    processed = 0
    with connect(cfg) as conn:
        rows = conn.execute(
            "SELECT aweme_id FROM videos WHERE status='new' OR status='downloaded' ORDER BY create_time DESC LIMIT ?",
            (max_process * 2,),
        ).fetchall()
    for row in rows:
        if processed >= max_process:
            break
        transcribe_one(cfg, row["aweme_id"])
        processed += 1
    return processed


if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("usage: python -m src.transcribe_worker <aweme_id>")
        sys.exit(1)
    from .config import load_config
    cfg = load_config()
    transcribe_one(cfg, sys.argv[1])