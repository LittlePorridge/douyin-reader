from src.db import requeue_failed, recover_stale, MAX_RETRY
from tests.conftest import insert_video, get_status


def test_transcribe_failed_requeued_until_retry_limit(cfg):
    insert_video(cfg, "1", status="transcribe_failed", retry_count=1)
    insert_video(cfg, "2", status="transcribe_failed", retry_count=MAX_RETRY)
    r = requeue_failed(cfg)
    assert get_status(cfg, "1") == "downloaded"
    assert get_status(cfg, "2") == "manual_failed"
    assert r["requeued_asr"] == 1 and r["manual_failed"] == 1


def test_summarize_failed_requeued_to_transcribed(cfg):
    insert_video(cfg, "3", status="summarize_failed", retry_count=2)
    requeue_failed(cfg)
    assert get_status(cfg, "3") == "transcribed"


def test_manual_failed_is_never_auto_retried(cfg):
    insert_video(cfg, "4", status="manual_failed", retry_count=5)
    requeue_failed(cfg)
    assert get_status(cfg, "4") == "manual_failed"


def test_recover_stale_intermediate_states(cfg):
    insert_video(cfg, "5", status="transcribing")
    insert_video(cfg, "6", status="summarizing")
    r = recover_stale(cfg)
    assert get_status(cfg, "5") == "downloaded"
    assert get_status(cfg, "6") == "transcribed"
    assert r == {"transcribing_reset": 1, "summarizing_reset": 1}


def test_downloaded_without_mp4_reverts_to_new_and_does_not_block_queue(cfg):
    from src.transcribe_worker import tick, transcribe_one
    # 两条 downloaded 但 mp4 不存在，一条正常有 mp4 的 new 之外的视频
    insert_video(cfg, "7", status="downloaded", create_time=3)
    insert_video(cfg, "8", status="downloaded", create_time=2)
    assert transcribe_one(cfg, "7") is False
    assert get_status(cfg, "7") == "new"
    # tick 不会因为跳过的记录而提前结束，也不会死循环
    r = tick(cfg, max_process=5)
    assert r.attempted == 0 and r.skipped == 2
