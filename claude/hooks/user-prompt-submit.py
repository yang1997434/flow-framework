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


SECTION_NAMES = ["Plan", "Execute Log", "Verify Report", "Sediment Notes"]


def extract_section(text: str, name: str) -> str:
    """Return content of `## <name>` section (between this header and next ## or EOF)."""
    pattern = rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


def is_section_filled(content: str) -> bool:
    """A section is 'filled' iff it has non-template, non-comment content.

    Strategy:
      1. Strip HTML comments
      2. Strip blank lines
      3. Check remaining content has any non-trivial text
    """
    # Remove HTML comments (including the TEMPLATE: marker block)
    no_comments = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    # Remove blank-only lines
    lines = [line.rstrip() for line in no_comments.splitlines() if line.strip()]
    return len(lines) > 0


def determine_phase(progress_md: Path) -> str:
    """Detect current phase by checking which sections have user-filled content.

    Phase mapping:
      - Plan empty               → phase1-plan
      - Plan filled, Execute empty → phase2-execute
      - Execute filled, Verify empty → phase3-finish
      - Verify filled, Sediment empty → phase4-sediment
      - All four filled          → done
    """
    if not progress_md.is_file():
        return "phase1-plan"
    text = progress_md.read_text(encoding="utf-8", errors="replace")

    sections = {name: is_section_filled(extract_section(text, name)) for name in SECTION_NAMES}

    if sections["Sediment Notes"]:
        return "done"
    if sections["Verify Report"]:
        return "phase4-sediment"
    if sections["Execute Log"]:
        return "phase3-finish"
    if sections["Plan"]:
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
