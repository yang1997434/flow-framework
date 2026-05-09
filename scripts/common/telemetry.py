"""v0.8.5 — dispatch telemetry (schema v1, frozen).

Each ``emit_event`` call appends one JSON record (one line) to the
configured path. The append is atomic-per-line via
``safe_io.append_jsonl_locked``. Failures (lock timeout, OSError on
write) are swallowed: telemetry MUST NEVER block dispatch. A
process-local counter (`swallow_count()`) records how many writes were
dropped — exposed for diagnostic-only test verification and operator
inspection (no external surface yet).

PRD: ``.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md``
sections R1, R2, R3, R5.

Design:

* I-class: pure-ish module. The only mutable state is the swallow
  counter, intentionally process-local. Tests reset / read it directly.
* G-class: writes go through ``safe_io.append_jsonl_locked`` (existing
  reviewed primitive — never roll our own append). Path parent is
  created on demand inside the helper.
* J-class: schema v1 field set is FROZEN. Adding a field is a v2 schema
  bump, not a silent edit. The unit tests pin the field set.
* Failure-mode invariant: ``emit_event`` never raises. Any exception
  inside the writer is caught, the swallow counter ticks up, and we
  log a single warning to stderr. This module is observability-only;
  dropping a row is preferable to crashing the dispatch loop.

PRD R3 phase coverage: callers wrap the 5 dispatch phases
(``worktree_create``, ``implementer``, ``reviewer``, ``gate_run``,
``codex_review``) in ``timed_span`` context managers. The timed span
captures wall-clock duration + supplies hooks for ``outcome`` and
``fail_reason_raw``, then emits exactly one event on context exit
(including on exception — the timing record must land even if the
phase blew up).
"""
from __future__ import annotations

import datetime
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

# Reuse the existing reviewed primitive (G-class — no new I/O code paths).
# Import lazily inside the writer so test-time monkeypatching of this
# module attribute keeps working (unit tests patch
# ``telemetry._append_jsonl_locked`` directly).
try:
    from common.safe_io import append_jsonl_locked as _append_jsonl_locked  # type: ignore
except ImportError:  # pragma: no cover - exercised only on broken import path
    from safe_io import append_jsonl_locked as _append_jsonl_locked  # type: ignore


__all__ = [
    "SCHEMA_VERSION",
    "PHASES",
    "EVENT_FIELDS",
    "VALID_OUTCOMES",
    "emit_event",
    "normalize_outcome",
    "swallow_count",
    "reset_swallow_count",
    "timed_span",
    "TimedSpan",
]


# ── Frozen schema constants ─────────────────────────────────────────

SCHEMA_VERSION: int = 1

# PRD R3 — five dispatch phases. Tuple is intentionally non-mutable.
PHASES: tuple[str, ...] = (
    "worktree_create",
    "implementer",
    "reviewer",
    "gate_run",
    "codex_review",
)

# PRD R2 — frozen v1 event field set. Tests pin this to defend against
# silent additions / removals (J-class regression guard).
EVENT_FIELDS: tuple[str, ...] = (
    "ts",
    "schema_version",
    "task_slug",
    "round_num",
    "phase",
    "duration_ms",
    "outcome",
    "fail_reason_raw",
    "fail_category",
    "worktree_id",
)

# PRD R2 — frozen v1 outcome enumeration. Anything else passed to
# ``emit_event`` is normalised to one of these (codex review I2).
# ``None`` is a meaningful value — phase had no outcome (e.g. infra
# failure where ``set_outcome`` never ran). The string set is closed:
#   pass  — phase succeeded
#   fail  — phase blocked / errored / verdict not pass / inconclusive
#   skip  — phase legitimately skipped (e.g. codex_review on non-codex
#           gates would emit skip; v0.8.5 currently only emits when the
#           gate ran, so this is reserved for v0.8.6 use)
VALID_OUTCOMES: frozenset = frozenset({"pass", "fail", "skip", None})

# Mapping table for non-frozen verdict strings observed in the wild.
# Codex review I2: GateRunner.verdict.status returns ``inconclusive``
# / ``blocked``; dispatch_with_retry surfaces ``rejected_with_rationale``.
# All collapse to ``fail`` (the safest non-pass classification); the
# verbatim string is preserved in ``fail_reason_raw`` so audit fidelity
# is not lost.
_OUTCOME_NORMALISATION_TABLE: dict = {
    "pass": "pass",
    "fail": "fail",
    "skip": "skip",
    "skipped": "skip",
    "blocked": "fail",
    "rejected_with_rationale": "fail",
    "inconclusive": "fail",
}


def normalize_outcome(outcome: Optional[str]) -> Optional[str]:
    """Normalise an outcome string to the frozen v1 enumeration.

    Returns one of ``VALID_OUTCOMES``. ``None`` passes through.
    Strings in the explicit mapping table are translated; anything
    else collapses to ``"fail"`` (defensive default — an unrecognised
    outcome is more likely a verdict variant than a happy-path pass).

    Pure function — no I/O, no warnings. Callers that want to flag
    the unrecognised case should do so before calling this helper.
    """
    if outcome is None:
        return None
    if outcome in _OUTCOME_NORMALISATION_TABLE:
        return _OUTCOME_NORMALISATION_TABLE[outcome]
    # Defensive default — unrecognised verdict collapses to fail so
    # the schema stays frozen. The verbatim string lands in
    # fail_reason_raw via emit_event so no audit info is lost.
    return "fail"


