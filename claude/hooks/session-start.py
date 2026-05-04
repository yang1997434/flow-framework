#!/usr/bin/env python3
"""SessionStart hook — inject Quick Read Guide + active task + relevant pitfalls.

Triggered on: startup / clear / compact

Reads JSON from stdin (hook_input). Outputs JSON with hookSpecificOutput.additionalContext.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path


# Repo location — symlink target found via this script's path
SCRIPT_PATH = Path(__file__).resolve()
# If installed as symlink, resolve original location
REPO_ROOT = SCRIPT_PATH.parent.parent.parent  # claude/hooks/<this> → flow-framework/


def find_project_flow(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur / ".flow"
        cur = cur.parent
    return None


def load_active_task(flow: Path) -> dict | None:
    pointer = flow / ".current-task"
    if not pointer.is_file():
        return None
    rel = pointer.read_text(encoding="utf-8").strip()
    if not rel:
        return None
    task_path = flow.parent / rel if not Path(rel).is_absolute() else Path(rel)
    if not task_path.is_dir():
        return {"path": str(task_path), "stale": True}

    info = {"path": str(task_path), "name": task_path.name, "stale": False}
    prd = task_path / "prd.md"
    if prd.is_file():
        for line in prd.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                info["title"] = line[2:].strip()
                break
    return info


def run_skill_diff_silently() -> str | None:
    """Trigger flow_skill_diff against current snapshot.

    Returns the contents of skill-diff-pending.md if there's an unresolved
    suggestion (either freshly generated or leftover from before). Best-effort:
    failures are silent — never block session start.
    """
    pending = Path.home() / ".flow" / ".runtime" / "skill-diff-pending.md"
    diff_script = REPO_ROOT / "scripts" / "flow_skill_diff.py"
    if not diff_script.is_file():
        return pending.read_text(encoding="utf-8") if pending.is_file() else None
    try:
        import subprocess
        subprocess.run(
            ["python3", str(diff_script), "diff", "--quiet"],
            capture_output=True, timeout=8,
        )
    except (subprocess.TimeoutExpired, OSError):
        pass
    if pending.is_file():
        return pending.read_text(encoding="utf-8")
    return None


def find_relevant_pitfalls(flow: Path, vault: Path) -> list[str]:
    """Find pitfalls whose trigger_paths might match files in this project.

    Heuristic-only: returns names of pitfall files in project + vault.
    Real matching against open files is left to the model on demand.
    """
    pitfalls = []
    for d in [flow / "pitfalls", vault / "pitfalls"]:
        if d.is_dir():
            for p in sorted(d.glob("*.md")):
                pitfalls.append(str(p))
    return pitfalls[:10]  # cap at 10


def quick_read_guide() -> str:
    return """# Flow Framework — Quick Read Guide

If user asks for Flow workflow, **load the orchestrator skill first** (`flow-orchestrator/SKILL.md`).

When you need framework reference:
- Triage criteria → orchestrator skill Step 2
- Phase X behavior → flow-phaseX-*/SKILL.md (load only the current phase)
- Specific skill chain → docs/Skills-Phase映射.md (only the relevant task type section)
- Pitfall reference → currently-loaded pitfalls in <relevant-pitfalls> below

Don't read full design doc unless user asks "explain the framework".

Available slash commands: /flow:start /flow:continue /flow:resume /flow:finish /flow:pitfall /flow:promote /flow:codex-review /flow:pause"""


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        hook_input = {}

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    flow = find_project_flow(cwd)
    vault = Path.home() / "data" / "knowledge-base"

    parts = ["<flow-context>"]
    parts.append(quick_read_guide())

    if flow:
        active = load_active_task(flow)
        if active:
            parts.append("\n## Active Task")
            if active["stale"]:
                parts.append(f"⚠️ STALE pointer: `{active['path']}` (run `/flow:resume` to clear)")
            else:
                parts.append(f"- Path: `{active['path']}`")
                parts.append(f"- Name: `{active['name']}`")
                parts.append(f"- Title: {active.get('title', '(unknown)')}")
                parts.append("- To resume: read prd.md + progress.md, then `/flow:continue`")

        # Relevant pitfalls (project + vault)
        pitfalls = find_relevant_pitfalls(flow, vault)
        if pitfalls:
            parts.append("\n## Available Pitfalls (project + vault)")
            for p in pitfalls:
                parts.append(f"- `{p}`")
            parts.append("\nRead a pitfall when its `trigger_paths` matches files you're working on.")
    else:
        parts.append("\n## Status")
        parts.append("No `.flow/` in this project. Run `flow init` to set up.")

    # Skill compatibility diff — surface pending suggestion if any
    skill_diff = run_skill_diff_silently()
    if skill_diff:
        parts.append("\n## Skill Compatibility Diff (pending)")
        parts.append(skill_diff.rstrip())
        parts.append("\nIf no action needed, dismiss with: `flow skill-diff clear`")

    parts.append("</flow-context>")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "SessionStart",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
