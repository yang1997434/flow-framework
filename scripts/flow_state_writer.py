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
import errno
import hashlib
import json
import os
import socket
import sys
import uuid
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional, Union


class JournalCorruptError(Exception):
    """Codex T5 R1 [P2] — raised by `_has_auto_engaged_for` when
    `decisions.jsonl` contains a line that cannot be parsed (json error
    or non-dict). Distinguishes "journal physically corrupt" from
    "no auto_engaged event present" so recovery does NOT silently
    treat a truncated/garbled journal as a pre-engagement state.

    Caller (`detect_auto_prepare_state`) catches this and routes to
    `interrupted_journal_corrupt` — same `block_type` as the other
    interrupted states (T19 routes identically), distinct state-name
    preserves cause/effect honesty per §6 contradiction-fix rule.
    """

sys.path.insert(0, str(Path(__file__).resolve().parent / "common"))
from safe_io import atomic_write_text, atomic_write_json, append_jsonl_locked


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
    # T6 / Q6.2 — promoted from `Optional[str]` (v0.8.0) to `list[str]`
    # (codex-refined). One retry can resolve multiple prior failed decisions.
    # Forward-compat normalization in `__post_init__`: a v0.8.0-shape single
    # string becomes `[str]`; explicit `None` becomes `[]`. Use `Union[...]`
    # at the type-annotation level so static checkers don't flag the legacy
    # caller shape during the v0.8.0 → v0.8.1 transition.
    supersedes: Union[list[str], str, None] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Q6.2 forward-compat: accept legacy single-string shape and None.
        # Schema-parsing rule (cf. .flow/pitfalls/schema-parsing-get-vs-in.md):
        # we explicitly distinguish None / str / list rather than relying on
        # truthiness — `[]` is a valid empty value, `None` is the legacy
        # absent shape, and a non-empty string must NOT be expanded to a
        # per-character list (the `isinstance(..., str)` branch comes BEFORE
        # any iteration). Anything else (e.g. tuple of strings) is normalized
        # to a list to keep JSON serialization deterministic; types other
        # than list/str/None are rejected because forwarding silently would
        # be the same A-class falsy bypass the pitfall doc warns against.
        if self.supersedes is None:
            self.supersedes = []
        elif isinstance(self.supersedes, str):
            self.supersedes = [self.supersedes]
        elif isinstance(self.supersedes, list):
            # Already correct shape — but enforce element type for fail-closed
            # JSON correctness; an int/None inside the list would slip through
            # to disk and confuse downstream readers.
            if not all(isinstance(x, str) for x in self.supersedes):
                raise ValueError(
                    f"supersedes must be list[str], got "
                    f"{[type(x).__name__ for x in self.supersedes]}"
                )
        else:
            raise ValueError(
                f"supersedes must be list[str] | str | None, "
                f"got {type(self.supersedes).__name__}"
            )


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
    block_type: Optional[str] = None,
) -> Path:
    """Write transient blocked.md. Resume protocol clears it on success.

    ``block_type`` is the §1 routing classifier T19's recovery
    dispatcher reads to pick the resolver (e.g.,
    ``manifest_violation``, ``post_merge_verify_failed``,
    ``atomic_merge_crashed``). Optional for back-compat with v0.8.0
    callers; T15+ writers SHOULD pass it. When present it is emitted
    as a frontmatter line so operators can grep without parsing the
    body. Validation of the value is the caller's responsibility —
    here we only guard against frontmatter injection by rejecting
    values that contain a newline (which would break out of the
    `block_type:` line into adjacent frontmatter rows).
    """
    bt_line = ""
    if block_type is not None:
        if not isinstance(block_type, str) or "\n" in block_type:
            raise ValueError(
                f"block_type must be a single-line str; got {block_type!r}"
            )
        bt_line = f"block_type: {block_type}\n"
    body = (
        f"---\n"
        f"{bt_line}"
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
    # Lifecycle invariants (Q6.1 schema): `started` events MUST have ALL
    # outcome fields = None. Codex T4 R1 [P2]: original check only covered
    # 3 of 7 outcome fields; a caller using `dataclasses.replace(completed_ev,
    # event="started")` could write a "started" line carrying exit_code /
    # log paths / command_hash — confusing T9's tail reader. Reject all 7.
    _OUTCOME_FIELDS = (
        "completed_at", "status", "duration_ms",
        "exit_code", "stdout_log_path", "stderr_log_path", "command_hash",
    )
    if ev.event == "started":
        leaked = [
            f for f in _OUTCOME_FIELDS if getattr(ev, f) is not None
        ]
        if leaked:
            raise ValueError(
                f"started event must have all outcome fields = None; "
                f"got non-None: {leaked}"
            )
    else:  # completed | timeout
        # Required outcome fields: completed_at + status + duration_ms.
        # exit_code / log paths / command_hash remain optional per design
        # (e.g., file_exists method has no exit code). Don't over-tighten.
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


# ----------------------------------------------------------------------
# T6 — 10 autonomy event types in `decisions.jsonl`.
#
# Per design §8.4 (event table) + §6 R3/R4/R6/Y3/Y8/R10. Single helper
# `append_autonomy_event(task_dir, event, fields)` validates:
#   1. `event` is one of the 10 known autonomy events (fail-closed enum).
#   2. `fields` covers the per-event required-field set (fail-closed schema).
#
# Ordering note (cf. blindspot-C / D2): validation runs BEFORE any disk
# write — invalid events raise `ValueError` and never touch the journal.
# Lock contention on `decisions.jsonl` falls back to a stderr WARN (mirrors
# v0.8.0 `append_decision` posture: audit gap, not crash). Distinct from
# schema validation — a contended lock is not a schema problem and must
# not raise at the validator layer.
#
# Co-existence with v0.8.0 records (Step 6.7): the v0.8.0 `DecisionRecord`
# shape has NO `event` field; v0.8.1 autonomy events ALWAYS carry
# `event: <name>`. Readers disambiguate by presence/absence of the key —
# do NOT use `.get("event")` truthiness (the schema-parsing pitfall says
# `null` and absent must not collide).
# ----------------------------------------------------------------------


EVENT_AUTO_ENGAGED = "auto_engaged"
EVENT_TASK_READY_TO_MERGE = "task_ready_to_merge"
EVENT_MERGE_STARTED = "merge_started"
EVENT_MERGE_APPLIED = "merge_applied"
EVENT_POST_MERGE_VERIFICATION_STARTED = "post_merge_verification_started"
EVENT_POST_MERGE_VERIFICATION_COMPLETED = "post_merge_verification_completed"
EVENT_POST_MERGE_VERIFY_FAILED = "post_merge_verify_failed"
EVENT_TASK_COMPLETED = "task_completed"
EVENT_AUTO_PREPARE_CONSUMED = "auto_prepare_consumed"
EVENT_AUTO_PREPARE_INTERRUPTED = "auto_prepare_interrupted"

ALL_AUTONOMY_EVENTS: tuple[str, ...] = (
    EVENT_AUTO_ENGAGED,
    EVENT_TASK_READY_TO_MERGE,
    EVENT_MERGE_STARTED,
    EVENT_MERGE_APPLIED,
    EVENT_POST_MERGE_VERIFICATION_STARTED,
    EVENT_POST_MERGE_VERIFICATION_COMPLETED,
    EVENT_POST_MERGE_VERIFY_FAILED,
    EVENT_TASK_COMPLETED,
    EVENT_AUTO_PREPARE_CONSUMED,
    EVENT_AUTO_PREPARE_INTERRUPTED,
)

# Per-event required-field map (design §8.4). `frozenset` prevents accidental
# in-place mutation by callers; `set(fields.keys()) - required` is the
# difference primitive used in the validator. We DO NOT use `.get()` here —
# `key not in fields` is the existence check (schema-parsing-get-vs-in
# pitfall: `null` value MUST NOT silently satisfy a required field). The
# validator below enforces presence; per-field type/value validation is the
# producer's responsibility (e.g. T10 emits `auto_engaged`, owns its types).
EVENT_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    EVENT_AUTO_ENGAGED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "worktree_id", "worktree_path",
        "original_base_commit", "current_base_commit",
        "lifecycle_state", "checkpoint_id",
        "contract_path", "contract_hash", "contract_schema_version",
    }),
    EVENT_TASK_READY_TO_MERGE: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "worktree_id", "worktree_path",
        "original_base_commit", "current_base_commit",
        "lifecycle_state", "diff_hash", "target_commit_pre_merge",
    }),
    EVENT_MERGE_STARTED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "worktree_id", "worktree_path",
        "integration_target", "target_commit_pre_merge",
    }),
    EVENT_MERGE_APPLIED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id", "worktree_id",
        "target_commit_post_merge", "merge_strategy",
    }),
    EVENT_POST_MERGE_VERIFICATION_STARTED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "verification_worktree_id", "verification_worktree_path",
        "target_commit_post_merge",
    }),
    EVENT_POST_MERGE_VERIFICATION_COMPLETED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "verification_worktree_id", "status", "criteria_results",
    }),
    EVENT_POST_MERGE_VERIFY_FAILED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "verification_worktree_id", "blocked_md_path", "user_choices",
    }),
    EVENT_TASK_COMPLETED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id", "worktree_id",
        "final_diff_hash", "target_commit_post_merge",
    }),
    EVENT_AUTO_PREPARE_CONSUMED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "lock_path", "consumed_at",
    }),
    EVENT_AUTO_PREPARE_INTERRUPTED: frozenset({
        "event_id", "ts", "slug", "run_id", "task_id",
        "lock_path", "blocked_md_path",
    }),
}


