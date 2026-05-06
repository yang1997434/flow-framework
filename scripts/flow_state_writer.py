"""flow_state_writer — append-only state writers for autonomous mode.

Per the v0.8 design: only the orchestrator writes canonical state. Subagents
write to their worktree journals; orchestrator merges. v0.8.0 ships writers
only — no reads from these files yet (v0.8.2 wires read paths for resume).

v0.8.1 (T4) adds `acceptance-progress.jsonl` — per-criterion lifecycle
events (`started` before invocation, `completed`/`timeout` after). Tail-read
drives in-flight resume in T9 (read path lives in flow_acceptance.py).
Concurrency model (design R3): only the orchestrator appends to canonical
jsonl; subagents never write here directly. Lock contention → audit gap →
log to stderr, proceed (mirrors v0.8.0 `append_decision` posture).
"""
from __future__ import annotations

import datetime
import hashlib
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

# T4 — acceptance-progress.jsonl enums (design §6 line 261 + Y7).
VALID_PROGRESS_EVENTS = ("started", "completed", "timeout")
VALID_PROGRESS_STATUSES = (
    "pass", "fail", "inconclusive", "interrupted", "timed_out",
)
VALID_PROGRESS_TYPES = (
    "unit", "integration", "e2e", "smoke", "behavior", "regression",
)
VALID_PROGRESS_METHODS = ("cmd", "file_exists", "json_query", "http")
# `idempotent` is a string (not bool) for forward-compat: T4 schema accepts
# the third value `"unknown"` per R8 hardening — bool would conflate with
# absent. See plan T4 §6 line 261.
VALID_PROGRESS_IDEMPOTENT = ("true", "false", "unknown")


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


# ----------------------------------------------------------------------
# T4 — acceptance-progress.jsonl: per-criterion lifecycle events.
#
# Per design §6 (line 261) + Y7. One `started` line written before the
# acceptance check is invoked; one `completed` (with status) OR `timeout`
# line written after. T9 tail-reads to drive in-flight resume; T4 is the
# producer side only.
# ----------------------------------------------------------------------


@dataclass
class AcceptanceProgressEvent:
    # Identity / context (always present).
    event_id: str
    ts: str
    slug: str
    task_id: str
    run_id: str
    worktree_id: str
    attempt_id: str
    retry_idx: int
    # Criterion identity (S2 + Y7).
    criterion_id: str
    criterion_idx: int
    criterion_hash: str
    type: str           # R5: orthogonal to method.
    method: str         # cmd | file_exists | json_query | http
    idempotent: str     # "true" | "false" | "unknown"
    # Lifecycle phase + timestamps.
    event: str          # started | completed | timeout
    started_at: str
    completed_at: Optional[str]
    timeout_sec: int
    # Outcome — null on `started`, populated on `completed`/`timeout` (S3).
    status: Optional[str]
    exit_code: Optional[int]
    duration_ms: Optional[int]
    stdout_log_path: Optional[str]
    stderr_log_path: Optional[str]
    command_hash: Optional[str]


def _validate_progress_event(ev: AcceptanceProgressEvent) -> None:
    """Fail-closed enum + lifecycle invariants for acceptance-progress events.

    Schema-parsing rule (see flow_contract.py module docstring): we never
    accept an out-of-vocabulary enum string by silent fallthrough — explicit
    rejection routes the caller to either fix the producer or escalate.
    Lifecycle invariants enforce the Q6.1 schema contract: `started` events
    must NOT carry outcome fields; `completed`/`timeout` events MUST carry
    them. T9 resume relies on this invariant when reading the tail.
    """
    if ev.event not in VALID_PROGRESS_EVENTS:
        raise ValueError(
            f"event must be in {VALID_PROGRESS_EVENTS}, got {ev.event!r}"
        )
    # `status is None` is meaningful (started has no status yet) — the
    # explicit None branch + membership check is intentional, not a
    # `.get()`-style bypass. See pitfall: schema-parsing-get-vs-in.
    if ev.status is not None and ev.status not in VALID_PROGRESS_STATUSES:
        raise ValueError(
            f"status must be in {VALID_PROGRESS_STATUSES} or None, "
            f"got {ev.status!r}"
        )
    if ev.type not in VALID_PROGRESS_TYPES:
        raise ValueError(
            f"type must be in {VALID_PROGRESS_TYPES}, got {ev.type!r}"
        )
    if ev.method not in VALID_PROGRESS_METHODS:
        raise ValueError(
            f"method must be in {VALID_PROGRESS_METHODS}, got {ev.method!r}"
        )
    if ev.idempotent not in VALID_PROGRESS_IDEMPOTENT:
        raise ValueError(
            f"idempotent must be in {VALID_PROGRESS_IDEMPOTENT}, "
            f"got {ev.idempotent!r}"
        )
    # Lifecycle invariants (Q6.1 schema):
    if ev.event == "started":
        if (ev.completed_at is not None or ev.status is not None
                or ev.duration_ms is not None):
            raise ValueError(
                "started event must have completed_at/status/duration_ms = None"
            )
    else:  # completed | timeout
        if (ev.completed_at is None or ev.status is None
                or ev.duration_ms is None):
            raise ValueError(
                f"{ev.event} event requires completed_at + status + "
                f"duration_ms"
            )


def append_acceptance_progress(
    task_dir: Path, ev: AcceptanceProgressEvent,
) -> None:
    """Append one acceptance-progress event to `<task_dir>/acceptance-progress.jsonl`.

    Validation runs before any disk write — invalid events raise ValueError
    and never touch the file. Lock-contention path mirrors v0.8.0
    `append_decision`: log to stderr and proceed (audit gap, not crash).
    """
    _validate_progress_event(ev)
    path = task_dir / "acceptance-progress.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = append_jsonl_locked(path, asdict(ev))
    if not ok:
        # Audit gap → stderr; mirror v0.8.0 decision-write posture.
        print(
            f"WARN: lock contention on {path}; "
            f"acceptance-progress event dropped: {ev.event_id} ({ev.event})",
            file=sys.stderr,
        )


def compute_criterion_hash(criterion: dict) -> str:
    """Y7: sha256 over key-sorted JSON of the normalized criterion. Stable
    across runs / processes; distinct from `command_hash` (which is just
    sha256 of the resolved command line). Used for audit identity in
    `acceptance-progress.jsonl`.

    Key-sorted serialization makes the hash insensitive to dict insertion
    order — two criterions that differ only in JSON key order MUST produce
    the same hash, otherwise resume becomes flaky after a contract round-
    trip.
    """
    norm = json.dumps(criterion, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()
