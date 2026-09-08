"""Small standard-library exclusive lock shared by Codex and Claude hooks."""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Iterator, TextIO


WINDOWS_LOCK_TIMEOUT_SECONDS = 10.0
WINDOWS_LOCK_RETRY_SECONDS = 0.05


def _acquire_windows_lock(handle: TextIO) -> None:
    import msvcrt

    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write("0")
        handle.flush()
        os.fsync(handle.fileno())
    handle.seek(0)

    deadline = time.monotonic() + WINDOWS_LOCK_TIMEOUT_SECONDS
    while True:
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            return
        except OSError as error:
            if time.monotonic() >= deadline:
                raise TimeoutError(f"timed out acquiring lock: {handle.name}") from error
            time.sleep(WINDOWS_LOCK_RETRY_SECONDS)


def _release_windows_lock(handle: TextIO) -> None:
    import msvcrt

    handle.seek(0)
    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


@contextmanager
def exclusive_file_lock(path: Path) -> Iterator[None]:
    """Hold a cross-platform advisory lock while callers reread and replace state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        if os.name == "nt":
            _acquire_windows_lock(handle)
            try:
                yield
            finally:
                _release_windows_lock(handle)
            return

        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