def append_autonomy_event(
    task_dir: Path, event: str, fields: dict,
) -> None:
    """Append a v0.8.1 autonomy event to ``<task_dir>/decisions.jsonl``.

    Validates two things, fail-closed, BEFORE any disk write:
      1. ``event`` is in :data:`ALL_AUTONOMY_EVENTS` (raises ``ValueError``
         on unknown name — caller bug).
      2. ``fields`` covers :data:`EVENT_REQUIRED_FIELDS[event]` (raises
         ``ValueError`` listing the missing fields).

    On lock contention (audit gap), mirrors v0.8.0 ``append_decision``
    posture: print a WARN to stderr and proceed. We do NOT raise at the
    contention layer — it's a transient I/O event, not a schema problem.

    Schema-parsing rule (.flow/pitfalls/schema-parsing-get-vs-in.md): the
    presence check uses ``required - set(fields.keys())`` so an explicit
    ``null`` value still SATISFIES the required-field contract — the
    contract is "key present", not "value truthy". Per-field type/value
    validation is the producer's concern.
    """
    if event not in ALL_AUTONOMY_EVENTS:
        raise ValueError(
            f"unknown autonomy event: {event!r}; "
            f"must be in {ALL_AUTONOMY_EVENTS}"
        )
    required = EVENT_REQUIRED_FIELDS[event]
    missing = required - set(fields.keys())
    if missing:
        raise ValueError(
            f"event {event!r} missing required fields: {sorted(missing)}"
        )
    # Build the on-disk record. `event` is positioned first by convention
    # (helps tail-readers + grep visually); **fields preserves caller's
    # ordering for the rest. If `fields` already contains an `event` key
    # (caller bug), our explicit `event=` placement wins via dict-merge
    # semantics — but only because `**fields` is expanded LAST. To prevent
    # a caller from accidentally overriding the validated event-name, we
    # reject `event` in `fields` upfront.
    if "event" in fields:
        raise ValueError(
            f"`fields` must not contain an 'event' key; pass event name "
            f"as the second positional arg (got fields['event']="
            f"{fields['event']!r})"
        )
    record = {"event": event, **fields}
    path = task_dir / "decisions.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    ok = append_jsonl_locked(path, record)
    if not ok:
        # Audit gap → stderr; mirror v0.8.0 decision-write posture.
        print(
            f"WARN: lock contention on {path}; "
            f"autonomy event dropped: {event}/{fields.get('event_id')}",
            file=sys.stderr,
        )


