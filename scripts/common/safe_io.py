"""Atomic file writes + fcntl.flock helpers for v0.5+ state files.

All state files (intent.md, mechanical.json, autopilot-state.json,
nudge-state.json, hint files, history.jsonl) MUST go through these helpers.
Ad-hoc `open(path, 'w').write(...)` is banned in flow code paths that
write state observable across processes.
"""
from __future__ import annotations

import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write content to path atomically. Either old content or new content
    is observable; never a partial file. Uses POSIX rename semantics.

    Caller's responsibility: parent dir must exist.
    """
    path = Path(path)
    parent = path.parent
    # Temp file in same dir to guarantee same filesystem (rename is atomic
    # only within a filesystem boundary).
    tmp_fd, tmp_path = _mkstemp_in(parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as f:
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp_path, mode)
        os.replace(tmp_path, path)  # POSIX atomic rename
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, obj: Any, indent: int = 2) -> None:
    """Atomic JSON write with stable indent + trailing newline."""
    text = json.dumps(obj, ensure_ascii=False, indent=indent) + "\n"
    atomic_write_text(path, text)


def append_jsonl_locked(path: Path, record: dict, timeout_s: float = 2.0) -> bool:
    """Append one JSON record as a single line, holding fcntl.flock LOCK_EX.

    Returns True on success, False if the lock could not be acquired within
    timeout_s. Caller should treat False as "audit gap, log to stderr,
    proceed". File is created if missing.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout_s
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(path, "a", encoding="utf-8") as f:
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    return False
                time.sleep(0.05)
        try:
            f.write(line)
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return True


def _mkstemp_in(dir_: Path, prefix: str, suffix: str) -> tuple[int, str]:
    """Wrapper around tempfile.mkstemp pinned to a specific dir."""
    import tempfile
    fd, name = tempfile.mkstemp(prefix=prefix, suffix=suffix, dir=str(dir_))
    return fd, name
