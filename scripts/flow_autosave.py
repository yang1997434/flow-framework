#!/usr/bin/env python3
"""flow_autosave — Layer 2 semantic checkpoint orchestrator (Lv1 / Lv2 / Lv3).

Three tiers (event-driven, no timer-based polling):

  Lv1  trickle      git commit / Edit / Write batch   append to progress.md
                                                       (handled directly in
                                                        post-tool-bash.py and
                                                        post-tool-edit.py)
  Lv2  phase switch model + LLM template               not in this script (lives
                                                       in flow_promote.py / phase
                                                       commands)
  Lv3  full distill /flow:pause | /flow:finish |       THIS SCRIPT
                    Stop hook | PreCompact

Heartbeat fallback for Lv3: > 30 min since last distill AND > 50 tool calls
since last distill -> queue lightweight distill.

CRITICAL: Hooks must NOT call an LLM directly (timeout/cost risk). Instead we
write a "distill queued" marker to the active task's progress.md
`## Sediment Notes` section, plus a structured queue file under
`~/.flow/.runtime/distill-queue.jsonl`. SessionStart will surface pending
distills to Claude/the user, who can then run the actual LLM distillation
through the appropriate slash command (/flow:pause | /flow:finish | manual).

Future LLM path (NOT done in hook): see Sediment Notes — the distill prompt
should read the queue, look at last N events from progress.md + recent commits
+ context-mode raw transcripts, produce a 200-400 token summary, and append to
progress.md `## Sediment Notes`. Candidate dispatchers: /flow:save command, a
SessionStart bootstrap action, or an explicit `flow distill --run` CLI subcommand.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# Optional config knobs (defaults sane). We do NOT force YAML parsing here
# (stdlib-only, configurable through env or fall back to constants).
DEFAULT_DISTILL_COOLDOWN_MIN = 5
DEFAULT_HEARTBEAT_MIN = 30
DEFAULT_HEARTBEAT_TOOL_CALLS = 50

VALID_TRIGGERS = ("pause", "finish", "stop", "precompact", "heartbeat", "manual")


# ----- runtime state files (under ~/.flow/.runtime/) -----------------------

def runtime_dir() -> Path:
    """`~/.flow/.runtime/` (created on demand). Override via FLOW_HOME."""
    home = os.environ.get("FLOW_HOME")
    base = Path(home) if home else Path.home() / ".flow"
    rt = base / ".runtime"
    rt.mkdir(parents=True, exist_ok=True)
    return rt


def last_distill_path() -> Path:
    return runtime_dir() / "last-distill.txt"


def distill_queue_path() -> Path:
    return runtime_dir() / "distill-queue.jsonl"


def tool_count_path() -> Path:
    return runtime_dir() / "tool-count.txt"


def cwd_hash(cwd: Path) -> str:
    return hashlib.sha1(str(cwd.resolve()).encode("utf-8")).hexdigest()[:12]


def touched_log_path(cwd: Path) -> Path:
    return runtime_dir() / f"touched-{cwd_hash(cwd)}.log"


# ----- task discovery (mirrors common.paths but tolerant of any cwd) -------

def find_active_task(start: Path) -> Path | None:
    """Walk upward from `start` to find a `.flow/.current-task` pointer, or
    a `.flow/` dir with a single in-progress task (best-effort)."""
    cur = start.resolve()
    while cur != cur.parent:
        flow = cur / ".flow"
        if flow.is_dir():
            ptr = flow / ".current-task"
            if ptr.is_file():
                raw = ptr.read_text(encoding="utf-8").strip()
                if raw:
                    p = Path(raw)
                    if not p.is_absolute():
                        p = flow.parent / p
                    if p.is_dir():
                        return p
            return None
        cur = cur.parent
    return None


# ----- cooldown ------------------------------------------------------------

def read_last_distill() -> dict:
    p = last_distill_path()
    if not p.is_file():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def write_last_distill(trigger: str, ts: datetime) -> None:
    payload = {
        "trigger": trigger,
        "ts": ts.isoformat(),
        "ts_epoch": int(ts.timestamp()),
    }
    last_distill_path().write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def cooldown_active(now: datetime, cooldown_min: int) -> tuple[bool, int]:
    """Return (is_in_cooldown, seconds_since_last)."""
    last = read_last_distill()
    if not last:
        return (False, 10**9)
    last_epoch = last.get("ts_epoch", 0)
    delta = int(now.timestamp()) - int(last_epoch)
    return (delta < cooldown_min * 60, delta)


# ----- tool-count accounting (used for heartbeat) --------------------------

def read_tool_count() -> int:
    p = tool_count_path()
    if not p.is_file():
        return 0
    try:
        return int(p.read_text(encoding="utf-8").strip() or "0")
    except (ValueError, OSError):
        return 0


def increment_tool_count(by: int = 1) -> int:
    """Increment cumulative tool-call counter. Best-effort; on race we just
    overwrite (counter is only used as a coarse heartbeat trigger)."""
    cur = read_tool_count() + by
    try:
        tool_count_path().write_text(str(cur), encoding="utf-8")
    except OSError:
        pass
    return cur


def reset_tool_count() -> None:
    try:
        tool_count_path().write_text("0", encoding="utf-8")
    except OSError:
        pass


# ----- progress.md "Sediment Notes" append ---------------------------------

def append_distill_marker(task_dir: Path, trigger: str, now: datetime, note: str = "") -> bool:
    """Append a "distill queued" marker to progress.md `## Sediment Notes`.
    Returns True on write, False on no-op (e.g., file missing)."""
    progress = task_dir / "progress.md"
    if not progress.is_file():
        return False

    text = progress.read_text(encoding="utf-8")
    timestamp = now.strftime("%Y-%m-%d %H:%M")
    marker = (
        f"- [{timestamp}] distill queued (trigger={trigger})"
        + (f" — {note}" if note else "")
    )

    if "## Sediment Notes" in text:
        # Insert after the Sediment Notes heading, preserving the rest.
        new_text = _append_under_heading(text, "## Sediment Notes", marker)
    else:
        # Append a fresh section at end.
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + f"{sep}\n## Sediment Notes\n\n{marker}\n"

    progress.write_text(new_text, encoding="utf-8")
    return True


def _append_under_heading(text: str, heading: str, line: str) -> str:
    """Append `line` to the section started by `heading`, just before the next
    heading-of-equal-or-higher-level (or EOF). Strips placeholder template
    comments so we don't double-write under the noise."""
    lines = text.splitlines(keepends=False)
    out: list[str] = []
    i = 0
    found = False
    inserted = False
    heading_level = heading.count("#")
    while i < len(lines):
        out.append(lines[i])
        if not found and lines[i].strip() == heading:
            found = True
            i += 1
            # Collect section body until the next heading of same/higher level
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
            # Drop placeholder template comments
            cleaned = [
                ln for ln in section
                if not ln.strip().startswith("<!-- TEMPLATE")
            ]
            # Trim trailing blanks for tidy append
            while cleaned and not cleaned[-1].strip():
                cleaned.pop()
            # Trim leading blanks for cleanliness
            while cleaned and not cleaned[0].strip():
                cleaned.pop(0)
            # Compose: heading already in `out`, then blank, then cleaned, then new line, then blank
            if cleaned:
                out.append("")
                out.extend(cleaned)
            out.append("")
            out.append(line)
            out.append("")
            inserted = True
            continue
        i += 1
    if not found:
        # Caller should have detected this; defensive fallback.
        sep = "" if text.endswith("\n") else "\n"
        return text + f"{sep}\n{heading}\n\n{line}\n"
    # Strip a trailing blank to avoid pile-up over many writes
    while out and not out[-1].strip():
        out.pop()
    return "\n".join(out) + "\n"


