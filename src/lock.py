"""流水线互斥锁：同一时间只允许一个会修改视频状态的 orchestrator 进程运行。

用 fcntl.flock（非阻塞）实现：进程退出或被杀时内核自动释放，不会留下陈旧锁。
拿到锁后才能安全地回退遗留的中间态（transcribing/summarizing）。
"""
from __future__ import annotations

import fcntl
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import Config


class LockBusy(RuntimeError):
    pass


def lock_path(cfg: Config) -> Path:
    return cfg.data_dir / ".pipeline.lock"


@contextmanager
def pipeline_lock(cfg: Config) -> Iterator[None]:
    path = lock_path(cfg)
    path.parent.mkdir(parents=True, exist_ok=True)
    f = path.open("a+")
    try:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            f.seek(0)
            holder = f.read().strip() or "unknown"
            raise LockBusy(f"另一个流水线任务正在运行（pid={holder}），请稍后再试") from None
        f.seek(0)
        f.truncate()
        f.write(str(os.getpid()))
        f.flush()
        try:
            yield
        finally:
            f.seek(0)
            f.truncate()
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    finally:
        f.close()
