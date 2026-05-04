"""Append-only outbox for cascade hints from /flow:pause to L3 (personal /save).

Layout:
  ~/.flow/.runtime/hints/                     ← pending hint files
                  /hints/processed/           ← consumed hint files

Each hint is a separate JSON file (no shared single-file race). Filename:
  <ISO8601-with-seconds>-<seq>.json

Consumer (personal /save) calls list_pending(), processes each, then
mark_processed(path) — moves it under processed/.
"""
from __future__ import annotations

import os
from datetime import datetime
from pathlib import Path

from .safe_io import atomic_write_json


def _runtime_dir() -> Path:
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def _hints_dir() -> Path:
    d = _runtime_dir() / "hints"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _processed_dir() -> Path:
    d = _hints_dir() / "processed"
    d.mkdir(parents=True, exist_ok=True)
    return d


def write_hint(payload: dict) -> Path:
    """Write a hint file with a unique filename. Returns the path."""
    payload = dict(payload)
    payload.setdefault("schema_version", 1)
    payload.setdefault("ts", datetime.now().astimezone().isoformat(timespec="seconds"))
    base = payload["ts"].replace(":", "").replace("+", "p").replace("-", "")
    seq = 0
    while True:
        fname = f"{base}-{seq:03d}.json"
        path = _hints_dir() / fname
        if not path.exists():
            atomic_write_json(path, payload)
            return path
        seq += 1


def list_pending() -> list[Path]:
    """Return all *.json files directly under hints/ (NOT processed/)."""
    d = _hints_dir()
    return sorted(p for p in d.glob("*.json") if p.is_file())


def mark_processed(hint_path: Path) -> None:
    """Move hint into processed/ subdir."""
    target = _processed_dir() / hint_path.name
    os.replace(hint_path, target)
