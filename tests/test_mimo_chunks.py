"""MiMo 分片：分片中途失败必须判为失败，不能返回残缺文字稿。"""
from pathlib import Path

import av
import numpy as np
import pytest

from src import asr_client
from src.asr_client import MiMoASRProvider, _iter_audio_chunks


def _make_wav(path: Path, seconds: float, sr: int = 16000) -> None:
    t = np.arange(int(seconds * sr)) / sr
    samples = (0.2 * np.sin(2 * np.pi * 440 * t) * 32767).astype(np.int16)
    with av.open(str(path), "w", format="wav") as out:
        st = out.add_stream("pcm_s16le", rate=sr)
        st.layout = "mono"
        frame = av.AudioFrame.from_ndarray(samples.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = sr
        for p in st.encode(frame):
            out.mux(p)
        for p in st.encode(None):
            out.mux(p)


@pytest.fixture
def long_wav(tmp_path):
    p = tmp_path / "a.wav"
    _make_wav(p, 7.0)
    return p


def test_chunk_iter_yields_multiple_chunks(long_wav):
    chunks = list(_iter_audio_chunks(long_wav, 2.0))
    assert len(chunks) >= 3
    starts = [s for s, _ in chunks]
    assert starts == sorted(starts) and starts[0] == 0


def test_transcribe_raises_when_later_chunk_fails(long_wav, monkeypatch):
    monkeypatch.setattr(asr_client, "_probe_audio_seconds", lambda p: 30.0)
    calls = {"n": 0}

    def fake_api(path, *a, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("boom")
        return "你好。", 2.0

    monkeypatch.setattr(asr_client, "_call_mimo_api_one", fake_api)
    p = MiMoASRProvider(api_key="x", chunk_seconds=2.0)
    with pytest.raises(RuntimeError, match="chunk stream failed"):
        p.transcribe(long_wav)


def test_transcribe_full_success_joins_chunks(long_wav, monkeypatch):
    monkeypatch.setattr(asr_client, "_probe_audio_seconds", lambda p: 30.0)
    monkeypatch.setattr(asr_client, "_call_mimo_api_one", lambda path, *a, **k: ("句子。", 2.0))
    text, dur = MiMoASRProvider(api_key="x", chunk_seconds=2.0).transcribe(long_wav)
    lines = [ln for ln in text.splitlines() if ln.strip()]
    assert len(lines) >= 3
    assert all(ln.endswith("句子") for ln in lines)