# ----------------------------------------------------------------------
# T5 — auto_prepare.lock state machine + 4-state crash recovery.
#
# Per design §8.1 (file path / schema / state machine), §6 R10 (boundary
# marker rationale), §6 Y8 (`auto_prepare_consumed` event proves
# consumption), §1 row 17 (`blocked_auto_prepare` status routing).
#
# Three-call surface:
#   - write_auto_prepare_lock()      — atomic write at contract-parse time,
#                                       BEFORE the `auto_engaged` event.
#   - consume_auto_prepare_lock()    — rename to `auto_prepare.consumed`
#                                       AFTER `auto_engaged` succeeds + emit
#                                       `auto_prepare_consumed` event (Y8).
#   - detect_auto_prepare_state()    — return one of 6 states for orchestrator
#                                       (T19) crash recovery routing.
#
# Atomicity choices (and why):
#   - WRITE: same `atomic_write_json()` used everywhere else in this module
#     — temp + fsync + rename within the same fs. POSIX `rename(2)` is the
#     atomic primitive; either old-or-new content is observable, never a
#     partial JSON. Rejecting re-write while a live lock is present is
#     enforced by `path.exists()` before `atomic_write_json` (the alternative,
#     `O_CREAT|O_EXCL`, would race fork-children of the same orchestrator;
#     this layer is single-writer-per-task by design — the contract).
#   - CONSUME: `os.replace(src, dst)` — POSIX-atomic rename within the same
#     dir. After the call, exactly one of `auto_prepare.lock` /
#     `auto_prepare.consumed` is observable.
#
# PID liveness: pure-Python `os.kill(pid, 0)` distinguishes
#   - `ProcessLookupError` (errno ESRCH)  → DEAD.
#   - `PermissionError`    (errno EPERM)  → ALIVE-but-not-ours (different uid).
#   - any other OSError                   → re-raise (do NOT silently treat
#                                            EINVAL/EAGAIN/etc. as "alive" or
#                                            "dead"; that would be a D2/D3
#                                            blindspot — silently swallowing
#                                            an OSError that we don't
#                                            understand).
#   The default for unknown failure is "alive" (caller will treat it as
#   `active_run` — conservative: do not mistakenly classify a live process
#   as crashed). But we re-raise unknown errno because conservative-default
#   without a record is the very D2 antipattern the pitfall doc warns about.
# ----------------------------------------------------------------------


