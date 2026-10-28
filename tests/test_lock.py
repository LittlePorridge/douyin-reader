import fcntl

import pytest

from src.lock import LockBusy, lock_path, pipeline_lock


def test_lock_is_exclusive(cfg):
    with pipeline_lock(cfg):
        # 模拟另一个进程：另开一个 fd 尝试非阻塞加锁
        other = lock_path(cfg).open("a+")
        with pytest.raises(BlockingIOError):
            fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        other.close()
    # 释放后可重新获取
    with pipeline_lock(cfg):
        pass


def test_lock_busy_message(cfg):
    with pipeline_lock(cfg):
        with pytest.raises(LockBusy):
            with pipeline_lock(cfg):
                pass
