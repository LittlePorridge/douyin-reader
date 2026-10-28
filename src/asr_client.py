from __future__ import annotations

import json
import os
import time
import urllib.request
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class ASRProvider(Protocol):
    name: str

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        """转写音频文件，返回 (文字稿带时间戳, 音频时长秒)"""
        ...


_WHISPER_CACHE: dict[tuple[str, str, str], object] = {}


def _get_whisper_model(model_size: str, device: str, compute_type: str):
    """进程内缓存 WhisperModel：加载一次约数 GB 内存，逐条转写时复用。"""
    key = (model_size, device, compute_type)
    if key not in _WHISPER_CACHE:
        from faster_whisper import WhisperModel
        print(f"[asr] loading faster-whisper {model_size} ({device}, {compute_type})")
        _WHISPER_CACHE[key] = WhisperModel(model_size, device=device, compute_type=compute_type)
    return _WHISPER_CACHE[key]


@dataclass
class LocalWhisperProvider:
    name: str = "local_whisper"
    model_size: str = "large-v3"
    device: str = "cpu"
    compute_type: str = "int8"

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        model = _get_whisper_model(self.model_size, self.device, self.compute_type)
        segments, info = model.transcribe(
            str(audio_path), language="zh", vad_filter=True, beam_size=1,
        )
        seg_list = list(segments)
        lines = []
        for seg in seg_list:
            ts = f"[{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}]"
            lines.append(f"{ts} {seg.text.strip()}")
        return "\n".join(lines), info.duration


@dataclass
class GroqWhisperProvider:
    """Groq Whisper API — OpenAI 兼容，免费额度，极快"""
    name: str = "groq_whisper"
    api_key: str = ""
    model: str = "whisper-large-v3"
    api_base: str = "https://api.groq.com/openai/v1"

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        if not self.api_key:
            raise RuntimeError("GROQ_API_KEY not set")

        # Groq 限制文件 25MB，大文件需要截断或压缩
        file_size = audio_path.stat().st_size
        if file_size > 25 * 1024 * 1024:
            # 重新编码为低码率 mp3
            import subprocess
            import tempfile
            tmp = Path(tempfile.mktemp(suffix=".mp3"))
            subprocess.run([
                "ffmpeg", "-y", "-i", str(audio_path),
                "-vn", "-acodec", "libmp3lame", "-b:a", "32k", "-ar", "16000", "-ac", "1",
                str(tmp),
            ], capture_output=True, timeout=300)
            audio_path = tmp

        # multipart/form-data
        boundary = "----WebKitFormBoundary" + str(int(time.time()))
        with open(audio_path, "rb") as f:
            file_data = f.read()

        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="model"\r\n\r\n'
            f"{self.model}\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="response_format"\r\n\r\n'
            f"verbose_json\r\n"
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{audio_path.name}"\r\n'
            f"Content-Type: audio/wav\r\n\r\n"
        ).encode() + file_data + f"\r\n--{boundary}--\r\n".encode()

        url = f"{self.api_base}/audio/transcriptions"
        req = urllib.request.Request(
            url, data=body,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": f"multipart/form-data; boundary={boundary}",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            err = e.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Groq HTTP {e.code}: {err}") from None

        # verbose_json 格式有 segments 和 duration
        duration = result.get("duration", 0.0)
        segments = result.get("segments", [])
        if segments:
            lines = []
            for seg in segments:
                start = seg.get("start", 0)
                end = seg.get("end", 0)
                text = seg.get("text", "").strip()
                lines.append(f"[{_fmt_ts(start)} --> {_fmt_ts(end)}] {text}")
            return "\n".join(lines), duration
        # 没有 segments 就用纯文本
        return result.get("text", ""), duration


@dataclass
class DashscopeProvider:
    """阿里云 DashScope Paraformer — 需要先上传文件到 OSS"""
    name: str = "dashscope_paraformer"
    api_key: str = ""
    model: str = "paraformer-v2"
    api_base: str = "https://dashscope.aliyuncs.com/api/v1"

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        raise NotImplementedError(
            "DashScope Paraformer 需要文件 URL，暂未实现。"
            "请用 Groq Whisper（免费）或 MiMo ASR（¥0.5/小时）。"
        )


