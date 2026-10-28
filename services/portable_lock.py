"""跨平台文件锁（POSIX flock / Windows msvcrt）。

背景：`services/local_storage.py` 与 `services/vector_search_service.py` 原先直接
`import fcntl`，在 Windows 上导入即失败（T19a 可移植性）。这里收敛成唯一入口：

- POSIX：`fcntl.flock`（shared / exclusive 都支持）
- Windows：`msvcrt.locking`（没有共享锁，`lock_shared` 退化为独占，保证互斥；
  会牺牲并发读性能，但不牺牲正确性）
- 其他平台：退化为单进程语义（记录 debug 日志，不声称跨进程）

注意：这不是 T04 的 `compare_and_mutate` 分布式锁，只是本地文件写的进程间互斥。
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

try:  # POSIX
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - Windows
    _fcntl = None

try:  # Windows
    import msvcrt as _msvcrt
except ImportError:  # pragma: no cover - POSIX
    _msvcrt = None


def _msvcrt_lock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_LOCK, 1)


def _msvcrt_unlock(fd: int) -> None:
    os.lseek(fd, 0, os.SEEK_SET)
    _msvcrt.locking(fd, _msvcrt.LK_UNLCK, 1)


def lock_shared(fd: int) -> None:
    """共享锁（读）；Windows 上退化为独占锁。"""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_SH)
    elif _msvcrt is not None:
        _msvcrt_lock(fd)
    else:  # pragma: no cover - 未知平台
        logger.debug("no cross-process lock backend; single-process only")


def lock_exclusive(fd: int) -> None:
    """独占锁（写）。"""
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_EX)
    elif _msvcrt is not None:
        _msvcrt_lock(fd)
    else:  # pragma: no cover - 未知平台
        logger.debug("no cross-process lock backend; single-process only")


def unlock(fd: int) -> None:
    if _fcntl is not None:
        _fcntl.flock(fd, _fcntl.LOCK_UN)
    elif _msvcrt is not None:
        _msvcrt_unlock(fd)
    else:  # pragma: no cover - 未知平台
        pass
