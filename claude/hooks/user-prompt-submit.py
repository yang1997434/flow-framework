#!/usr/bin/env python3
"""UserPromptSubmit hook — detect Flow keywords + inject phase-state breadcrumb.

Reads JSON from stdin. Outputs JSON with hookSpecificOutput.additionalContext.

Detects:
  - Flow trigger keywords ("走 Flow", "use flow", "flow:start", etc.)
    → adds hint: "Use flow-orchestrator skill"
  - Active task state (if .flow/.current-task exists)
    → adds breadcrumb of current phase

Designed to be lightweight (timeout: 5s).
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


FLOW_TRIGGERS = [
    r"\b走\s*Flow\b", r"\b用\s*Flow\b", r"\bFlow\s*流程\b",
    r"\bflow:\w+\b", r"\bstart\s+a\s+(?:flow\s+)?task\b",
    r"\b跑框架\b", r"\buse\s+flow\b",
]

OVERRIDE_TRIGGERS = [
    r"\bskip\s+flow\b", r"\b别走\s*流程\b", r"\b跳过\s*flow\b",
    r"\b小修\s*一下\b", r"\b直接改\b", r"\bjust\s+do\s+it\b",
]


def find_project_flow(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur / ".flow"
        cur = cur.parent
    return None


def determine_phase(progress_md: Path) -> str:
    """Heuristic detection of current phase from progress.md state."""
    if not progress_md.is_file():
        return "phase1-plan"
    text = progress_md.read_text(encoding="utf-8", errors="replace")

    # Find each section + check if it has content beyond the comment placeholder
    has_plan = bool(re.search(r"##\s+Plan\s*\n.*?(?=##|\Z)", text, re.DOTALL)) and "main session" in text or "sub-agent" in text
    has_execute = "Execute Log" in text and re.search(r"\|\s*\d{4}", text)  # has date in table
    has_verify = re.search(r"##\s+Verify Report.*?(pass|fail|pending)", text, re.DOTALL | re.IGNORECASE)
    has_sediment = "Sediment Notes" in text and ("promoted" in text.lower() or "no new" in text.lower())

    if has_sediment:
        return "done"
    if has_verify and "pending" not in str(has_verify):
        return "phase4-sediment"
    if has_execute:
        return "phase3-finish"
    if has_plan:
        return "phase2-execute"
    return "phase1-plan"


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    user_message = hook_input.get("user_prompt") or hook_input.get("message") or ""
    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()

    # Check for override (per-turn opt-out)
    override = any(re.search(p, user_message, re.IGNORECASE) for p in OVERRIDE_TRIGGERS)

    # Check for trigger
    is_flow_trigger = any(re.search(p, user_message, re.IGNORECASE) for p in FLOW_TRIGGERS)

    flow = find_project_flow(cwd)
    parts = []

    if override:
        parts.append("<flow-override>User requested skipping Flow this turn. Acknowledge briefly and proceed without framework.</flow-override>")
    elif is_flow_trigger:
        parts.append("<flow-route>User invoking Flow framework. Load `flow-orchestrator` skill if not already loaded.</flow-route>")

    # Always inject active-task breadcrumb if exists
    if flow:
        pointer = flow / ".current-task"
        if pointer.is_file():
            rel = pointer.read_text(encoding="utf-8").strip()
            if rel:
                task_path = flow.parent / rel if not Path(rel).is_absolute() else Path(rel)
                if task_path.is_dir():
                    progress = task_path / "progress.md"
                    phase = determine_phase(progress)
                    parts.append(f"<flow-state>Active task: `{task_path.name}` | Current phase: {phase}. Use `/flow:continue` to advance, `/flow:pause` to break.</flow-state>")

    if not parts:
        sys.exit(0)  # No output → no injection

    output = {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
