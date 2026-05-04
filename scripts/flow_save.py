#!/usr/bin/env python3
"""flow save — auto-save current task progress to journal + memory pointer.

Usage:
  flow_save.py [--task PATH] [--note "summary"]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import get_current_task_path, get_user_workspace, get_machine_id
from common.git import get_recent_commits, is_dirty


def main():
    parser = argparse.ArgumentParser(description="Auto-save current task progress")
    parser.add_argument("--task", help="Task dir (default: active)")
    parser.add_argument("--note", default="", help="Free-form note")
    parser.add_argument("--no-commit", action="store_true", help="Don't auto-commit journal")
    args = parser.parse_args()

    task_path = Path(args.task) if args.task else get_current_task_path()
    if task_path is None or not task_path.is_dir():
        print("(no active task to save)", file=sys.stderr)
        sys.exit(1)

    workspace = get_user_workspace()
    workspace.mkdir(parents=True, exist_ok=True)
    journal = workspace / "journal.md"

    # Read task metadata
    prd_path = task_path / "prd.md"
    title = "(no title)"
    if prd_path.is_file():
        for line in prd_path.read_text(encoding="utf-8").splitlines():
            if line.startswith("# "):
                title = line[2:].strip()
                break

    # Compose journal entry
    now = datetime.now()
    machine = get_machine_id()
    commits = get_recent_commits(3)
    dirty = is_dirty()

    entry = [
        "",
        f"## {task_path.name} — {now.strftime('%Y-%m-%d %H:%M')}",
        f"- Title: {title}",
        f"- Machine: {machine}",
        f"- Status: {'dirty tree' if dirty else 'clean'}",
    ]
    if commits:
        entry.append(f"- Recent commits: {', '.join(c[0] for c in commits[:3])}")
    if args.note:
        entry.append(f"- Note: {args.note}")
    entry.append("")

    # Append to journal
    if journal.exists():
        existing = journal.read_text(encoding="utf-8")
    else:
        existing = "# Journal\n"
    journal.write_text(existing + "\n".join(entry), encoding="utf-8")

    print(f"Saved to {journal}")

    # Optional: commit journal (only the journal file, not the project)
    if not args.no_commit and not dirty:
        # Don't auto-commit if user has dirty work — could mix concerns
        # If clean, journal commit is safe
        try:
            import subprocess
            subprocess.run(
                ["git", "add", str(journal)],
                check=True, capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"chore: flow journal — {task_path.name}"],
                check=True, capture_output=True,
            )
            print(f"Committed journal entry.")
        except subprocess.CalledProcessError:
            pass  # Not a git repo, or nothing to commit, ignore


if __name__ == "__main__":
    main()