# ── Process-local swallow counter ───────────────────────────────────

_swallow_count = 0


def swallow_count() -> int:
    """Return the number of telemetry writes dropped due to write
    failure since process start (or last ``reset_swallow_count``)."""
    return _swallow_count


def reset_swallow_count() -> None:
    """Test seam: zero the swallow counter."""
    global _swallow_count
    _swallow_count = 0


# ── Event emission ──────────────────────────────────────────────────

def _now_iso_utc() -> str:
    """ISO 8601 UTC timestamp with ``Z`` suffix (no microseconds — keeps
    JSONL line lengths predictable). Mirrors the ``afk_monitor.now_iso_utc``
    convention used elsewhere in the orchestrator."""
    return (
        datetime.datetime.now(datetime.timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z")
    )


def emit_event(
    *,
    path: Path,
    task_slug: str,
    round_num: int,
    phase: str,
    duration_ms: int,
    outcome: Optional[str],
    fail_reason_raw: Optional[str],
    worktree_id: Optional[str],
    enabled: bool,
) -> None:
    """Emit one v1 telemetry event to the JSONL file at ``path``.

    Opt-out: when ``enabled`` is False the call is a no-op (no file
    created, no side effect). PRD R5 invariant.

    Failure-mode: any exception raised by the writer is caught, the
    swallow counter is incremented, and a single warning is written to
    stderr. This function MUST NEVER raise — telemetry is
    observability-only and cannot block dispatch.

    Field set is frozen at ``EVENT_FIELDS``. ``fail_category`` is always
    None in v1 (reserved for v0.8.6 classifier).
    """
    if not enabled:
        return

    # I2 frozen-schema enforcement: normalise outcome to ``pass | fail
    # | skip | null`` (PRD R2). When normalisation collapses a verbose
    # verdict (``blocked``, ``rejected_with_rationale``, ...) into
    # ``fail``, preserve the verbatim string in ``fail_reason_raw`` so
    # audit fidelity is not lost. If the caller already populated
    # ``fail_reason_raw`` we APPEND (don't clobber) so neither the
    # caller's diagnostic nor the raw verdict is dropped.
    normalised = normalize_outcome(outcome)
    enriched_reason = fail_reason_raw
    if outcome is not None and outcome != normalised:
        # The original verdict string disappears from ``outcome`` —
        # tuck it into ``fail_reason_raw`` so downstream readers can
        # recover it.
        if enriched_reason is None or enriched_reason == "":
            enriched_reason = str(outcome)
        elif str(outcome) not in str(enriched_reason):
            enriched_reason = f"{enriched_reason} (raw_outcome={outcome})"

    record = {
        "ts": _now_iso_utc(),
        "schema_version": SCHEMA_VERSION,
        "task_slug": task_slug,
        "round_num": round_num,
        "phase": phase,
        "duration_ms": int(duration_ms),
        "outcome": normalised,
        "fail_reason_raw": enriched_reason,
        "fail_category": None,  # PRD R2 — reserved for v0.8.6
        "worktree_id": worktree_id,
    }

    global _swallow_count
    try:
        ok = _append_jsonl_locked(Path(path), record)
        if not ok:
            _swallow_count += 1
            print(
                f"WARN: telemetry append lock timeout at {path}; "
                f"event dropped (swallow_count={_swallow_count})",
                file=sys.stderr,
            )
    except (OSError, ValueError, TypeError) as exc:
        # Catch the narrow set of exceptions a JSONL append can raise.
        # Anything else (KeyboardInterrupt, MemoryError) we let through
        # — those signal a process-level failure where dropping
        # telemetry is the least of our problems.
        _swallow_count += 1
        print(
            f"WARN: telemetry append failed at {path}: {exc!r}; "
            f"event dropped (swallow_count={_swallow_count})",
            file=sys.stderr,
        )


# ── Timed span context manager ──────────────────────────────────────

class TimedSpan:
    """Mutable per-span outcome holder. ``set_outcome`` /
    ``set_fail_reason`` capture review verdicts that resolve mid-span;
    they are written into the emitted event on context exit.
    """

    def __init__(self) -> None:
        self.outcome: Optional[str] = None
        self.fail_reason_raw: Optional[str] = None

    def set_outcome(self, outcome: Optional[str]) -> None:
        self.outcome = outcome

    def set_fail_reason(self, reason: Optional[str]) -> None:
        self.fail_reason_raw = reason


@contextmanager
def timed_span(
    *,
    path: Path,
    task_slug: str,
    round_num: int,
    phase: str,
    worktree_id: Optional[str],
    enabled: bool,
) -> Iterator[TimedSpan]:
    """Context manager that times the wrapped block and emits exactly
    one telemetry event on exit.

    Emits even on exception (the timing record is the whole point — a
    phase that crashed is the most interesting kind of telemetry). The
    exception still propagates after the event lands.

    When ``enabled`` is False, the span still works (caller gets a
    ``TimedSpan`` to set outcome on) but no event is written.
    """
    span = TimedSpan()
    start = time.monotonic()
    try:
        yield span
    finally:
        duration_ms = int((time.monotonic() - start) * 1000.0)
        emit_event(
            path=path,
            task_slug=task_slug,
            round_num=round_num,
            phase=phase,
            duration_ms=duration_ms,
            outcome=span.outcome,
            fail_reason_raw=span.fail_reason_raw,
            worktree_id=worktree_id,
            enabled=enabled,
        )