AUTO_PREPARE_LOCK_FILENAME = "auto_prepare.lock"
AUTO_PREPARE_CONSUMED_FILENAME = "auto_prepare.consumed"


@dataclass
class AutoPrepareLock:
    """13-field lock record per design §8.1 — pre-`auto_engaged` boundary
    marker. Single file per task at `<task_dir>/auto_prepare.lock`. NEVER
    co-exists with an `auto_engaged` event for the same `run_id/task_id`
    (orphan_lock_post_engaged is the recovery state for that anomaly).
    """
    lock_version: int
    slug: str
    run_id: str
    task_id: str
    contract_path: str
    contract_hash: str
    contract_schema_version: int
    created_at: str
    pid: int
    host: str
    cwd: str
    target_branch: str
    intended_first_task_dispatch_at: str


def _new_event_id() -> str:
    """12-hex uuid suffix — used by all autonomy events (§8.4)."""
    return uuid.uuid4().hex[:12]


def write_auto_prepare_lock(task_dir: Path, lock: AutoPrepareLock) -> Path:
    """Atomic write at contract-parse time, BEFORE the `auto_engaged` event.

    Rejects re-write while a live (un-consumed) lock is already present —
    that would mean either a duplicate orchestrator startup (caller bug) or
    that the previous run's recovery hasn't finished (must call
    `detect_auto_prepare_state` first). Either case is a programmer error;
    fail-loud rather than silently overwrite.
    """
    task_dir = Path(task_dir)
    task_dir.mkdir(parents=True, exist_ok=True)
    path = task_dir / AUTO_PREPARE_LOCK_FILENAME
    if path.exists():
        raise FileExistsError(
            f"auto_prepare.lock already present at {path}; "
            f"detect_auto_prepare_state() before retry"
        )
    atomic_write_json(path, asdict(lock))
    return path