# ----- distill queue (durable, machine-readable) ---------------------------

def enqueue_distill(trigger: str, task_dir: Path | None, now: datetime, extra: dict | None = None) -> None:
    """Append a structured record to `~/.flow/.runtime/distill-queue.jsonl`.
    SessionStart can read this file to surface "you have N pending distills"."""
    rec = {
        "trigger": trigger,
        "ts": now.isoformat(),
        "task": str(task_dir) if task_dir else None,
        "extra": extra or {},
    }
    try:
        with distill_queue_path().open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError:
        pass  # best-effort; not critical


# ----- subcommands ---------------------------------------------------------

def cmd_distill(args) -> int:
    """Lv3 entry point. Trigger types:
       pause | finish | stop | precompact | heartbeat | manual"""
    trigger = args.trigger
    if trigger not in VALID_TRIGGERS:
        print(f"ERROR: invalid trigger {trigger!r}", file=sys.stderr)
        return 2

    cooldown_min = args.cooldown_minutes if args.cooldown_minutes is not None \
        else DEFAULT_DISTILL_COOLDOWN_MIN

    now = datetime.now(timezone.utc).astimezone()

    in_cooldown, seconds_since = cooldown_active(now, cooldown_min)
    # finish/pause are user-initiated -> bypass cooldown (they're explicit)
    bypass = trigger in ("pause", "finish", "manual")
    if in_cooldown and not bypass:
        if args.verbose:
            print(
                f"[autosave] distill cooldown active "
                f"({seconds_since}s < {cooldown_min*60}s) — skipping",
                file=sys.stderr,
            )
        return 0

    cwd = Path(args.cwd or os.getcwd()).resolve()
    task = find_active_task(cwd)

    appended = False
    if task is not None:
        appended = append_distill_marker(
            task, trigger, now, note=args.note or ""
        )

    enqueue_distill(trigger, task, now, extra={"appended": appended})
    write_last_distill(trigger, now)
    # Reset tool count so next heartbeat starts fresh
    reset_tool_count()

    if args.verbose:
        where = task or "(no active task)"
        print(f"[autosave] distill queued trigger={trigger} task={where}")
    return 0


