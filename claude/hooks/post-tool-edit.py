#!/usr/bin/env python3
"""PostToolUse(Edit | Write) hook — Lv1 file-touch trickle.

Records every Edit / Write target into ~/.flow/.runtime/touched-{cwd-hash}.log
(JSONL, one record per line). Periodically flushes a deduplicated tail of the
last N touched files into the active task's progress.md `## Files Touched`
section.

Debouncing — flush only when one of:
  * 60 seconds have elapsed since the last flush
  * 10 unflushed records have accumulated since the last flush

Flush state is tracked in ~/.flow/.runtime/touched-{cwd-hash}.flush, a tiny
JSON file with `last_flush_epoch` + `unflushed_count`.

Hook timeout: 5s (matches PostToolUse defaults). Heartbeat bump is fired in
the same call so non-Bash tool calls also count toward heartbeat thresholds.
"""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve()
REPO_ROOT = SCRIPT_PATH.parent.parent.parent
FLOW_AUTOSAVE = REPO_ROOT / "scripts" / "flow_autosave.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
from common.context_estimator import estimate_context_pct
from common.nudge import maybe_nudge_text, derive_window_id
from common.checkpoint_paths import mechanical_path, history_path
from common.mechanical import build_payload
from common.safe_io import atomic_write_json, append_jsonl_locked
from typing import Optional

FLUSH_AFTER_SECONDS = 60
FLUSH_AFTER_COUNT = 10
LAST_N_FILES_IN_PROGRESS = 20  # how many recent unique files to surface


def runtime_dir() -> Path:
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def cwd_hash(cwd: Path) -> str:
    return hashlib.sha1(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]


def touched_log_path(cwd: Path) -> Path:
    return runtime_dir() / f"touched-{cwd_hash(cwd)}.log"


