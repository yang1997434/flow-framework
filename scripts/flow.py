#!/usr/bin/env python3
"""flow — CLI dispatcher for Flow Framework.

Usage:
  flow init              -- bootstrap .flow/ in current project
  flow task <subcmd>     -- task lifecycle (create / start / current / finish / archive / list / phase)
  flow save              -- save current task progress
  flow triage <desc>     -- heuristic classify a task
  flow staleness         -- check stale memory references
  flow conflict          -- detect rule/ADR conflicts (heuristic)
  flow promote           -- promote knowledge between tiers
  flow sediment <type>   -- render pitfall/pattern/ADR template + link to active task
  flow install <subcmd>  -- declarative install (check-system / register-marketplaces / install-plugins / install-hooks / all)
  flow doctor            -- environment consistency diagnostic (static)
  flow selftest [scope]  -- functional verification (dynamic; scope: hooks/init/task/plugins/doctor/all)
  flow skill-diff <sub>  -- compare new plugins against capability registry (snapshot/diff/show/clear/reset-cache)
  flow waves <subcmd>    -- preview/inspect wave decomposition
                            (--preview / --show / --invalidate <slug>)
  flow contract <subcmd>  -- contract.json validate/init (--validate <slug> / --init <slug>)
  flow orchestrator <subcmd> -- dry-run preview / auto-execute
                               (--dry-run <slug> / --auto-execute <slug>)
  flow acceptance <subcmd> -- v0.8.1+ Phase 3 verify gate
                              (--run <slug> [--phase {2,3}])
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
        "conflict": "flow_conflict.py",
        "promote": "flow_promote.py",
        "sediment": "flow_sediment.py",
        "install": "flow_install.py",
        "doctor": "flow_doctor.py",
        "selftest": "flow_selftest.py",
        "skill-diff": "flow_skill_diff.py",
        "waves": "flow_waves.py",
        "contract": "flow_contract.py",   # NEW
        "orchestrator": "flow_orchestrator.py",  # NEW v0.8.0
        "acceptance": "flow_acceptance_cli.py",  # NEW v0.8.1 (T22)
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