@dataclass
class MiMoASRProvider:
    """小米 MiMo-V2.5-ASR — 走 /v1/chat/completions，base64 传音频，¥0.5/小时"""
    name: str = "mimo_asr"
    api_key: str = ""
    model: str = "mimo-v2.5-asr"
    api_base: str = "https://api.xiaomimimo.com/v1"
    language: str = "zh"

    # 单段最长音频秒数。MiMo-V2.5-ASR context 上限 8192 tokens，
    # 2 分钟以内音频稳定不超限（5 分钟曾有 8368 tokens 超限记录），留余量取 120s。
    chunk_seconds: float = 120.0

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        if not self.api_key:
            raise RuntimeError("MIMO_API_KEY not set")

        # 探测音频时长。探测失败时直接走单段路径（保守、与旧行为一致）。
        total_seconds = _probe_audio_seconds(audio_path)
        if total_seconds is None or total_seconds <= self.chunk_seconds + 5:
            text, dur = _call_mimo_api_one(
                audio_path, self.api_key, self.model, self.api_base, self.language
            )
            stamp = f"[00:00:00.000 --> 00:{dur:06.1f}] {_strip_trailing_dot(text)}"
            return stamp + "\n", dur

        # 长音频：流式切片，每段写盘 → 上传 → 删除，盘上峰值只有 1 个 chunk
        print(f"[MiMo] audio {total_seconds:.1f}s > {self.chunk_seconds}s, streaming chunks")
        lines: list[str] = []
        total_dur = 0.0
        idx = 0
        try:
            for idx, (start_off, chunk_path) in enumerate(
                _iter_audio_chunks(audio_path, self.chunk_seconds), 1
            ):
                chunk_text, chunk_dur = _call_mimo_api_one(
                    chunk_path, self.api_key, self.model, self.api_base, self.language
                )
                end_s = start_off + chunk_dur
                lines.append(
                    f"[{_fmt_ts(start_off)} --> {_fmt_ts(end_s)}] {_strip_trailing_dot(chunk_text)}"
                )
                total_dur = end_s
                print(f"[MiMo] chunk {idx} done start={start_off:.1f}s dur={chunk_dur:.1f}s")
        except Exception as e:
            # 切片流式失败：若已有部分结果，封装为 partial 异常让上层记录失败原因；
            # 若一条都没拿到，退化为单段重试（音频时长本身就在 chunk_seconds+5 之上，
            # 单段几乎必超 token，但仍尝试一次以便短一些的边界音频能成功）
            if lines:
                raise RuntimeError(
                    f"MiMo chunk stream failed at chunk {idx + 1}: {e!r}; got {len(lines)} chunks"
                ) from e
            print(
                f"[MiMo] chunk stream failed before any chunk completed: {e!r}; "
                f"fallback single-segment"
            )
            text, dur = _call_mimo_api_one(
                audio_path, self.api_key, self.model, self.api_base, self.language
            )
            stamp = f"[00:00:00.000 --> 00:{dur:06.1f}] {_strip_trailing_dot(text)}"
            return stamp + "\n", dur

        if not lines:
            # 流式生成器没产出任何 chunk（例如无音频流），退化为单段
            text, dur = _call_mimo_api_one(
                audio_path, self.api_key, self.model, self.api_base, self.language
            )
            stamp = f"[00:00:00.000 --> 00:{dur:06.1f}] {_strip_trailing_dot(text)}"
            return stamp + "\n", dur

        return "\n".join(lines) + "\n", total_dur


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


def _strip_trailing_dot(text: str) -> str:
    """MiMo 会给句末加句号，统一去掉，保持与原 transcribe 行为一致"""
    if text.endswith("。"):
        return text[:-1]
    if text.endswith("."):
        return text[:-1]
    return text


def _probe_audio_seconds(audio_path: Path) -> float | None:
    """fast probe：用 av 容器 duration，拿不到则 None"""
    try:
        import av
        container = av.open(str(audio_path))
        dur = float(container.duration) / 1_000_000.0  # 微秒 → 秒
        container.close()
        if dur > 0:
            return dur
    except Exception as e:
        print(f"[MiMo] probe duration failed: {e}")
    return None


