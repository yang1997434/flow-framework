#!/usr/bin/env python3
"""Stop hook — auto-save current task progress when session ends.

Triggered when Claude Code session ends or context is exhausted.
Calls flow_save.py to write a journal entry for the active task.

Best-effort: if anything fails, exit silently (don't block session close).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
FLOW_SAVE = REPO_ROOT / "scripts" / "flow_save.py"


def find_project_flow(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur / ".flow"
        cur = cur.parent
    return None


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    flow = find_project_flow(cwd)
    if not flow:
        sys.exit(0)

    pointer = flow / ".current-task"
    if not pointer.is_file():
        sys.exit(0)

    if not FLOW_SAVE.is_file():
        sys.exit(0)

    # Run flow_save in best-effort mode
    try:
        subprocess.run(
            [sys.executable, str(FLOW_SAVE), "--note", "auto-save (session stop)", "--no-commit"],
            cwd=flow.parent,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass  # silent

    sys.exit(0)


if __name__ == "__main__":
    main()
