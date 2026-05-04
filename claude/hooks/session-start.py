#!/usr/bin/env python3
"""SessionStart hook — inject Quick Read Guide + active task + relevant pitfalls.

Triggered on: startup / clear / compact

Reads JSON from stdin (hook_input). Outputs JSON with hookSpecificOutput.additionalContext.
"""
from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path


# Repo location — symlink target found via this script's path
SCRIPT_PATH = Path(__file__).resolve()
# If installed as symlink, resolve original location
REPO_ROOT = SCRIPT_PATH.parent.parent.parent  # claude/hooks/<this> → flow-framework/

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.checkpoint_paths import intent_path, mechanical_path, history_path
from common.safe_io import append_jsonl_locked
from common.nudge import rotate_window


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


def build_compact_resume_block(task_dir: Path) -> str | None:
    """If .checkpoint/ exists, build the <flow-resumed-from-compact> block.
    Returns None if no checkpoint files present (fall back to startup behavior)."""
    # Guard BEFORE calling intent_path/mechanical_path — those helpers create
    # .checkpoint/ via mkdir(parents=True, exist_ok=True), which would have a
    # read-side side effect of polluting the task dir on a no-checkpoint read.
    cp = task_dir / ".checkpoint"
    if not cp.is_dir():
        return None
    intent = intent_path(task_dir)
    mech = mechanical_path(task_dir)
    if not intent.is_file() and not mech.is_file():
        return None

    parts = ["<flow-resumed-from-compact>"]

    if intent.is_file():
        text = intent.read_text(encoding="utf-8", errors="replace")
        # Truncate body to ~1500 tokens (roughly 6000 chars) if huge.
        # Clip at the last full line break so we never cut mid-line.
        if len(text) > 6000:
            text = text[:6000].rsplit("\n", 1)[0] + "\n\n[... truncated, see full file at " + str(intent) + "]"
        parts.append("## Last Intent")
        parts.append(text.rstrip())

    intent_ts = None
    mech_ts = None
    if mech.is_file():
        try:
            data = json.loads(mech.read_text(encoding="utf-8"))
            mech_ts = data.get("ts")
            git_info = data.get("git", {})
            files = data.get("files_touched_recent", [])
            parts.append("\n## Latest Mechanical State")
            parts.append(f"- Snapshot ts: {mech_ts}")
            parts.append(f"- Branch: {git_info.get('branch', '?')} @ {git_info.get('head', '?')}")
            commits = git_info.get("recent_commits", [])
            if commits:
                parts.append("- Recent commits:")
                for c in commits[:5]:
                    parts.append(f"  - {c.get('hash', '?')} {c.get('subject', '')}")
            if files:
                parts.append(f"- Files touched recent: {', '.join(files[:10])}")
        except (json.JSONDecodeError, OSError):
            pass

    if intent.is_file():
        try:
            text_head = intent.read_text(encoding="utf-8", errors="replace")
            m = re.match(r"\A---\s*\n(.*?)\n---\s*\n", text_head, re.DOTALL)
            if m:
                # No PyYAML in this project — use a tolerant regex over the
                # frontmatter block. Handles `ts: 2026-...`, `ts: '2026-...'`,
                # `ts: "2026-..."` (single line, no nested mapping).
                ts_match = re.search(
                    r"^\s*ts:\s*['\"]?([^'\"\n]+?)['\"]?\s*$",
                    m.group(1),
                    re.MULTILINE,
                )
                if ts_match:
                    intent_ts = ts_match.group(1).strip()
        except OSError:
            pass

    if intent_ts and mech_ts:
        try:
            ti = datetime.fromisoformat(intent_ts)
            tm = datetime.fromisoformat(mech_ts)
            if tm - ti > timedelta(minutes=5):
                delta_min = round((tm - ti).total_seconds() / 60)
                parts.append("\n## Staleness")
                parts.append(
                    f"⚠️ Mechanical state is {delta_min} minutes newer than intent. "
                    f"Review commits + file edits before assuming intent is still fresh."
                )
        except ValueError:
            pass

    # Resume Mode last — directive should be the final thing the model reads.
    parts.append("\n## Resume Mode")
    parts.append("MANUAL — present the briefing above to the user, then await their direction.")
    parts.append("Do NOT auto-execute next actions.")

    parts.append("</flow-resumed-from-compact>")
    return "\n".join(parts)


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

    # v0.5: SessionStart(compact) → re-inject latest checkpoint state
    matcher = hook_input.get("trigger") or hook_input.get("hook_event_matcher") or ""
    if matcher == "compact" and flow:
        active = load_active_task(flow)
        if active and not active.get("stale"):
            task_dir = Path(active["path"])
            block = build_compact_resume_block(task_dir)
            if block:
                parts.append("\n" + block)
                # Append history event + roll over nudge window.
                # Narrow except: tolerate filesystem / encoding / lock-timeout
                # issues but let truly unexpected errors propagate (consistent
                # with run_skill_diff_silently above).
                try:
                    append_jsonl_locked(history_path(task_dir), {
                        "schema_version": 1,
                        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "event": "resumed_from_compact",
                        "mode": "manual",
                    })
                    rotate_window(task_dir.name)
                except (OSError, json.JSONDecodeError, ValueError):
                    pass

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