def _iter_audio_chunks(audio_path: Path, chunk_seconds: float, sample_rate: int = 16000):
    """流式生成器：每次 yield (起始秒, 临时 wav 路径)。
    每段 chunk 写盘 → yield 给调用方 → 调用方消费完后删除该文件 →
    生成器继续 decode 下一帧开新 chunk。盘上同时只存在 1 个 chunk 文件。
    生成器退出（正常/异常）时整体清理临时目录。
    切片用 PyAV 解码 + 重采样，无需 ffmpeg 二进制。"""
    import av
    import tempfile
    import shutil

    tmp_dir = Path(tempfile.mkdtemp(prefix="mimo_chunk_"))
    container = av.open(str(audio_path))
    try:
        astream = next((s for s in container.streams if s.type == "audio"), None)
        if astream is None:
            raise RuntimeError(f"no audio stream in {audio_path}")
        resampler = av.AudioResampler(format="s16", layout="mono", rate=sample_rate)

        out_container = None
        out_stream = None
        current_path: Path | None = None
        chunk_idx = 0
        # 已积累解码的音频秒数（重采样后）
        chunk_seen_seconds = 0.0
        # 当前 chunk 在原始音频里的起始秒
        chunk_start_seconds = 0.0
        # 当前 chunk 的目标结束秒
        next_chunk_end = chunk_seconds

        def open_chunk() -> Path:
            nonlocal out_container, out_stream, current_path
            current_path = tmp_dir / f"chunk_{chunk_idx:03d}.wav"
            oc = av.open(str(current_path), "w", format="wav")
            ost = oc.add_stream("pcm_s16le", rate=sample_rate)
            ost.layout = "mono"
            out_container = oc
            out_stream = ost
            return current_path

        def close_chunk():
            nonlocal out_container, out_stream
            if out_container is None:
                return
            for pkt in out_stream.encode(None):
                out_container.mux(pkt)
            out_container.close()
            out_container = None
            out_stream = None

        try:
            for frame in container.decode(astream):
                for r in resampler.resample(frame):
                    # 当前没有打开 chunk → 开新 chunk
                    if out_container is None:
                        open_chunk()
                    # 编码写入
                    for pkt in out_stream.encode(r):
                        out_container.mux(pkt)
                    chunk_seen_seconds += r.samples / float(sample_rate)
                    # 达到边界 → 收尾、yield、删除、开下一个边界
                    while chunk_seen_seconds >= next_chunk_end:
                        close_chunk()
                        if (
                            current_path is not None
                            and current_path.exists()
                            and current_path.stat().st_size > 1000
                        ):
                            yield chunk_start_seconds, current_path
                            # 调用方已消费，立即删除释放磁盘
                            try:
                                current_path.unlink()
                            except OSError:
                                pass
                        chunk_idx += 1
                        chunk_start_seconds = chunk_seen_seconds
                        next_chunk_end = chunk_start_seconds + chunk_seconds
                        break  # while 仅在 break 后由外层 for 触发新 chunk 开启
            # 收尾最后一段
            close_chunk()
            if (
                current_path is not None
                and current_path.exists()
                and current_path.stat().st_size > 1000
            ):
                yield chunk_start_seconds, current_path
                try:
                    current_path.unlink()
                except OSError:
                    pass
        finally:
            # 异常必须向上抛出：静默吞掉会让残缺的文字稿被当作转写成功写入。
            # 这里只负责释放未关闭的 chunk 容器，异常由调用方处理。
            if out_container is not None:
                try:
                    out_container.close()
                except Exception:
                    pass
    finally:
        try:
            container.close()
        except Exception:
            pass
        shutil.rmtree(tmp_dir, ignore_errors=True)


