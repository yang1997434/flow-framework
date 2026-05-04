#!/usr/bin/env python3
"""PreToolUse(Task/Agent) hook — inject task-specific spec context into sub-agents.

Trellis-style mechanism: when a sub-agent is dispatched, read the active task's
`implement.jsonl` or `check.jsonl` and inject the referenced spec files into
the sub-agent's prompt.

JSONL format (per line):
  {"file": "<path-relative-to-repo-root>", "reason": "<why-needed>"}

Heuristic to pick implement vs check:
  - Prompt contains "verify" / "check" / "review" → use check.jsonl
  - Prompt contains "implement" / "write" / "build" → use implement.jsonl
  - Default: implement.jsonl

Best-effort: if jsonl missing or empty, exit silently.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path


MAX_INJECTED_BYTES = 50 * 1024  # 50 KB cap on total injected content
MAX_PER_FILE_BYTES = 10 * 1024  # 10 KB per file


def find_project_flow(start: Path) -> Path | None:
    cur = start.resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir():
            return cur / ".flow"
        cur = cur.parent
    return None


def get_active_task(flow: Path) -> Path | None:
    pointer = flow / ".current-task"
    if not pointer.is_file():
        return None
    rel = pointer.read_text(encoding="utf-8").strip()
    if not rel:
        return None
    p = Path(rel)
    if not p.is_absolute():
        p = flow.parent / p
    return p if p.is_dir() else None


def pick_jsonl(task_dir: Path, prompt: str) -> Path | None:
    check_keywords = re.compile(r"\b(verify|check|review|audit|test|lint)\b", re.IGNORECASE)
    impl_keywords = re.compile(r"\b(implement|write|build|fix|refactor|add)\b", re.IGNORECASE)

    if check_keywords.search(prompt):
        target = task_dir / "check.jsonl"
        if target.is_file():
            return target

    if impl_keywords.search(prompt) or True:  # default
        target = task_dir / "implement.jsonl"
        if target.is_file():
            return target

    return None


def parse_jsonl(jsonl_path: Path) -> list[dict]:
    entries = []
    try:
        for line in jsonl_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict) and row.get("file"):
                entries.append(row)
    except (OSError, UnicodeDecodeError):
        pass
    return entries


def load_spec_content(repo_root: Path, file_rel: str) -> str | None:
    """Read a referenced spec file, return content or None if missing."""
    candidates = [
        repo_root / file_rel,
        Path(file_rel).expanduser() if Path(file_rel).is_absolute() else None,
    ]
    for c in candidates:
        if c and c.is_file():
            try:
                content = c.read_text(encoding="utf-8", errors="replace")
                if len(content) > MAX_PER_FILE_BYTES:
                    content = content[:MAX_PER_FILE_BYTES] + "\n... (truncated by hook)"
                return content
            except OSError:
                pass
    return None


def main():
    try:
        hook_input = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError:
        sys.exit(0)

    tool_name = hook_input.get("tool_name", "")
    if tool_name not in ("Task", "Agent"):
        sys.exit(0)

    tool_input = hook_input.get("tool_input", {})
    prompt = tool_input.get("prompt", "") or tool_input.get("description", "")

    cwd = Path(hook_input.get("cwd", os.getcwd())).resolve()
    flow = find_project_flow(cwd)
    if not flow:
        sys.exit(0)

    task_dir = get_active_task(flow)
    if not task_dir:
        sys.exit(0)

    jsonl = pick_jsonl(task_dir, prompt)
    if not jsonl:
        sys.exit(0)

    entries = parse_jsonl(jsonl)
    if not entries:
        sys.exit(0)

    repo_root = flow.parent
    parts = ["<flow-task-spec-context>"]
    parts.append(f"Loaded from: {jsonl.relative_to(repo_root) if jsonl.is_relative_to(repo_root) else jsonl}")
    parts.append("")

    total_bytes = 0
    for entry in entries:
        if total_bytes >= MAX_INJECTED_BYTES:
            parts.append(f"... ({len(entries)} entries, truncated at {MAX_INJECTED_BYTES // 1024} KB total)")
            break
        file_rel = entry["file"]
        reason = entry.get("reason", "")
        content = load_spec_content(repo_root, file_rel)
        parts.append(f"### {file_rel}")
        if reason:
            parts.append(f"*Reason*: {reason}")
        if content is None:
            parts.append("*(file not found — spec stale; flag in `/flow:resume` staleness check)*")
        else:
            parts.append("```")
            parts.append(content.rstrip())
            parts.append("```")
            total_bytes += len(content)
        parts.append("")

    parts.append("</flow-task-spec-context>")

    output = {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "additionalContext": "\n".join(parts),
        }
    }
    print(json.dumps(output, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
