"""Build the mechanical.json payload from existing data sources.

Used by PreCompact hook AND PostToolUse extension so the two paths produce
identical schemas. Zero LLM cost — pure data extraction.
"""
from __future__ import annotations

import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

from .context_estimator import estimate_context_pct

SCHEMA_VERSION = 1
RECENT_COMMITS_LIMIT = 5
RECENT_FILES_LIMIT = 10

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def build_payload(
    project_root: Path,
    task_dir: Path,
    trigger: str,
    transcript_path: Optional[str | Path],
    recent_files: Optional[list[str]] = None,
) -> dict:
    """Compose mechanical state. `trigger` is e.g. 'precompact' or 'post-tool'."""
    pct, conf = estimate_context_pct(transcript_path) if transcript_path else (None, "low")
    transcript_size = 0
    if transcript_path:
        try:
            transcript_size = Path(transcript_path).stat().st_size
        except OSError:
            pass

    return {
        "schema_version": SCHEMA_VERSION,
        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
        "trigger": trigger,
        "task_slug": task_dir.name,
        "phase": _read_phase(task_dir),
        "git": _git_state(project_root),
        "files_touched_recent": (recent_files or [])[:RECENT_FILES_LIMIT],
        "context_pct_estimated": pct if pct is not None else 0,
        "transcript_path_size_bytes": transcript_size,
        "estimator_confidence": conf,
    }


def _read_phase(task_dir: Path) -> str:
    pmd = task_dir / "progress.md"
    if not pmd.is_file():
        return "unknown"
    text = pmd.read_text(encoding="utf-8", errors="replace")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return "unknown"
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            if k.strip() == "phase":
                return v.strip().strip('"').strip("'") or "unknown"
    return "unknown"


def _git_state(project_root: Path) -> dict:
    """Best-effort git state. Returns sane defaults on any failure."""
    out = {
        "branch": "unknown",
        "head": "unknown",
        "dirty_files": 0,
        "recent_commits": [],
    }
    if not (project_root / ".git").exists():
        return out
    try:
        out["branch"] = subprocess.check_output(
            ["git", "-C", str(project_root), "branch", "--show-current"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip() or "unknown"
        out["head"] = subprocess.check_output(
            ["git", "-C", str(project_root), "rev-parse", "--short", "HEAD"],
            text=True, stderr=subprocess.DEVNULL, timeout=3,
        ).strip() or "unknown"
        status = subprocess.check_output(
            ["git", "-C", str(project_root), "status", "--porcelain"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        out["dirty_files"] = sum(1 for line in status.splitlines() if line.strip())
        log = subprocess.check_output(
            ["git", "-C", str(project_root), "log",
             f"-{RECENT_COMMITS_LIMIT}", "--pretty=%h\t%s"],
            text=True, stderr=subprocess.DEVNULL, timeout=5,
        )
        for ln in log.splitlines():
            if "\t" in ln:
                h, s = ln.split("\t", 1)
                out["recent_commits"].append({"hash": h.strip(), "subject": s.strip()})
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError, OSError):
        pass
    return out
