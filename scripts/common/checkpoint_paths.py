"""Per-task .checkpoint/ path resolution for v0.5+ state files."""
from __future__ import annotations

from pathlib import Path


def checkpoint_dir(task_dir: Path) -> Path:
    """Return <task>/.checkpoint/, creating it if missing."""
    d = Path(task_dir) / ".checkpoint"
    d.mkdir(parents=True, exist_ok=True)
    return d


def intent_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "intent.md"


def mechanical_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "mechanical.json"


def history_path(task_dir: Path) -> Path:
    return checkpoint_dir(task_dir) / "history.jsonl"


def autopilot_state_path(task_dir: Path) -> Path:
    """v0.6 only — return path even if file doesn't exist yet."""
    return checkpoint_dir(task_dir) / "autopilot-state.json"
