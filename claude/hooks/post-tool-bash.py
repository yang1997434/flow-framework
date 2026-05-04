#!/usr/bin/env python3
"""PostToolUse(Bash) hook — credential grep after git commit.

Reads JSON from stdin. Detects if the bash command contained `git commit`,
and if so, runs a credential grep across .flow/ and vault patterns.

If matches found: outputs warning into context (model sees it, can act).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path


CREDENTIAL_PATTERN = (
    r"(password|secret|api[_-]?key|token|bearer).*[:=]\s*['\"][^'\"]{4,}['\"]"
)


def find_project_root(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir() or (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return None


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    command = tool_input.get("command", "")

    # Only act on git commit
    if "git commit" not in command:
        sys.exit(0)

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    project_root = find_project_root(cwd)
    if project_root is None:
        sys.exit(0)

    # Run grep
    targets = []
    flow = project_root / ".flow"
    if flow.is_dir():
        targets.append(str(flow))
    vault = Path.home() / "data" / "knowledge-base"
    if vault.is_dir():
        targets.append(str(vault))

    if not targets:
        sys.exit(0)

    try:
        result = subprocess.run(
            ["grep", "-rEn", "--include=*.md", "--include=*.yaml", "--include=*.yml",
             CREDENTIAL_PATTERN, *targets],
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        sys.exit(0)

    matches = result.stdout.strip()
    if not matches:
        sys.exit(0)

    warning = (
        "<flow-credential-warning>\n"
        "⚠️ POSSIBLE credential leak detected after git commit. Review:\n"
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
