"""flow wave planner — decompose progress.md into wave structure.

Phase 1 (this commit): Parse `### Tasks` YAML block from progress.md.
Phase 2 (next commits): independence algorithm, cache, CLI.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


class PlanError(ValueError):
    """Raised when progress.md `### Tasks` YAML block is malformed."""


@dataclass
class Task:
    id: str
    writes: Optional[list[str]] = None  # None means undeclared → strict serial
    reads: list[str] = field(default_factory=list)
    description: str = ""


# Match: ### Tasks header, then ```yaml ... ``` fence
_TASKS_BLOCK_RE = re.compile(
    r"^###\s+Tasks\s*$.*?^```yaml\s*$(.*?)^```\s*$",
    re.MULTILINE | re.DOTALL,
)


def _parse_task_yaml(yaml_text: str) -> dict:
    """Minimal YAML parser for the task list format.

    Handles:
    - Top-level key: tasks:
    - List items starting with `- key: value`
    - Continuation key: value lines within a list item
    - Inline lists: [item1, item2]
    - Quoted strings (single or double)

    Raises PlanError on structural errors (e.g. unclosed inline list).
    """
    lines = yaml_text.splitlines()
    result: dict = {}
    current_list: list | None = None
    current_item: dict | None = None

    def _parse_value(raw: str) -> object:
        """Parse a scalar or inline list value."""
        raw = raw.strip()
        if raw.startswith("["):
            # Inline list — must be closed on same line
            if not raw.endswith("]"):
                raise PlanError(f"Unclosed inline list: {raw!r}")
            inner = raw[1:-1].strip()
            if not inner:
                return []
            items = [_unquote(x.strip()) for x in inner.split(",") if x.strip()]
            return items
        # Scalar
        return _unquote(raw)

    def _unquote(s: str) -> str:
        if (s.startswith('"') and s.endswith('"')) or (
            s.startswith("'") and s.endswith("'")
        ):
            return s[1:-1]
        return s

    for lineno, line in enumerate(lines, 1):
        # Strip inline comments (but don't strip inside quoted strings — close enough for our format)
        stripped = re.sub(r"\s+#.*$", "", line).rstrip()
        if not stripped.strip():
            continue

        indent = len(stripped) - len(stripped.lstrip())
        content = stripped.strip()

        # Top-level key (e.g. `tasks:`)
        if indent == 0 and ":" in content and not content.startswith("-"):
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            # New top-level key — always reset the active list-item context so
            # any subsequent indented `key: value` lines don't bleed into the
            # previously open task. (Issue #3 fix.)
            current_item = None
            if not val:
                # Start of a block value (list or dict follows)
                current_list = []
                result[key] = current_list
            else:
                current_list = None
                result[key] = _parse_value(val)
            continue

        # List item starting with `-`
        if content.startswith("-"):
            if current_list is None:
                raise PlanError(f"Line {lineno}: list item outside of any key context")
            item_content = content[1:].strip()
            current_item = {}
            current_list.append(current_item)
            if ":" in item_content:
                key, _, val = item_content.partition(":")
                key = key.strip()
                val = val.strip()
                current_item[key] = _parse_value(val) if val else None
            continue

        # Continuation key: value inside a list item
        if current_item is not None and ":" in content and not content.startswith("-"):
            key, _, val = content.partition(":")
            key = key.strip()
            val = val.strip()
            current_item[key] = _parse_value(val) if val else None
            continue

    return result


def parse_plan_tasks(progress_md_text: str) -> list[Task]:
    """Extract task list from progress.md. Empty list if no `### Tasks` block."""
    match = _TASKS_BLOCK_RE.search(progress_md_text)
    if not match:
        return []
    yaml_text = match.group(1)
    data = _parse_task_yaml(yaml_text)

    if not data or "tasks" not in data:
        return []
    tasks_data = data["tasks"]
    if not isinstance(tasks_data, list):
        raise PlanError("`tasks:` must be a list")

    out: list[Task] = []
    for i, t in enumerate(tasks_data):
        if not isinstance(t, dict) or "id" not in t:
            raise PlanError(f"task #{i} missing required `id` field")
        writes_raw = t.get("writes")  # None if absent
        writes: Optional[list[str]]
        if writes_raw is None:
            writes = None
        elif isinstance(writes_raw, list):
            writes = [str(w) for w in writes_raw]
        else:
            writes = [str(writes_raw)]
        reads_raw = t.get("reads") or []
        reads = [str(r) for r in reads_raw] if isinstance(reads_raw, list) else [str(reads_raw)]
        out.append(Task(
            id=str(t["id"]),
            writes=writes,
            reads=reads,
            description=str(t.get("description") or ""),
        ))
    return out


# ---------------------------------------------------------------------------
# SHARED_ARTIFACTS loader + overlap check (Phase 2 / T4)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parent.parent
SHARED_ARTIFACTS_FILE = (
    REPO_ROOT / "claude" / "skills" / "flow" / "flow-wave-runner" / "SHARED_ARTIFACTS.md"
)

# Match the shared_artifacts: YAML block in the markdown file
_SHARED_BLOCK_RE = re.compile(
    r"```yaml\s*\n(shared_artifacts:.*?)\n```",
    re.DOTALL,
)

# Regex to extract individual glob entries from the flat list.
# Decision: use a focused line-regex rather than _parse_task_yaml (which
# expects dict-shaped items) or a full YAML parser (optional dep). The
# SHARED_ARTIFACTS format is a fixed flat list of quoted/unquoted strings —
# a single regex over lines is simplest and has no dependencies.
_GLOB_LINE_RE = re.compile(r"^\s*-\s*\"?([^\"\n]+?)\"?\s*$", re.MULTILINE)


def load_shared_artifacts() -> list[str]:
    """Parse SHARED_ARTIFACTS.md and return the glob list.

    The file contains a yaml block::

        ```yaml
        shared_artifacts:
          # comment
          - "**/package.json"
          ...
        ```

    Returns an empty list if the file is missing or the block is absent.
    """
    if not SHARED_ARTIFACTS_FILE.is_file():
        return []
    text = SHARED_ARTIFACTS_FILE.read_text(encoding="utf-8")
    m = _SHARED_BLOCK_RE.search(text)
    if not m:
        return []
    yaml_text = m.group(1)
    # Extract all `- "glob"` / `- glob` entries via regex (see _GLOB_LINE_RE).
    return _GLOB_LINE_RE.findall(yaml_text)


def wave_touches_shared(tasks: list[Task]) -> bool:
    """Return True if any task's writes overlaps SHARED_ARTIFACTS globs."""
    _common = str(REPO_ROOT / "scripts" / "common")
    if _common not in sys.path:
        sys.path.insert(0, _common)
    from glob_overlap import globs_overlap  # type: ignore  # noqa: PLC0415

    shared = load_shared_artifacts()
    if not shared:
        return False
    for task in tasks:
        if task.writes is None:
            continue
        if globs_overlap(task.writes, shared):
            return True
    return False