def consume_auto_prepare_lock(
    task_dir: Path, *, slug: str, run_id: str, task_id: str,
) -> Path:
    """Rename `auto_prepare.lock` → `auto_prepare.consumed` AFTER
    `auto_engaged` succeeds + emit `auto_prepare_consumed` event (Y8).

    `os.replace` is the atomic rename primitive (within a single fs).
    Y8 event-emit ordering: rename FIRST, then append event. Rationale:
    if rename succeeds and event-append fails (lock contention on
    decisions.jsonl), the audit gap is logged (mirrors v0.8.0 posture);
    the boundary marker is still consumed. The reverse ordering would
    leave a "consumed" event with the lock still on disk on the rare
    event-write-failure path — worse for forensics.
    """
    task_dir = Path(task_dir)
    lock_path = task_dir / AUTO_PREPARE_LOCK_FILENAME
    if not lock_path.exists():
        raise FileNotFoundError(
            f"no auto_prepare.lock to consume at {lock_path}"
        )
    consumed_path = task_dir / AUTO_PREPARE_CONSUMED_FILENAME
    os.replace(lock_path, consumed_path)  # POSIX-atomic rename
    consumed_at = datetime.datetime.now(datetime.UTC).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    # Y8: emit auto_prepare_consumed event. T6 wires this through
    # `append_autonomy_event`, gaining required-field validation
    # (event name + 7 required fields). Behavior preserved vs T5's
    # ad-hoc emission: same 7 fields, same fail-soft on lock contention
    # (audit gap → stderr WARN inside append_autonomy_event itself).
    # The rename already succeeded; consumption is "done" from the
    # boundary marker's perspective. T19 recovery sees `consumed` file
    # + `auto_engaged` event = clean_post_engagement.
    append_autonomy_event(
        task_dir,
        EVENT_AUTO_PREPARE_CONSUMED,
        {
            "event_id": _new_event_id(),
            "ts": consumed_at,
            "slug": slug,
            "run_id": run_id,
            "task_id": task_id,
            "lock_path": str(consumed_path),
            "consumed_at": consumed_at,
        },
    )
    return consumed_path


