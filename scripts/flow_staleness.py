#!/usr/bin/env python3
"""flow staleness — check whether memories cite paths that have moved.

Usage:
  flow_staleness.py [--scope project|vault|all]

STUB: this is a v0.3.1 placeholder. The current implementation only does
basic file-existence verification — full staleness with git log analysis
and prompt-for-action is planned.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import get_flow_dir


# Match path-like strings (heuristic) referenced in memory files
PATH_PATTERN = re.compile(r"`([./\w-]+\.(?:py|ts|tsx|js|jsx|md|yaml|yml|json|sh|sql))`")


def scan_file(memory_file: Path, project_root: Path) -> list[tuple[str, bool]]:
    """Return [(path, exists), ...]."""
    text = memory_file.read_text(encoding="utf-8", errors="replace")
    paths = PATH_PATTERN.findall(text)
    results = []
    for p in set(paths):
        # Try project-relative
        candidate = project_root / p
        if candidate.is_file():
            results.append((p, True))
        elif Path(p).is_absolute() and Path(p).is_file():
            results.append((p, True))
        else:
            results.append((p, False))
    return results


def main():
    parser = argparse.ArgumentParser(description="Stale-memory check (basic)")
    parser.add_argument("--scope", choices=["project", "vault", "all"], default="project")
    args = parser.parse_args()

    flow = get_flow_dir()
    project_root = flow.parent if flow.is_dir() else Path.cwd()

    targets = []
    if args.scope in ("project", "all") and flow.is_dir():
        targets += list((flow / "ADRs").glob("*.md"))
        targets += list((flow / "patterns").glob("*.md"))
        targets += list((flow / "pitfalls").glob("*.md"))
    if args.scope in ("vault", "all"):
        vault = Path.home() / "data" / "knowledge-base"
        if vault.is_dir():
            targets += list((vault / "patterns").glob("*.md")) if (vault / "patterns").is_dir() else []
            targets += list((vault / "pitfalls").glob("*.md")) if (vault / "pitfalls").is_dir() else []

    if not targets:
        print("(no memory files to check)")
        return

    stale_count = 0
    for memory in targets:
        results = scan_file(memory, project_root)
        stale = [(p, e) for p, e in results if not e]
        if stale:
            print(f"STALE: {memory}")
            for p, _ in stale:
                print(f"  - {p}")
            stale_count += len(stale)

    print()
    if stale_count:
        print(f"{stale_count} stale path reference(s). Review and update or mark obsolete.")
    else:
        print("All references resolved.")


if __name__ == "__main__":
    main()
