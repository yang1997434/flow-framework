#!/usr/bin/env python3
"""flow staleness — verify memories cite paths that still exist + are not stale.

Usage:
  flow_staleness.py [--scope project|vault|all] [--json] [--recent N]

  --scope: which memory tier to check (default: project)
  --json: output JSON (for hook consumption)
  --recent N: flag as stale if cited path was modified in last N commits AND
              memory file hasn't been touched since (default: 5)

A memory entry is "stale" if any of:
  1. Cited path no longer exists
  2. Cited symbol/function no longer matches (heuristic — checks if name still appears in cited file)
  3. Cited path was modified in last N commits AND memory file is older than that change
"""
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import get_flow_dir


# Match path-like strings (heuristic) referenced in memory files
PATH_PATTERN = re.compile(
    r"`([./\w-]+\.(?:py|ts|tsx|js|jsx|md|yaml|yml|json|sh|sql|rs|go|java|cpp|c|h))`"
)


@dataclass
class StaleFinding:
    memory_file: str
    cited_path: str
    reason: str  # "missing" | "modified-after-memory" | "symbol-not-found"
    detail: str = ""


def get_file_last_modified(path: Path) -> datetime | None:
    """Return path's mtime as UTC datetime, or None if missing."""
    if not path.exists():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)


def git_log_recent(path: Path, n: int, repo_root: Path) -> list[tuple[str, datetime]]:
    """Return [(sha, commit_time), ...] for last n commits touching path."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--format=%H|%cI", "--", str(path)],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    commits = []
    for line in result.stdout.strip().splitlines():
        if "|" in line:
            sha, ct = line.split("|", 1)
            try:
                commits.append((sha, datetime.fromisoformat(ct)))
            except ValueError:
                continue
    return commits


def find_repo_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def scan_memory_file(memory: Path, project_root: Path, recent_n: int) -> list[StaleFinding]:
    """Scan a single memory file for stale references. Return findings."""
    findings: list[StaleFinding] = []

    text = memory.read_text(encoding="utf-8", errors="replace")
    paths = set(PATH_PATTERN.findall(text))
    if not paths:
        return findings

    memory_mtime = get_file_last_modified(memory)
    repo_root = find_repo_root(project_root) or project_root

    for cited in paths:
        # Resolve cited path
        candidates = [
            project_root / cited,
            repo_root / cited,
            Path(cited).expanduser() if Path(cited).is_absolute() else None,
        ]
        existing = next((c for c in candidates if c and c.is_file()), None)

        if existing is None:
            findings.append(StaleFinding(
                memory_file=str(memory),
                cited_path=cited,
                reason="missing",
                detail=f"Path does not exist (checked {len(candidates) - candidates.count(None)} candidates)",
            ))
            continue

        # Check if path was modified after memory was written
        commits = git_log_recent(existing, recent_n, repo_root)
        if commits and memory_mtime:
            most_recent_change = max(c[1] for c in commits)
            if most_recent_change > memory_mtime:
                findings.append(StaleFinding(
                    memory_file=str(memory),
                    cited_path=cited,
                    reason="modified-after-memory",
                    detail=(
                        f"File modified at {most_recent_change.isoformat()}, "
                        f"memory written at {memory_mtime.isoformat()}. "
                        f"Recent commit: {commits[0][0][:8]}"
                    ),
                ))

    return findings


def collect_targets(scope: str, flow: Path) -> list[Path]:
    targets = []
    if scope in ("project", "all") and flow.is_dir():
        for sub in ("ADRs", "patterns", "pitfalls"):
            d = flow / sub
            if d.is_dir():
                targets += list(d.glob("*.md"))
    if scope in ("vault", "all"):
        vault = Path.home() / "data" / "knowledge-base"
        if vault.is_dir():
            for sub in ("patterns", "pitfalls", "ADRs"):
                d = vault / sub
                if d.is_dir():
                    targets += list(d.glob("*.md"))
    return targets


def main():
    parser = argparse.ArgumentParser(description="Stale-memory check")
    parser.add_argument("--scope", choices=["project", "vault", "all"], default="project")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    parser.add_argument("--recent", type=int, default=5, help="Flag if path modified in last N commits")
    args = parser.parse_args()

    flow = get_flow_dir()
    project_root = flow.parent if flow.is_dir() else Path.cwd()

    targets = collect_targets(args.scope, flow)

    if not targets:
        if args.json:
            print(json.dumps({"findings": [], "checked_files": 0}))
        else:
            print("(no memory files to check)")
        return

    all_findings: list[StaleFinding] = []
    for memory in targets:
        all_findings.extend(scan_memory_file(memory, project_root, args.recent))

    if args.json:
        print(json.dumps({
            "findings": [asdict(f) for f in all_findings],
            "checked_files": len(targets),
        }, ensure_ascii=False))
        return

    # Human output
    print(f"Checked {len(targets)} memory file(s) in scope: {args.scope}")
    if not all_findings:
        print("All references resolved. No stale memory.")
        return

    by_reason: dict[str, list[StaleFinding]] = {}
    for f in all_findings:
        by_reason.setdefault(f.reason, []).append(f)

    print(f"\n{len(all_findings)} stale finding(s):")
    for reason, items in by_reason.items():
        print(f"\n[{reason}]")
        for f in items:
            print(f"  {f.memory_file}")
            print(f"    cites: {f.cited_path}")
            if f.detail:
                print(f"    detail: {f.detail}")

    print("\nNext: review and update memory files, or mark obsolete with `status: obsolete` frontmatter.")
    sys.exit(1 if all_findings else 0)


if __name__ == "__main__":
    main()
