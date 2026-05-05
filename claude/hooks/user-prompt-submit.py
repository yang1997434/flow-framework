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

# Canonical phase progression. Used by min_phase() to clamp section-based
# advancement against the user's explicit frontmatter declaration.
PHASE_ORDER = ("phase1-plan", "phase2-execute", "phase3-finish", "phase4-sediment", "done")

# Map progress.md frontmatter `phase:` field (the user-authoritative declaration)
# to the canonical phase output names. Frontmatter values are documented in the
# task template comments: triage | research | implement | check | verify | sediment.
PHASE_FRONTMATTER_MAP = {
    "triage":    "phase1-plan",
    "research":  "phase1-plan",
    "implement": "phase2-execute",
    "check":     "phase3-finish",
    "verify":    "phase3-finish",
    "sediment":  "phase4-sediment",
}

# Match old-format autosave breadcrumbs that may linger in pre-fix progress.md
# files. Pattern variants observed in pre-fix files:
#   - [YYYY-MM-DD HH:MM] distill queued
#   - [YYYY-MM-DD HH:MM] distill queued (trigger=stop)
#   - [YYYY-MM-DD HH:MM] distill queued (trigger=heartbeat) — after 70 tool calls
# The trailing `.*$` consumes the entire line up to the newline so the section
# is fully erased from the "filled" check (otherwise residual trigger/note text
# remained, fooling is_section_filled). Post-fix, autosave writes to
# ~/.flow/.runtime/autosave-log-<hash>.md instead, but old files may persist.
AUTOSAVE_BREADCRUMB_RE = re.compile(
    r"^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] distill queued.*$",
    re.MULTILINE,
)


def extract_section(text: str, name: str) -> str:
    """Return content of `## <name>` section (between this header and next ## or EOF)."""
    pattern = rf"^##\s+{re.escape(name)}\s*\n(.*?)(?=^##\s|\Z)"
    m = re.search(pattern, text, re.DOTALL | re.MULTILINE)
    return m.group(1) if m else ""


def parse_frontmatter_phase(text: str) -> str | None:
    """Extract the `phase:` field from YAML frontmatter and map to canonical name.

    Returns one of PHASE_ORDER values, or None if no frontmatter / no phase field /
    unrecognized value. Defensive against malformed YAML — never raises.
    """
    m = re.match(r"^---\n(.*?)\n---", text, re.DOTALL)
    if not m:
        return None
    pm = re.search(r"^\s*phase:\s*([A-Za-z][A-Za-z0-9_-]*)", m.group(1), re.MULTILINE)
    if not pm:
        return None
    return PHASE_FRONTMATTER_MAP.get(pm.group(1))


def min_phase(a: str, b: str) -> str:
    """Return the earlier of two canonical phases per PHASE_ORDER.

    Why: section-based heuristic can over-advance when a downstream section gets
    transient content (e.g. brainstorm milestones logged to Execute Log during
    phase 1). The frontmatter `phase:` field is the user's authoritative
    declaration and acts as an upper bound.
    """
    try:
        return a if PHASE_ORDER.index(a) <= PHASE_ORDER.index(b) else b
    except ValueError:
        return a  # unknown phase string — fail safe to first arg


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
    """Detect current phase by combining frontmatter declaration + section content.

    Resolution order:
      1. Section-based heuristic computes a candidate phase from filled sections
         (sequential AND-chain — a later section being filled no longer skips
         ahead past empty earlier sections).
      2. Frontmatter `phase:` field acts as an UPPER BOUND on advancement.
         If the user explicitly says they're still in phase1 (triage/research)
         or phase2 (implement), we respect that even if Execute Log has content.

    Section heuristic mapping:
      - Plan empty                                   → phase1-plan
      - Plan filled, Execute empty                    → phase2-execute
      - Plan + Execute filled, Verify empty           → phase3-finish
      - Plan + Execute + Verify filled, Sediment empty → phase4-sediment
      - All four filled                              → done

    Why frontmatter cap: brainstorm milestones (e.g. "sub-agent dispatched",
    "decisions locked") logged to Execute Log during phase 1 should NOT
    auto-advance the phase to phase3-finish. The user's explicit `phase:
    triage|research` declaration in frontmatter is the authoritative signal
    of where they are; /flow:continue is responsible for advancing it.

    Why min(): if the user manually advances frontmatter to "implement" but
    Plan section is still empty, sections (phase1-plan) win — prevents the
    inverse bug where a stale frontmatter advance jumps the displayed phase
    past actual artifact reality.
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
        section_phase = "done"
    elif verify_filled:
        section_phase = "phase4-sediment"
    elif execute_filled:
        section_phase = "phase3-finish"
    elif plan_filled:
        section_phase = "phase2-execute"
    else:
        section_phase = "phase1-plan"

    # Section heuristic wins when artifacts say "done" (all 4 sections filled).
    # Why: PHASE_FRONTMATTER_MAP has no key for "done" — the frontmatter enum
    # tops out at `sediment` (= phase4-sediment). If we always cap by frontmatter,
    # a fully-completed task with `phase: sediment` would forever report
    # phase4-sediment instead of done, even after Sediment Notes is filled.
    # Letting `done` short-circuit the cap preserves natural completion state.
    if section_phase == "done":
        return "done"
    fm_phase = parse_frontmatter_phase(text)
    if fm_phase is not None:
        return min_phase(fm_phase, section_phase)
    return section_phase


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