def _is_pid_alive(pid: int) -> bool:
    """POSIX `kill(pid, 0)` liveness check — distinguishes ESRCH (dead)
    from EPERM (alive, different uid). Re-raises any other OSError so
    we never silently treat an unknown errno as a definitive answer
    (D2/D3 blindspot — `except OSError: return X` is exactly the
    `subprocess rc / kill rc` confusion the pitfall doc warns about).

    Out-of-range PIDs are treated as dead (defensively); negative or
    zero PIDs would have special semantics under `kill(2)` (process
    group / all-processes broadcast) and MUST NOT be used here.
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        # errno.ESRCH — pid not found = definitively dead.
        return False
    except PermissionError:
        # errno.EPERM — pid exists but is owned by another user. The
        # process IS alive; we just can't signal it. From the recovery
        # standpoint that means "do not interfere" — same as our own
        # alive-pid case.
        return True
    except OSError as e:
        # Any other errno (EINVAL? EFAULT? unexpected). Do NOT silently
        # treat as alive-or-dead. Re-raise so the orchestrator surfaces
        # the unknown OS error rather than mis-classifying recovery state.
        raise OSError(
            f"unexpected OSError while checking pid {pid}: "
            f"errno={e.errno} ({errno.errorcode.get(e.errno, '?')})"
        ) from e


def _has_auto_engaged_for(
    task_dir: Path, run_id: str, task_id: str,
) -> bool:
    """Tail-scan `decisions.jsonl` for an `auto_engaged` event matching
    this `run_id/task_id`. Per Q7.2 (round-3 R10): scope is per-task,
    so the match must include both fields.

    Schema-parsing rule (cf. flow_contract.py CONTRIBUTOR NOTE): for the
    matching predicate we DO use `dict.get()` here intentionally — these
    fields are read from a forward-compat append-only log where missing /
    null fields are SEMANTICALLY equivalent to "this record is not
    `auto_engaged` for our run". The `.get(...) == X` form on a record
    we did not produce is the forward-compat-correct check; it's not a
    schema-parsing-of-our-own-input path. (If the v0.8.1 producer ever
    writes `event=null` instead of omitting the key, the equality check
    against `"auto_engaged"` correctly returns False — no bypass.)
    """
    path = task_dir / "decisions.jsonl"
    if not path.is_file():
        return False
    # Whole-file read is fine for this size class — `decisions.jsonl`
    # is per-task, capped by the task's lifetime. T9's tail-reader
    # uses incremental scanning; T5's recovery path is one-shot startup.
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        # Don't swallow — re-raise. If decisions.jsonl is unreadable,
        # recovery cannot make a sound classification (D2 blindspot:
        # silently treating "unreadable journal" as "no engagement"
        # would falsely classify a real interrupted run as `no_run`
        # and silently restart — exactly the silent-degeneration mode
        # the §6 contradiction-fix prohibits).
        raise
    # Codex T5 R2 [P2] — scan EVERY line before returning. Original code
    # short-circuited on first matching `auto_engaged` event, which meant a
    # corruption AFTER a valid match was silently ignored. That bypass let
    # `clean_post_engagement` win when the journal actually has an integrity
    # problem the operator must see. Track match in a flag, fall through to
    # the end-of-loop, and return the match only after every line has been
    # validated. Any malformed line raises JournalCorruptError before we
    # return — caller routes to `interrupted_journal_corrupt`.
    matched = False
    for line_no, raw_line in enumerate(text.splitlines(), start=1):
        line = raw_line.strip()
        if not line:
            continue
        # Codex T5 R1 [P2] — fail-closed on malformed lines. Original
        # silent-skip was a D2 fallback bypass: if the only `auto_engaged`
        # event for this task got truncated mid-flush, silent-skip → caller
        # sees False → recovery classifies as no_run / pre-engagement and
        # silently dispatches FRESH, even though the original orchestrator
        # HAD engaged. Raise a distinguished exception so the caller routes
        # to a definite block state (interrupted_journal_corrupt).
        try:
            rec = json.loads(line)
        except json.JSONDecodeError as e:
            raise JournalCorruptError(
                f"decisions.jsonl line {line_no} malformed: {e}"
            ) from e
        if not isinstance(rec, dict):
            raise JournalCorruptError(
                f"decisions.jsonl line {line_no} not a dict "
                f"(got {type(rec).__name__})"
            )
        if (rec.get("event") == "auto_engaged"
                and rec.get("run_id") == run_id
                and rec.get("task_id") == task_id):
            matched = True
            # Do NOT return early — keep scanning so a corruption after the
            # match still fails closed.
    return matched


def detect_auto_prepare_state(
    task_dir: Path, *,
    run_id: str, task_id: str, current_contract_hash: str,
) -> dict:
    """Return one of 6 (+3 synthetic) states for the orchestrator's (T19)
    crash recovery dispatcher. Per design §8.1 detection-state-machine
    table:

        | lock? | engaged? | pid? | hash? | host? | state                       |
        |-------|----------|------|-------|-------|-----------------------------|
        | no    | no       | n/a  | n/a   | n/a   | no_run                      |
        | no    | yes      | n/a  | n/a   | n/a   | clean_post_engagement       |
        | yes   | yes      | n/a  | n/a   | n/a   | orphan_lock_post_engaged    |
        | yes   | no       | alive| match | match | active_run                  |
        | yes   | no       | dead | match | match | interrupted_dead_pid        |
        | yes   | no       | n/a  | mis   | n/a   | interrupted_contract_changed|
        | yes   | no       | n/a  | n/a   | mis   | interrupted_host_mismatch † |
        | yes   | no       | n/a  | n/a   | n/a   | interrupted_lock_corrupt †  |
        | n/a   | corrupt  | n/a  | n/a   | n/a   | interrupted_journal_corrupt†|

      † = synthetic states (not in plan's 6-state table). All three share
          `block_type=auto_prepare_interrupted` so T19 routes them
          identically; distinct state-names preserve cause/effect honesty
          per §6 contradiction-fix rule (a state name that lies about its
          cause is the kind of fallback-after-soft-degrade footgun the
          rule prohibits).

    Detection-order rationale (D1 / blindspot-C):
      1. Read lock-presence + engaged-presence FIRST (cheap, no parse).
         Engaged-scan can raise `JournalCorruptError` if decisions.jsonl
         has a malformed line — caught and routed to
         `interrupted_journal_corrupt` (codex T5 R1 [P2] fix; silent-skip
         was a D2 fallback bypass — a truncated `auto_engaged` line could
         be the only proof of engagement).
      2. If both present → orphan (lock that should have been consumed
         when engaged was emitted; we report this for cleanup, not for
         classification).
      3. Only when lock-present-and-not-engaged do we parse lock JSON +
         consult contract hash → host → pid liveness. The contract-hash
         check is a `==` compare, NOT `.get(...) or ""` — see schema-parsing-
         get-vs-in pitfall: an explicitly-null `contract_hash` in the
         on-disk lock indicates a malformed lock and MUST NOT silently
         match an arbitrary current hash.
      4. Contract-mismatch is checked BEFORE host/pid because it's the
         most decisive signal: if the contract changed, the recovery
         decision is `block` regardless of host/pid.
      5. Host-mismatch is checked BEFORE pid because cross-host PID
         collision (codex T5 R1 [P2]): lock written on machine A copied/
         synced to B. On B, `_is_pid_alive(lock_pid)` would treat any
         locally-live PID as "the original orchestrator" — coincidence.
         Without the host check, recovery classifies as `active_run`
         forever and never proceeds.
    """
    task_dir = Path(task_dir)
    lock_path = task_dir / AUTO_PREPARE_LOCK_FILENAME
    lock_present = lock_path.is_file()
    # Codex T5 R1 [P2] — if decisions.jsonl has a malformed line, route to
    # `interrupted_journal_corrupt`. Distinct state-name (vs lock_corrupt)
    # preserves cause/effect honesty; same `block_type` so T19 routes
    # identically. Do NOT silent-skip the bad line in `_has_auto_engaged_for`:
    # that would be a D2 fallback bypass — a truncated `auto_engaged` line
    # could be the only proof of engagement, and silent-skip would let
    # recovery dispatch fresh on top of a real interrupted run.
    try:
        engaged = _has_auto_engaged_for(task_dir, run_id, task_id)
    except JournalCorruptError as e:
        return {
            "state": "interrupted_journal_corrupt",
            "block_type": "auto_prepare_interrupted",
            "lock": None,
            "journal_corrupt": True,
            "parse_error": str(e),
        }

    if not lock_present and not engaged:
        return {"state": "no_run"}
    if not lock_present and engaged:
        return {"state": "clean_post_engagement"}
    if lock_present and engaged:
        # §8.1 invariant: lock NEVER co-lives with auto_engaged.
        # T19 routes this to "consume + warn" (not block — the engaged
        # event proves the auto run got past the boundary).
        return {
            "state": "orphan_lock_post_engaged",
            "action": "consume_with_warning",
            "lock_path": str(lock_path),
        }

    # lock_present and not engaged — distinguish the three sub-cases.
    # Parse lock JSON. If parse fails, that itself is a blocked-state
    # signal: the boundary marker is corrupt, do NOT silently `no_run`.
    # We surface this as `interrupted_lock_corrupt` (a synthetic 7th state
    # not in the plan's 6-state table) rather than conflate it with
    # `interrupted_dead_pid` — same `block_type` so T19 routes the same
    # way, but a distinct state-name avoids D1 conflation: a state name
    # that lies about its cause is exactly the kind of fallback-after-
    # soft-degrade footgun the §6 contradiction-fix prohibits.
    try:
        raw = lock_path.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Concurrent consume between is_file() and read_text(). Treat as
        # `clean_post_engagement` if engaged would now be True — but we
        # already checked engaged above. Race-window inside this function
        # is single-orchestrator-per-task by design (T19 startup); the
        # only reason we'd see this is a hand-edit. Re-raise as a loud
        # signal rather than silently mis-classify.
        raise
    except OSError:
        raise
    try:
        lock = json.loads(raw)
    except json.JSONDecodeError as e:
        return {
            "state": "interrupted_lock_corrupt",
            "block_type": "auto_prepare_interrupted",
            "lock": None,
            "lock_corrupt": True,
            "parse_error": str(e),
        }
    if not isinstance(lock, dict):
        return {
            "state": "interrupted_lock_corrupt",
            "block_type": "auto_prepare_interrupted",
            "lock": None,
            "lock_corrupt": True,
        }

    # Use `==` against required fields (no `.get(...) or ""` — explicit
    # null / missing must NOT silently match an arbitrary current hash).
    on_disk_hash = lock.get("contract_hash")
    if on_disk_hash != current_contract_hash:
        return {
            "state": "interrupted_contract_changed",
            "block_type": "auto_prepare_interrupted",
            "lock": lock,
        }
    # Codex T5 R1 [P2] — cross-host PID-collision guard. Lock was written
    # on machine A (host="hostA" recorded), then task_dir was copied/synced
    # to machine B. On B, `_is_pid_alive(lock_pid)` would treat any locally
    # live PID as "the original orchestrator" — but it's a totally unrelated
    # B-process. Without this check, recovery classifies as `active_run`
    # forever and never proceeds.
    #
    # Schema-parsing rule (cf. .flow/pitfalls/schema-parsing-get-vs-in.md):
    # use `"host" in lock` for explicit-null treatment, NOT `lock.get("host")`.
    # v0.8.1 lock schema requires `host: str` (see AutoPrepareLock dataclass).
    # Three cases:
    #   1. `host` key missing       → fail-closed (older v0.8.0-shaped lock
    #      should never reach here in v0.8.1; fail-closed routes to a
    #      definite block state rather than silently trusting PID)
    #   2. `host` key present, == ours → trust pid liveness (pre-existing path)
    #   3. `host` key present, != ours → original orchestrator unreachable
    #      from here; route to interrupted_host_mismatch (distinct
    #      state-name preserves cause/effect; same block_type → T19 routes
    #      identically to dead_pid).
    current_host = socket.gethostname()
    if "host" not in lock or not isinstance(lock.get("host"), str):
        # Missing or non-string host → fail-closed. v0.8.1 schema requires
        # `host: str`; a lock without it is malformed for this version.
        # We treat as host_mismatch (not lock_corrupt) because the lock
        # JSON itself parses as a dict — only the host field is wrong.
        return {
            "state": "interrupted_host_mismatch",
            "block_type": "auto_prepare_interrupted",
            "lock": lock,
            "current_host": current_host,
            "lock_host": lock.get("host"),
        }
    if lock["host"] != current_host:
        # Cross-host: the recorded PID can't be trusted on this machine.
        # The lock's host orchestrator is unreachable from B's perspective;
        # we cannot signal it, cannot probe it, and any locally-live PID
        # match is a coincidence. Route to a distinct state so forensics
        # / T19 logs show the actual cause (per §6 contradiction-fix rule).
        return {
            "state": "interrupted_host_mismatch",
            "block_type": "auto_prepare_interrupted",
            "lock": lock,
            "current_host": current_host,
            "lock_host": lock["host"],
        }

    # Pid liveness LAST (cheapest semantically, but most expensive
    # in the only-trustworthy-from-our-uid sense). `_is_pid_alive`
    # validates `pid > 0`; pass `-1` for missing/non-int to force the
    # "dead" branch deterministically rather than crashing on int().
    raw_pid = lock.get("pid")
    try:
        pid_val = int(raw_pid) if raw_pid is not None else -1
    except (TypeError, ValueError):
        pid_val = -1
    if _is_pid_alive(pid_val):
        return {"state": "active_run", "lock": lock}
    return {
        "state": "interrupted_dead_pid",
        "block_type": "auto_prepare_interrupted",
        "lock": lock,
    }
