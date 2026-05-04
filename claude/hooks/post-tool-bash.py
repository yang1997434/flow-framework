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
import shlex
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional


SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
FLOW_AUTOSAVE = REPO_ROOT / "scripts" / "flow_autosave.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.context_estimator import estimate_context_pct
from common.nudge import maybe_nudge_text, derive_window_id
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload
from common.safe_io import atomic_write_json, append_jsonl_locked, locked_text_rmw

CREDENTIAL_PATTERN = (
    # Note: case-insensitivity is provided by `grep -i` below. Do NOT prepend
    # `(?i)` — GNU grep -E treats it as a literal optional group, breaking
    # the intent. (Local dev with ugrep aliased as grep masked this.)
    r"(password|secret|api[_-]?key|token|bearer).*[:=]\s*['\"][^'\"]{4,}['\"]"
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

    Idempotency: if file already mentions this short hash, skip (the
    transform returns the original text unchanged → locked_text_rmw
    returns False).

    Concurrency: serialized via fcntl.LOCK_EX so a racing post-tool-edit
    `## Files Touched` write can't clobber our `## Commits` append.
    Returns True on append, False on skip/lock-timeout/no-file."""
    progress = task_dir / "progress.md"
    if not progress.is_file():
        return False

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    line = f"- [{timestamp}] `{short_hash}` {subject}"

    def _transform(text: str) -> str:
        if short_hash in text:
            return text  # already recorded — idempotent (no-op)
        if "## Commits" in text:
            return _append_under_heading(text, "## Commits", line)
        sep = "" if text.endswith("\n") else "\n"
        return text + f"{sep}\n## Commits\n\n{line}\n"

    return locked_text_rmw(progress, _transform)


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
    """Best-effort heartbeat increment. Fire-and-forget so the hook never
    waits on the autosave subprocess (which can momentarily stall on git ops).

    The child is detached via start_new_session so it survives this hook's
    exit; stdio is redirected to DEVNULL so it can't write back into the
    Claude Code transport.
    """
    if not FLOW_AUTOSAVE.is_file():
        return
    try:
        subprocess.Popen(
            [sys.executable, str(FLOW_AUTOSAVE), "heartbeat", "--cwd", str(cwd)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
    except Exception:
        pass


_SHELL_SEPARATORS = frozenset({
    "&&", "||", ";", "|", "&", "(", ")", "{", "}",
    "then", "else", "elif", "fi", "do", "done", "!",
})


def is_git_commit_command(command: str) -> bool:
    """True if `command` invokes `git commit` somewhere.

    Tokenizes via shlex so multi-space and quoted forms work, and so
    `git -C path commit -m "..."` and `... && git commit` both match.
    Requires the `git` token to sit at the start of a command segment
    (index 0 or right after a shell separator like `&&`, `;`, `|`) so
    that `echo git commit` does NOT match. Looks for a `commit` token
    within 6 tokens of `git` — enough slack for `-C <path>`,
    `--git-dir=...`, `-c key=val`, etc.
    """
    if not command:
        return False
    try:
        tokens = shlex.split(command, comments=True, posix=True)
    except ValueError:
        # Unbalanced quotes — fall back to whitespace split.
        tokens = command.split()
    n = len(tokens)
    for i, t in enumerate(tokens):
        if not (t == "git" or t.endswith("/git")):
            continue
        # Segment-start guard: avoid `echo git commit`, `printf "%s" git commit`, etc.
        if i > 0 and tokens[i - 1] not in _SHELL_SEPARATORS:
            continue
        for j in range(i + 1, min(i + 7, n)):
            if tokens[j] == "commit":
                return True
    return False


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


def _maybe_nudge_and_update_mechanical(
    project_root: Path,
    task_dir: Path,
    transcript_path: str,
) -> Optional[str]:
    """v0.5 PostToolUse extension: emit nudge if ctx >= threshold AND not
    acknowledged this window; throttle mechanical.json writes to once per 60s.

    Returns the nudge text (to be merged into a single hookSpecificOutput
    by the caller) or None.
    """
    try:
        pct, conf = estimate_context_pct(transcript_path)
        if pct is None:
            return None

        window_id = derive_window_id(task_dir.name)
        nudge_text = maybe_nudge_text(
            task_slug=task_dir.name,
            pct=pct,
            confidence=conf,
            window_id=window_id,
            min_seconds_between=60,
        )

        # Throttled mechanical update — only if last write > 60s ago
        mech = mechanical_path(task_dir)
        now_epoch = time.time()
        write_mech = True
        if mech.is_file():
            try:
                if now_epoch - mech.stat().st_mtime < 60:
                    write_mech = False
            except OSError:
                pass
        if write_mech:
            payload = build_payload(
                project_root=project_root,
                task_dir=task_dir,
                trigger="post-tool",
                transcript_path=transcript_path,
            )
            atomic_write_json(mech, payload)

        if nudge_text:
            append_jsonl_locked(history_path(task_dir), {
                "schema_version": 1,
                "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                "event": "nudge_emitted",
                "ctx_pct": pct,
                "estimator_confidence": conf,
                "window_id": window_id,
            })
            return nudge_text
        return None
    except Exception:
        # Fail-closed; never break the hook chain.
        return None


def _emit_post_tool_output(additional_context: str) -> None:
    """Emit the single allowed PostToolUse JSON output."""
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PostToolUse",
            "additionalContext": additional_context,
        }
    }, ensure_ascii=False), flush=True)


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

    # v0.5: context-pressure nudge + throttled mechanical update.
    transcript_path = hook_input.get("transcript_path")
    nudge_text: Optional[str] = None
    project_root = find_project_root(cwd)
    if project_root is not None:
        task_dir = find_active_task(project_root)
        if task_dir is not None and transcript_path:
            nudge_text = _maybe_nudge_and_update_mechanical(
                project_root=project_root,
                task_dir=task_dir,
                transcript_path=transcript_path,
            )

    # Only the rest of the work is for git-commit events.
    if not is_git_commit_command(command):
        if nudge_text:
            _emit_post_tool_output(nudge_text)
        sys.exit(0)

    if project_root is None:
        if nudge_text:
            _emit_post_tool_output(nudge_text)
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
    if not matches and not nudge_text:
        sys.exit(0)

    parts = []
    if nudge_text:
        parts.append(nudge_text)
    if matches:
        parts.append(
            "<flow-credential-warning>\n"
            "POSSIBLE credential leak detected after git commit. Review:\n"
            f"{matches}\n\n"
            "If real credentials: rotate immediately, remove from history (git filter-repo), "
            "and move to ~/.flow/credentials.local. If false positive (e.g., template / docs example), "
            "rename the matched key to avoid future false alarms.\n"
            "</flow-credential-warning>"
        )

    _emit_post_tool_output("\n\n".join(parts))


if __name__ == "__main__":
    main()