def _call_mimo_api_one(
    audio_path: Path,
    api_key: str,
    model: str,
    api_base: str,
    language: str,
) -> tuple[str, float]:
    """对单个音频文件调用 MiMo /chat/completions，返回 (识别文本, 时长秒)。
    内部已处理 >10MB 文件的 PyAV 压缩。"""
    actual_path = audio_path
    file_size = audio_path.stat().st_size
    if file_size > 8 * 1024 * 1024:
        try:
            import av
            import tempfile
            tmp = Path(tempfile.mktemp(suffix=".mp3"))
            container = av.open(str(audio_path))
            audio_stream = next((s for s in container.streams if s.type == "audio"), None)
            if audio_stream:
                resampler = av.AudioResampler(format="s16p", layout="mono", rate=16000)
                with av.open(str(tmp), "w", format="mp3") as out:
                    out_stream = out.add_stream("mp3", rate=16000)
                    out_stream.bit_rate = 32000
                    for frame in container.decode(audio_stream):
                        for resampled in resampler.resample(frame):
                            for packet in out_stream.encode(resampled):
                                out.mux(packet)
                    for packet in out_stream.encode(None):
                        out.mux(packet)
                container.close()
                if tmp.exists() and tmp.stat().st_size > 1000:
                    actual_path = tmp
                    print(f"[MiMo] compressed {file_size/1024/1024:.1f}MB -> "
                          f"{tmp.stat().st_size/1024/1024:.1f}MB")
        except Exception as e:
            print(f"[MiMo] PyAV compress failed: {e}, sending original")

    import base64
    with open(actual_path, "rb") as f:
        audio_bytes = f.read()

    suffix = audio_path.suffix.lower()
    mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac"}
    mime = mime_map.get(suffix, "audio/wav")
    b64 = base64.b64encode(audio_bytes).decode("ascii")

    body = {
        "model": model,
        "messages": [{
            "role": "user",
            "content": [{
                "type": "input_audio",
                "input_audio": {"data": f"data:{mime};base64,{b64}"}
            }]
        }],
        "asr_options": {"language": language},
    }

    url = f"{api_base}/chat/completions"
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "User-Agent": "douyin-reader/0.1",
            "Accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=600) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"MiMo HTTP {e.code}: {err}") from None

    text = payload["choices"][0]["message"]["content"]
    duration = float(payload.get("usage", {}).get("seconds", 60.0))
    return text, duration


def load_asr_provider(config_path: Path) -> ASRProvider:
    """从 asr_config.yaml 读 provider 配置"""
    import yaml
    with config_path.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    return load_asr_provider_from_dict(cfg)


def load_asr_provider_from_dict(cfg: dict) -> ASRProvider:
    active = cfg.get("active_provider", "local_whisper")
    providers = cfg.get("providers", {})

    if active == "local_whisper":
        p = providers.get("local_whisper", {})
        return LocalWhisperProvider(
            model_size=p.get("model_size", "large-v3"),
            device=p.get("device", "cpu"),
            compute_type=p.get("compute_type", "int8"),
        )

    if active == "groq_whisper":
        p = providers.get("groq_whisper", {})
        api_key = p.get("api_key") or os.environ.get(p.get("api_key_env", "GROQ_API_KEY"), "")
        return GroqWhisperProvider(
            api_key=api_key,
            model=p.get("model", "whisper-large-v3"),
            api_base=p.get("api_base", "https://api.groq.com/openai/v1"),
        )

    if active == "dashscope_paraformer":
        p = providers.get("dashscope_paraformer", {})
        api_key = p.get("api_key") or os.environ.get(p.get("api_key_env", "DASHSCOPE_API_KEY"), "")
        return DashscopeProvider(api_key=api_key, model=p.get("model", "paraformer-v2"))

    if active == "mimo_asr":
        p = providers.get("mimo_asr", {})
        api_key = p.get("api_key") or os.environ.get(p.get("api_key_env", "MIMO_API_KEY"), "")
        return MiMoASRProvider(
            api_key=api_key,
            model=p.get("model", "mimo-v2.5-asr"),
            api_base=p.get("api_base", "https://api.xiaomimimo.com/v1"),
            language=p.get("language", "zh"),
        )

    raise KeyError(f"unknown ASR provider: {active}")


if __name__ == "__main__":
    import sys
    from .config import load_config
    cfg = load_config()
    asr_cfg = cfg.data_dir / "asr_config.yaml"
    if not asr_cfg.exists():
        print(f"missing {asr_cfg}; using local_whisper")
        provider = LocalWhisperProvider()
    else:
        provider = load_asr_provider(asr_cfg)
    print(f"ASR provider: {provider.name}")
    if len(sys.argv) > 1:
        text, dur = provider.transcribe(Path(sys.argv[1]))
        print(f"duration: {dur:.1f}s")
        print(f"text (first 200): {text[:200]}")