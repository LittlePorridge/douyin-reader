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


@dataclass
class LocalWhisperProvider:
    name: str = "local_whisper"
    model_size: str = "large-v3"
    device: str = "cpu"
    compute_type: str = "int8"

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        from faster_whisper import WhisperModel
        model = WhisperModel(
            self.model_size, device=self.device, compute_type=self.compute_type,
        )
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
            import subprocess, tempfile
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

    def transcribe(self, audio_path: Path) -> tuple[str, float]:
        if not self.api_key:
            raise RuntimeError("MIMO_API_KEY not set")

        # 大文件用 PyAV 转低码率 mp3（MiMo 限制 base64 10MB）
        actual_path = audio_path
        file_size = audio_path.stat().st_size
        if file_size > 8 * 1024 * 1024:
            try:
                import av
                import tempfile
                tmp = Path(tempfile.mktemp(suffix=".mp3"))
                container = av.open(str(audio_path))
                audio_stream = next((s for s in container.streams if s.type == 'audio'), None)
                if audio_stream:
                    resampler = av.AudioResampler(format='s16p', layout='mono', rate=16000)
                    with av.open(str(tmp), 'w', format='mp3') as out:
                        out_stream = out.add_stream('mp3', rate=16000)
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
                        print(f"[MiMo] compressed {file_size/1024/1024:.1f}MB -> {tmp.stat().st_size/1024/1024:.1f}MB")
            except Exception as e:
                print(f"[MiMo] PyAV compress failed: {e}, sending original")

        import base64
        with open(actual_path, "rb") as f:
            audio_bytes = f.read()

        # 判断 MIME type
        suffix = audio_path.suffix.lower()
        mime_map = {".wav": "audio/wav", ".mp3": "audio/mpeg", ".m4a": "audio/mp4",
                    ".mp4": "audio/mp4", ".ogg": "audio/ogg", ".flac": "audio/flac"}
        mime = mime_map.get(suffix, "audio/wav")
        b64 = base64.b64encode(audio_bytes).decode("ascii")

        body = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [{
                    "type": "input_audio",
                    "input_audio": {
                        "data": f"data:{mime};base64,{b64}"
                    }
                }]
            }],
            "asr_options": {"language": self.language},
        }

        url = f"{self.api_base}/chat/completions"
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.api_key}",
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
        # MiMo 返回纯文本（不带时间戳），加个占位时间戳
        duration = payload.get("usage", {}).get("seconds", 60.0)
        lines = [f"[00:00:00.000 --> 00:{duration:06.1f}] {text[:-1] if text.endswith('.') else text}"]
        return "\n".join(lines), duration


def _fmt_ts(seconds: float) -> str:
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:06.3f}"


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