def cmd_heartbeat(args) -> int:
    """Increment tool count; if heartbeat thresholds met, queue a heartbeat distill."""
    heartbeat_min = args.heartbeat_minutes if args.heartbeat_minutes is not None \
        else DEFAULT_HEARTBEAT_MIN
    heartbeat_calls = args.heartbeat_tool_calls if args.heartbeat_tool_calls is not None \
        else DEFAULT_HEARTBEAT_TOOL_CALLS

    count = increment_tool_count(by=args.increment)
    now = datetime.now(timezone.utc).astimezone()
    last = read_last_distill()
    last_epoch = last.get("ts_epoch", 0)
    delta = int(now.timestamp()) - int(last_epoch)

    triggered = (delta >= heartbeat_min * 60) and (count >= heartbeat_calls)
    if not triggered:
        if args.verbose:
            print(
                f"[autosave] heartbeat not met "
                f"(delta={delta}s/{heartbeat_min*60}s, "
                f"calls={count}/{heartbeat_calls})",
                file=sys.stderr,
            )
        return 0

    # Queue heartbeat distill (always, ignore cooldown — heartbeat already
    # implies last-distill > 30 min, which exceeds the 5 min cooldown).
    cwd = Path(args.cwd or os.getcwd()).resolve()
    task = find_active_task(cwd)
    appended = False
    if task is not None:
        appended = append_distill_marker(
            task, "heartbeat", now, note=f"after {count} tool calls"
        )
    enqueue_distill("heartbeat", task, now,
                    extra={"appended": appended, "tool_calls": count})
    write_last_distill("heartbeat", now)
    reset_tool_count()
    if args.verbose:
        print(f"[autosave] heartbeat distill queued (calls={count})")
    return 0


def cmd_status(args) -> int:
    """Dump runtime state for debugging / SessionStart readout."""
    last = read_last_distill()
    queue = distill_queue_path()
    pending = []
    if queue.is_file():
        try:
            for line in queue.read_text(encoding="utf-8").splitlines():
                if line.strip():
                    pending.append(json.loads(line))
        except (OSError, json.JSONDecodeError):
            pass
    payload = {
        "last_distill": last,
        "tool_count": read_tool_count(),
        "queue_size": len(pending),
        "queue_tail": pending[-3:],
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


def main():
    parser = argparse.ArgumentParser(description="Flow Layer-2 autosave orchestrator")
    parser.add_argument("--verbose", "-v", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_dist = sub.add_parser("distill", help="Queue an Lv3 distill (no LLM call)")
    p_dist.add_argument("--trigger", required=True, choices=list(VALID_TRIGGERS))
    p_dist.add_argument("--cwd", help="cwd hint (default: $PWD)")
    p_dist.add_argument("--note", default="", help="Free-form note appended to marker")
    p_dist.add_argument("--cooldown-minutes", type=int, default=None)
    p_dist.set_defaults(func=cmd_distill)

    p_hb = sub.add_parser("heartbeat", help="Increment tool count + maybe queue heartbeat distill")
    p_hb.add_argument("--cwd", help="cwd hint (default: $PWD)")
    p_hb.add_argument("--increment", type=int, default=1)
    p_hb.add_argument("--heartbeat-minutes", type=int, default=None)
    p_hb.add_argument("--heartbeat-tool-calls", type=int, default=None)
    p_hb.set_defaults(func=cmd_heartbeat)

    sub.add_parser("status", help="Print runtime state").set_defaults(func=cmd_status)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