def flush_state_path(cwd: Path) -> Path:
    return runtime_dir() / f"touched-{cwd_hash(cwd)}.flush"


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir() or (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def find_active_task(project_root: Path) -> Path | None:
    flow = project_root / ".flow"
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


def extract_file_path(tool_name: str, tool_input: dict) -> str | None:
    """Pull the target file path out of an Edit / Write tool input."""
    if tool_name in ("Edit", "Write", "MultiEdit"):
        return tool_input.get("file_path")
    return None


def append_record(log_path: Path, record: dict) -> None:
    try:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass


def read_flush_state(p: Path) -> dict:
    if not p.is_file():
        return {"last_flush_epoch": 0, "unflushed_count": 0}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {"last_flush_epoch": 0, "unflushed_count": 0}


def write_flush_state(p: Path, state: dict) -> None:
    try:
        p.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")
    except OSError:
        pass


def should_flush(state: dict, now_epoch: float) -> bool:
    age = now_epoch - state.get("last_flush_epoch", 0)
    if age >= FLUSH_AFTER_SECONDS:
        return True
    if state.get("unflushed_count", 0) >= FLUSH_AFTER_COUNT:
        return True
    return False


def collect_recent_files(log_path: Path, project_root: Path, n: int) -> list[str]:
    """Read tail of touched log, dedupe by file path (last write wins),
    return up to `n` most recent unique relative paths."""
    if not log_path.is_file():
        return []
    try:
        lines = log_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    seen: dict[str, int] = {}
    for idx, ln in enumerate(lines):
        if not ln.strip():
            continue
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        path = rec.get("path")
        if not path:
            continue
        # Try to make relative to project_root for tidiness
        try:
            rel = str(Path(path).resolve().relative_to(project_root.resolve()))
        except ValueError:
            rel = path
        seen[rel] = idx  # later entries overwrite earlier ones
    # Sort by appearance order (descending) and take last n unique
    ordered = sorted(seen.items(), key=lambda kv: kv[1], reverse=True)
    return [k for k, _ in ordered[:n]]


def render_files_block(paths: list[str]) -> str:
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    if not paths:
        return f"_(no recent edits as of {timestamp})_"
    lines = [f"_Updated {timestamp} (last {len(paths)} unique edits)_:", ""]
    for p in paths:
        lines.append(f"- `{p}`")
    return "\n".join(lines)


def upsert_files_section(progress: Path, block: str) -> bool:
    """Replace the body of `## Files Touched` (or create it) with `block`.
    Returns True if file was updated, False on no-op."""
    if not progress.is_file():
        return False
    text = progress.read_text(encoding="utf-8")
    new_text = _replace_section_body(text, "## Files Touched", block)
    if new_text == text:
        return False
    progress.write_text(new_text, encoding="utf-8")
    return True


def _replace_section_body(text: str, heading: str, body: str) -> str:
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
            # Skip the existing section body
            while i < len(lines):
                nxt = lines[i]
                stripped = nxt.lstrip()
                if stripped.startswith("#"):
                    n_hash = len(stripped) - len(stripped.lstrip("#"))
                    if n_hash <= heading_level:
                        break
                i += 1
            # Insert new body
            out.append("")
            out.extend(body.splitlines())
            out.append("")
            continue
        i += 1
    if not found:
        sep = "" if text.endswith("\n") else "\n"
        return text + f"{sep}\n{heading}\n\n{body}\n"
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


def bump_heartbeat(cwd: Path) -> None:
    """Fire-and-forget heartbeat. See post-tool-bash.py for rationale."""
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


def _maybe_nudge_and_update_mechanical(
    project_root: Path,
    task_dir: Path,
    transcript_path: str,
) -> Optional[str]:
    """v0.5 PostToolUse extension. See post-tool-bash.py for prose."""
    try:
        pct, conf = estimate_context_pct(transcript_path)
        if pct is None:
            return None

        window_id = derive_window_id(task_dir.name)
        nudge_text = maybe_nudge_text(
            task_slug=task_dir.name, pct=pct, confidence=conf,
            window_id=window_id, min_seconds_between=60,
        )

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
                project_root=project_root, task_dir=task_dir,
                trigger="post-tool", transcript_path=transcript_path,
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
        return None


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = hook_input.get("tool_name") or hook_input.get("tool", "")
    tool_input = hook_input.get("tool_input", {})
    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()

    bump_heartbeat(cwd)

    transcript_path = hook_input.get("transcript_path")
    nudge_text: Optional[str] = None
    project_root_v05 = find_project_root(cwd)
    if project_root_v05 is not None:
        task_dir_v05 = find_active_task(project_root_v05)
        if task_dir_v05 is not None and transcript_path:
            nudge_text = _maybe_nudge_and_update_mechanical(
                project_root=project_root_v05,
                task_dir=task_dir_v05,
                transcript_path=transcript_path,
            )

    file_path = extract_file_path(tool_name, tool_input)
    if not file_path:
        if nudge_text:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": nudge_text,
                }
            }, ensure_ascii=False), flush=True)
        sys.exit(0)

    project_root = find_project_root(cwd)
    if project_root is None:
        if nudge_text:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": nudge_text,
                }
            }, ensure_ascii=False), flush=True)
        sys.exit(0)

    task = find_active_task(project_root)
    if task is None:
        if nudge_text:
            print(json.dumps({
                "hookSpecificOutput": {
                    "hookEventName": "PostToolUse",
                    "additionalContext": nudge_text,
                }
            }, ensure_ascii=False), flush=True)
        sys.exit(0)

    log = touched_log_path(cwd)
    state_path = flush_state_path(cwd)
    state = read_flush_state(state_path)

    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "tool": tool_name,
        "path": file_path,
    }
    append_record(log, record)
    state["unflushed_count"] = int(state.get("unflushed_count", 0)) + 1

    now_epoch = time.time()
    if should_flush(state, now_epoch):
        recent = collect_recent_files(log, project_root, LAST_N_FILES_IN_PROGRESS)
        block = render_files_block(recent)
        upsert_files_section(task / "progress.md", block)
        state["last_flush_epoch"] = now_epoch
        state["unflushed_count"] = 0

    write_flush_state(state_path, state)

    if nudge_text:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": nudge_text,
            }
        }
        print(json.dumps(output, ensure_ascii=False), flush=True)
    sys.exit(0)


if __name__ == "__main__":
    main()
