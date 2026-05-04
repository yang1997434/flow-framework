"""Context-pressure nudge helper for PostToolUse hooks (v0.5).

Decides whether to inject a 'consider /flow:pause' reminder into the model's
next turn. State per task slug (not per cwd) so multi-task projects don't
collide. State at ~/.flow/.runtime/nudge-state-<task_slug>.json.

v0.5 assumes a single Claude Code session per FLOW_HOME. State writes are
last-writer-wins (atomic_write_json); concurrent multi-session writes are
not protected. v0.6 multi-session may need fcntl.flock around _write_state.

acknowledge() / maybe_nudge_text() rely on rotate_window() being called by
session-start.py compact-matcher (Task 10). If that wiring breaks, ack from
window N silences window N+1 forever — fixable by deleting the state file.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Optional

from .safe_io import atomic_write_json

CTX_THRESHOLD_PCT = 50  # configurable later via flow.config.local.yaml


def _runtime_dir() -> Path:
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def _state_path(task_slug: str) -> Path:
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in task_slug)
    return _runtime_dir() / f"nudge-state-{safe}.json"


def _read_state(task_slug: str) -> dict:
    p = _state_path(task_slug)
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _write_state(task_slug: str, state: dict) -> None:
    state.setdefault("schema_version", 1)
    state.setdefault("task_slug", task_slug)
    atomic_write_json(_state_path(task_slug), state)


def maybe_nudge_text(
    task_slug: str,
    pct: Optional[int],
    confidence: str,
    window_id: str,
    min_seconds_between: int = 60,
) -> Optional[str]:
    """Decide whether a nudge should fire and return the additionalContext
    text, or None if not.

    Side effect: updates nudge-state with last_nudge_ts + last_nudge_ctx_pct
    when a nudge IS fired. acknowledge() is a separate call.
    """
    if pct is None or pct < CTX_THRESHOLD_PCT:
        return None
    if confidence == "low":
        return None

    state = _read_state(task_slug)
    now = datetime.now().astimezone()

    if state.get("current_window_id") == window_id and state.get("acknowledged"):
        return None

    last_ts = state.get("last_nudge_ts")
    if last_ts and state.get("current_window_id") == window_id:
        try:
            last = datetime.fromisoformat(last_ts)
            if (now - last).total_seconds() < min_seconds_between:
                return None
        except ValueError:
            pass

    text = (
        f"<flow-checkpoint-suggested priority=\"medium\" cycle=\"{window_id}\">\n"
        f"Context usage estimated at {pct}% (estimator confidence: {confidence}).\n"
        f"Best moment to checkpoint while model is still clear.\n\n"
        f"Tell the user verbatim before any other content (only once per session):\n"
        f"> 💾 上下文已到 {pct}%。建议 /flow:pause 存档，新 session 跑 /flow:resume 续上。\n\n"
        f"This is a soft hint — user may continue if they prefer. Do not interrupt\n"
        f"in-flight tool sequences; surface at the next natural pause.\n"
        f"</flow-checkpoint-suggested>"
    )

    _write_state(task_slug, {
        "current_window_id": window_id,
        "last_nudge_ts": now.isoformat(timespec="seconds"),
        "last_nudge_ctx_pct": pct,
        "acknowledged": False,
        "acknowledged_via": None,
    })
    return text


def acknowledge(task_slug: str, via: str) -> None:
    """Mark current nudge as acknowledged (e.g., user ran /flow:pause)."""
    state = _read_state(task_slug)
    state["acknowledged"] = True
    state["acknowledged_via"] = via
    state["acknowledged_ts"] = datetime.now().astimezone().isoformat(timespec="seconds")
    _write_state(task_slug, state)


def derive_window_id(task_slug: str) -> str:
    """Produce a stable cycle id for the current window. Caller (SessionStart
    on `compact`) is expected to roll over by calling rotate_window.

    Persists the freshly-minted id on first call so subsequent invocations
    return the same value until rotate_window mints a new one. Without this,
    every PostToolUse hook would mint a fresh ts-based id, defeating the
    per-window throttle in maybe_nudge_text.
    """
    state = _read_state(task_slug)
    existing = state.get("current_window_id")
    if existing:
        return existing
    new_id = f"cycle-{datetime.now().astimezone().isoformat(timespec='seconds')}"
    state["current_window_id"] = new_id
    _write_state(task_slug, state)
    return new_id


def rotate_window(task_slug: str) -> str:
    """Force a new window_id (called by SessionStart on `compact` matcher)."""
    new_id = f"cycle-{datetime.now().astimezone().isoformat(timespec='seconds')}"
    _write_state(task_slug, {
        "current_window_id": new_id,
        "acknowledged": False,
        "acknowledged_via": None,
        "last_nudge_ts": None,
        "last_nudge_ctx_pct": None,
    })
    return new_id
