#!/usr/bin/env python3
"""Stop hook — Lv3 distill trigger (with cooldown).

v0.4 change: raw session persistence is delegated to the context-mode plugin
(Layer 1). flow's stop.py no longer writes a raw journal entry. Instead it
queues an Lv3 semantic distill via scripts/flow_autosave.py — which itself
does NOT call an LLM (hook timeout / cost risk). The orchestrator only writes
a "distill queued" marker to the active task's progress.md `## Sediment Notes`
section and appends a record to ~/.flow/.runtime/distill-queue.jsonl. A later
SessionStart / explicit slash command can drive the actual LLM distillation.

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
FLOW_AUTOSAVE = REPO_ROOT / "scripts" / "flow_autosave.py"


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
        # No active task -> nothing to distill at the semantic layer.
        sys.exit(0)

    if not FLOW_AUTOSAVE.is_file():
        sys.exit(0)

    # Queue an Lv3 distill (cooldown is enforced inside flow_autosave.py).
    try:
        subprocess.run(
            [
                sys.executable,
                str(FLOW_AUTOSAVE),
                "distill",
                "--trigger", "stop",
                "--cwd", str(cwd),
            ],
            cwd=flow.parent,
            capture_output=True,
            timeout=10,
        )
    except Exception:
        pass  # silent — Stop hook must never block

    sys.exit(0)


if __name__ == "__main__":
    main()
