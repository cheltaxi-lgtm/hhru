"""Advisory inter-process lock for commands that can mutate hh.ru."""

from __future__ import annotations

import contextlib
import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

if os.name == "nt":
    import msvcrt
else:
    import fcntl


class WriteLockBusy(RuntimeError):
    """Another hhru-bot write command is already running."""

    def __init__(self, owner: dict | None = None):
        self.owner = owner or {}
        super().__init__("another write command is already running")


def _lock_exclusive_nb(lock_file) -> None:
    if os.name == "nt":
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() < 1:
            lock_file.write(" ")
            lock_file.flush()
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(lock_file) -> None:
    if os.name == "nt":
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        return
    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextlib.contextmanager
def acquire_write_lock(path: Path, *, command: str = "unknown") -> Iterator[None]:
    """Hold an exclusive, non-blocking lock until the command finishes."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+") as lock_file:
        try:
            _lock_exclusive_nb(lock_file)
        except OSError as exc:
            lock_file.seek(0)
            try:
                owner = json.loads(lock_file.read() or "{}")
            except (json.JSONDecodeError, OSError):
                owner = {}
            raise WriteLockBusy(owner) from exc
        try:
            owner = {
                "pid": os.getpid(),
                "command": command,
                "started_at": datetime.now(UTC).isoformat(),
            }
            lock_file.seek(0)
            lock_file.truncate()
            json.dump(owner, lock_file, sort_keys=True)
            lock_file.flush()
            os.fsync(lock_file.fileno())
            yield
        finally:
            _unlock(lock_file)
