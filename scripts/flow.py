#!/usr/bin/env python3
"""flow — CLI dispatcher for Flow Framework.

Usage:
  flow init              -- bootstrap .flow/ in current project
  flow task <subcmd>     -- task lifecycle (create / start / current / finish / archive / list)
  flow save              -- save current task progress
  flow triage <desc>     -- heuristic classify a task
  flow staleness         -- (stub) check stale memory
  flow promote           -- (stub) promote knowledge between tiers
  flow version           -- show version
"""
from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path


SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent


def show_version():
    version_file = REPO_ROOT / "VERSION"
    if version_file.is_file():
        print(f"flow {version_file.read_text().strip()}")
    else:
        print("flow (unknown version)")


def show_help():
    print(__doc__)


def main():
    if len(sys.argv) < 2:
        show_help()
        return

    cmd = sys.argv[1]
    rest = sys.argv[2:]

    routing = {
        "init": "flow_init.py",
        "task": "flow_task.py",
        "save": "flow_save.py",
        "triage": "flow_triage.py",
        "staleness": "flow_staleness.py",
        "promote": "flow_promote.py",
    }

    if cmd in ("version", "--version", "-v"):
        show_version()
    elif cmd in ("help", "--help", "-h"):
        show_help()
    elif cmd in routing:
        target = SCRIPTS / routing[cmd]
        if not target.is_file():
            print(f"ERROR: {target} not implemented yet (stub)", file=sys.stderr)
            sys.exit(2)
        sys.exit(subprocess.call([sys.executable, str(target), *rest]))
    else:
        print(f"Unknown command: {cmd}", file=sys.stderr)
        show_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
