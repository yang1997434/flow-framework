#!/usr/bin/env python3
"""PostToolUse(Bash) hook — credential grep + Lv1 git-commit trickle.

Two responsibilities, both tightly time-bounded (<15s budget):

1. Credential grep after `git commit` — if a commit was made and any value in
   .flow/ or vault matches the credential pattern, surface a warning into the
   model's context.

2. Lv1 trickle (event-driven save, tier 1) — when the bash command was
   `git commit`, append a single-line entry to the active task's
   progress.md `## Commits` section with the new HEAD's short hash + first
   subject line. Debounced: only one append per minute even if multiple
   commits happen rapid-fire (mtime check).

Heartbeat: each invocation also bumps the global tool-call counter via
flow_autosave heartbeat (best-effort, async-style — we don't wait on it).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
FLOW_AUTOSAVE = REPO_ROOT / "scripts" / "flow_autosave.py"

CREDENTIAL_PATTERN = (
    r"(?i)(password|secret|api[_-]?key|token|bearer).*[:=]\s*['\"][^'\"]{4,}['\"]"
)


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir() or (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def find_active_task(project_root: Path) -> Path | None:
    flow = project_root / ".flow"
    if not flow.is_dir():
        return None
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


def get_head_short_hash_and_subject(project_root: Path) -> tuple[str, str] | None:
    try:
        result = subprocess.run(
            ["git", "log", "-1", "--pretty=%h\t%s"],
            cwd=str(project_root),
            capture_output=True,
            text=True,
            timeout=3,
        )
        if result.returncode != 0:
            return None
        line = result.stdout.strip().splitlines()[0] if result.stdout.strip() else ""
        if "\t" not in line:
            return None
        h, subject = line.split("\t", 1)
        return (h.strip(), subject.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def append_commit_to_progress(task_dir: Path, short_hash: str, subject: str) -> bool:
    """Append a single-line commit entry to progress.md `## Commits`.

    Debouncing: if mtime of progress.md is within the same minute as `now`
    AND the file already mentions this short hash, skip. Otherwise append.
    Returns True on append, False on debounce/skip."""
    progress = task_dir / "progress.md"
    if not progress.is_file():
        return False

    text = progress.read_text(encoding="utf-8")
    if short_hash in text:
        return False  # already recorded — idempotent

    # Minute-bucket debounce: if mtime is within the current minute and
    # there is at least one commit line already added in this minute, skip.
    try:
        mtime = progress.stat().st_mtime
    except OSError:
        mtime = 0
    now_epoch = time.time()
    same_minute = int(mtime) // 60 == int(now_epoch) // 60

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- [{timestamp}] `{short_hash}` {subject}"

    if "## Commits" in text:
        if same_minute:
            # Be conservative: a same-minute write already happened. We still
            # allow new hashes (different commits within one minute should be
            # logged), so we keep going. The hash-uniqueness check above
            # already filters redundant writes.
            pass
        new_text = _append_under_heading(text, "## Commits", line)
    else:
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + f"{sep}\n## Commits\n\n{line}\n"

    progress.write_text(new_text, encoding="utf-8")
    return True


def _append_under_heading(text: str, heading: str, line: str) -> str:
    """Insert `line` at the end of the section opened by `heading` (defined as:
    everything until the next heading of equal or higher level, or EOF)."""
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    found = False
    heading_level = heading.count("#")
    while i < len(lines):
        out.append(lines[i])
        if not found and lines[i].strip() == heading:
            found = True
            i += 1
            section: list[str] = []
            while i < len(lines):
                nxt = lines[i]
                stripped = nxt.lstrip()
                if stripped.startswith("#"):
                    n_hash = len(stripped) - len(stripped.lstrip("#"))
                    if n_hash <= heading_level:
                        break
                section.append(nxt)
                i += 1
            cleaned = [
                ln for ln in section
                if not ln.strip().startswith("<!-- TEMPLATE")
            ]
            while cleaned and not cleaned[-1].strip():
                cleaned.pop()
            while cleaned and not cleaned[0].strip():
                cleaned.pop(0)
            if cleaned:
                out.append("")
                out.extend(cleaned)
            out.append("")
            out.append(line)
            out.append("")
            continue
        i += 1
    if not found:
        sep = "" if text.endswith("\n") else "\n"
        return text + f"{sep}\n{heading}\n\n{line}\n"
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def bump_heartbeat(cwd: Path) -> None:
    """Best-effort heartbeat increment. Never blocks (timeout 3s, swallowed)."""
    if not FLOW_AUTOSAVE.is_file():
        return
    try:
        subprocess.run(
            [sys.executable, str(FLOW_AUTOSAVE), "heartbeat", "--cwd", str(cwd)],
            capture_output=True,
            timeout=3,
        )
    except Exception:
        pass


def credential_grep(project_root: Path) -> str | None:
    flow = project_root / ".flow"
    targets = []
    if flow.is_dir():
        targets.append(str(flow))
    vault = Path.home() / "data" / "knowledge-base"
    if vault.is_dir():
        targets.append(str(vault))
    if not targets:
        return None
    try:
        result = subprocess.run(
            ["grep", "-rEni", "--include=*.md", "--include=*.yaml", "--include=*.yml",
             CREDENTIAL_PATTERN, *targets],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    matches = result.stdout.strip()
    return matches or None


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")
    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()

    # Heartbeat bump on every Bash invocation (cheap; bounded).
    bump_heartbeat(cwd)

    # Only the rest of the work is for git-commit events.
    if "git commit" not in command:
        sys.exit(0)

    project_root = find_project_root(cwd)
    if project_root is None:
        sys.exit(0)

    # Lv1 trickle — append commit to progress.md (best-effort, never blocks).
    try:
        task = find_active_task(project_root)
        if task is not None:
            head = get_head_short_hash_and_subject(project_root)
            if head is not None:
                append_commit_to_progress(task, head[0], head[1])
    except Exception:
        pass

    # Credential grep (existing behavior).
    matches = credential_grep(project_root)
    if not matches:
        sys.exit(0)

    warning = (
        "<flow-credential-warning>\n"
        "POSSIBLE credential leak detected after git commit. Review:\n"
        f"{matches}\n\n"
        "If real credentials: rotate immediately, remove from history (git filter-repo), "
        "and move to ~/.flow/credentials.local. If false positive (e.g., template / docs example), "
        "rename the matched key to avoid future false alarms.\n"
        "</flow-credential-warning>"
    )

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": warning,
        }
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
