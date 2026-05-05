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

# Match old-format autosave breadcrumbs that may linger in pre-fix progress.md
# files. Pattern: "- [YYYY-MM-DD HH:MM] distill queued (trigger=stop)" — written
# by flow_autosave.py < v0.5 into `## Sediment Notes`. Post-fix, autosave writes
# to ~/.flow/.runtime/autosave-log-<hash>.md instead, but old files may persist.
AUTOSAVE_BREADCRUMB_RE = re.compile(
    r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] distill queued",
    re.MULTILINE,
)


def extract_section(text: str, name: str) -> str:
    """Return content of `## <name>` section (between this header and next ## or EOF)."""
    pattern = rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


def is_section_filled(content: str) -> bool:
    """A section is 'filled' iff it has non-template, non-comment, non-autosave content.

    Strategy:
      1. Strip HTML comments (incl. <!-- TEMPLATE: ... --> blocks)
      2. Strip autosave breadcrumb lines (defense-in-depth — autosave now writes
         to ~/.flow/.runtime/, but pre-fix progress.md files may still contain
         "distill queued" lines that would otherwise look like real content)
      3. Strip blank lines
      4. Check remaining content has any non-trivial text
    """
    no_comments = re.sub(r"<!--.*?-->", "", content, flags=re.DOTALL)
    no_autosave = AUTOSAVE_BREADCRUMB_RE.sub("", no_comments)
    lines = [line.rstrip() for line in no_autosave.splitlines() if line.strip()]
    return len(lines) > 0


def determine_phase(progress_md: Path) -> str:
    """Detect current phase by checking which sections have user-filled content.

    Phase mapping (REQUIRES sequential filling — a later section being filled
    no longer skips ahead past empty earlier sections):
      - Plan empty                                   → phase1-plan
      - Plan filled, Execute empty                    → phase2-execute
      - Plan + Execute filled, Verify empty           → phase3-finish
      - Plan + Execute + Verify filled, Sediment empty → phase4-sediment
      - All four filled                              → done

    Why sequential: prior version returned `done` whenever Sediment Notes had
    *any* non-template content, even if Plan was empty. That allowed
    automated breadcrumbs (or stray writes) to a downstream section to fool
    the phase determination. Sequential AND-chain blocks that.

    Future enhancement: optional `<!-- phaseN-approved -->` markers for
    explicit user-gating. Currently relies on sequential filling alone.
    """
    if not progress_md.is_file():
        return "phase1-plan"
    text = progress_md.read_text(encoding="utf-8", errors="replace")

    sections = {name: is_section_filled(extract_section(text, name)) for name in SECTION_NAMES}

    plan_filled     = sections["Plan"]
    execute_filled  = plan_filled and sections["Execute Log"]
    verify_filled   = execute_filled and sections["Verify Report"]
    sediment_filled = verify_filled and sections["Sediment Notes"]

    if sediment_filled:
        return "done"
    if verify_filled:
        return "phase4-sediment"
    if execute_filled:
        return "phase3-finish"
    if plan_filled:
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
