"""分段摘要：切分不丢内容、短文本单次调用、长文本逐段+汇总、失败重试复用分段缓存。"""
import json

import pytest

from src import summarize_worker as sw
from src.db import connect
from tests.conftest import insert_video, get_status


GOOD_JSON = json.dumps({
    "summary": "总结",
    "article": "正文",
    "key_points": ["要点一", "要点二"],
    "knowledge_points": [{"topic": "t", "why": "w", "direction": "d"}],
}, ensure_ascii=False)


class FakeProvider:
    name = "fake"
    model = "fake-model"
    temperature = 0.3

    def __init__(self, fail_final_times: int = 0):
        self.calls: list[list[dict]] = []
        self.fail_final_times = fail_final_times

    def chat(self, messages):
        self.calls.append(messages)
        if messages[0]["content"] == sw.SEGMENT_SYSTEM_PROMPT:
            return f"笔记({messages[1]['content'][-6:]})"
        if self.fail_final_times > 0:
            self.fail_final_times -= 1
            raise RuntimeError("final call down")
        return GOOD_JSON

    def segment_calls(self):
        return [c for c in self.calls if c[0]["content"] == sw.SEGMENT_SYSTEM_PROMPT]

    def final_calls(self):
        return [c for c in self.calls if c[0]["content"] == sw.SYSTEM_PROMPT]


def _write_transcript(cfg, aweme_id, text):
    p = cfg.paths_for(aweme_id)["transcript"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _long_transcript(lines: int = 3000) -> str:
    return "\n".join(f"[00:00:{i % 60:02d}.000 --> 00:00:{i % 60:02d}.500] 第{i}句话内容很长很长" for i in range(lines))


def test_split_keeps_all_content_and_respects_limit():
    text = "\n".join(f"line-{i:04d}" for i in range(2000))
    chunks = sw.split_transcript(text, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "\n".join(chunks).splitlines() == text.splitlines()


def test_split_hard_cuts_very_long_line():
    chunks = sw.split_transcript("x" * 1000, max_chars=300)
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks) == "x" * 1000


def test_short_transcript_is_single_call(cfg):
    aweme = "900001"
    insert_video(cfg, aweme, status="transcribed")
    _write_transcript(cfg, aweme, "短文本" * 100)
    fp = FakeProvider()
    assert sw.summarize_one(cfg, aweme, fp) is True
    assert len(fp.calls) == 1
    assert get_status(cfg, aweme) == "done"


def test_long_transcript_segments_then_final(cfg):
    aweme = "900002"
    insert_video(cfg, aweme, status="transcribed")
    text = _long_transcript()
    assert len(text) > sw.SEGMENT_THRESHOLD
    _write_transcript(cfg, aweme, text)
    fp = FakeProvider()
    assert sw.summarize_one(cfg, aweme, fp) is True
    n_seg = len(sw.split_transcript(text, sw.SEGMENT_CHARS))
    assert len(fp.segment_calls()) == n_seg
    assert len(fp.final_calls()) == 1
    final_user = fp.final_calls()[0][1]["content"]
    assert f"【第 {n_seg} 段】" in final_user          # 最后一段的笔记进入了最终输入
    assert "第2999句话" not in final_user               # 原文没有被整篇塞进最终 prompt
    # 成功后分段缓存被清理
    assert not sw._partial_path(cfg, aweme, "fake").exists()


def test_retry_reuses_cached_segments(cfg):
    aweme = "900003"
    insert_video(cfg, aweme, status="transcribed")
    _write_transcript(cfg, aweme, _long_transcript())
    fp = FakeProvider(fail_final_times=1)
    with pytest.raises(RuntimeError):
        sw.summarize_one(cfg, aweme, fp)
    first_seg = len(fp.segment_calls())
    assert first_seg > 0
    assert get_status(cfg, aweme) == "summarize_failed"

    # 第二次：状态回到 transcribed（模拟 requeue），分段全部命中缓存，只需再调一次汇总
    with connect(cfg) as conn:
        conn.execute("UPDATE videos SET status='transcribed' WHERE aweme_id=?", (aweme,))
    fp2 = FakeProvider()
    assert sw.summarize_one(cfg, aweme, fp2) is True
    assert len(fp2.segment_calls()) == 0
    assert len(fp2.final_calls()) == 1
    assert get_status(cfg, aweme) == "done"


def test_text_change_invalidates_segment_cache(cfg):
    aweme = "900004"
    insert_video(cfg, aweme, status="transcribed")
    _write_transcript(cfg, aweme, _long_transcript(2000))
    fp = FakeProvider(fail_final_times=1)
    with pytest.raises(RuntimeError):
        sw.summarize_one(cfg, aweme, fp)
    # 文字稿改动后，旧缓存的段哈希对不上，必须重新调用
    _write_transcript(cfg, aweme, _long_transcript(2000).replace("很长", "更长"))
    with connect(cfg) as conn:
        conn.execute("UPDATE videos SET status='transcribed' WHERE aweme_id=?", (aweme,))
    fp2 = FakeProvider()
    sw.summarize_one(cfg, aweme, fp2)
    assert len(fp2.segment_calls()) == len(sw.split_transcript(_long_transcript(2000).replace("很长", "更长"), sw.SEGMENT_CHARS))
