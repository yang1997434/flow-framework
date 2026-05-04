#!/usr/bin/env python3
"""PreCompact hook (v0.5) — write mechanical snapshot before Claude Code auto-compacts.

v0.5 behavior: write <task>/.checkpoint/mechanical.json (atomic) and append
one line to history.jsonl. Never block compact (always exit 0).

v0.6 will additionally fork the autopilot-checkpoint script when
autopilot-state.json exists and is active. That extension is NOT in this
file yet — see docs/specs/2026-05-04-auto-resume-design.md component K.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.safe_io import atomic_write_json, append_jsonl_locked
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur
        cur = cur.parent
    return None


def find_active_task(project_root: Path) -> Path | None:
    flow = project_root / ".flow"
    ptr = flow / ".current-task"
    if not ptr.is_file():
        return None
    raw = ptr.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = project_root / p
    return p if p.is_dir() else None


def main() -> int:
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        return 0

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    transcript_path = hook_input.get("transcript_path") or None

    project_root = find_project_root(cwd)
    if project_root is None:
        return 0

    task_dir = find_active_task(project_root)
    if task_dir is None:
        return 0

    try:
        payload = build_payload(
            project_root=project_root,
            task_dir=task_dir,
            trigger="precompact",
            transcript_path=transcript_path,
        )
        atomic_write_json(mechanical_path(task_dir), payload)
        append_jsonl_locked(history_path(task_dir), {
            "schema_version": 1,
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "precompact",
            "ctx_pct": payload.get("context_pct_estimated", 0),
            "trigger_origin": "hook",
        })
    except Exception:
        # Fail-closed: never block compact. Audit gap acceptable.
        pass

    return 0


if __name__ == "__main__":
    sys.exit(main())
