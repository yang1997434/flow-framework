"""flow_state_writer — append-only state writers for autonomous mode.

Per the v0.8 design: only the orchestrator writes canonical state. Subagents
write to their worktree journals; orchestrator merges. v0.8.0 ships writers
only — no reads from these files yet (v0.8.2 wires read paths for resume).
"""
from __future__ import annotations

import datetime
import json
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent / "common"))
from safe_io import atomic_write_text, append_jsonl_locked


VALID_REVIEW_DISPOSITIONS = (
    "fixed", "rejected_with_rationale", "superseded", "escalated", "open",
)
VALID_SEVERITIES = ("critical", "high", "med", "low", "info")


@dataclass
class DecisionRecord:
    id: str
    ts: str
    phase: int
    task: str
    decision: str
    reason: str
    alternatives: list[str] = field(default_factory=list)
    files_affected: list[str] = field(default_factory=list)
    review_status: str = "pending"
    supersedes: Optional[str] = None


@dataclass
class ReviewIssueRecord:
    id: str
    ts: str
    task: str
    severity: str
    reviewer: str
    description: str
    disposition: str = "open"
    rationale: Optional[str] = None


def append_decision(task_dir: Path, rec: DecisionRecord) -> None:
    path = task_dir / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_locked(path, asdict(rec))


def append_review_issue(task_dir: Path, rec: ReviewIssueRecord) -> None:
    if rec.disposition not in VALID_REVIEW_DISPOSITIONS:
        raise ValueError(
            f"disposition must be in {VALID_REVIEW_DISPOSITIONS}, got {rec.disposition!r}"
        )
    if rec.severity not in VALID_SEVERITIES:
        raise ValueError(
            f"severity must be in {VALID_SEVERITIES}, got {rec.severity!r}"
        )
    path = task_dir / "review-issues.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    append_jsonl_locked(path, asdict(rec))


def write_checkpoint(
    task_dir: Path, ts: str, body: str, git_hash: Optional[str] = None,
) -> Path:
    """Atomic write a checkpoint markdown file. Filename uses safe-ts (no `:`).

    Returns the path. Caller is responsible for ts uniqueness; we don't
    overwrite a same-ts checkpoint silently — we error.
    """
    cp_dir = task_dir / "checkpoints"
    cp_dir.mkdir(parents=True, exist_ok=True)
    safe_ts = ts.replace(":", "-")
    path = cp_dir / f"{safe_ts}.md"
    if path.exists():
        raise FileExistsError(f"checkpoint already exists: {path}")
    header = f"---\nts: {ts}\ngit_hash: {git_hash or 'unknown'}\n---\n\n"
    atomic_write_text(path, header + body)
    return path


def write_blocked(
    task_dir: Path,
    phase: int,
    task: str,
    why_blocked: str,
    required_choice: list[str],
    safe_resume_command: str,
) -> Path:
    """Write transient blocked.md. Resume protocol clears it on success."""
    body = (
        f"---\n"
        f"phase: {phase}\n"
        f"task: {task}\n"
        f"why_blocked: {why_blocked}\n"
        f"required_choice: {json.dumps(required_choice)}\n"
        f"safe_resume_command: {safe_resume_command}\n"
        f"ts: {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%dT%H:%M:%SZ')}\n"
        f"---\n\n"
        f"# Blocked: {why_blocked}\n\n"
        f"Choices: {', '.join(required_choice)}\n\n"
        f"Resume: `{safe_resume_command}`\n"
    )
    path = task_dir / "blocked.md"
    atomic_write_text(path, body)
    return path
