"""Paused-clock with first-class interval records (v0.8.2 T1).

Time-tracking primitive for `active_wallclock_minutes` budget counter
and AFK timeout (T17). Pause periods are stored as a list of
`PauseInterval` records — NOT as a single accumulated duration —
because the latter cannot be safely reconstructed across crashes
(G-class disk-state drift; PRD §R2.4).

API:
- `pause(reason, now_iso)` opens an interval; idempotent if already
  paused.
- `resume(now_iso)` closes the most-recent open interval; idempotent
  if not currently paused.
- `active_seconds(now_iso)` returns total elapsed minus closed pause
  durations minus the open pause (if currently paused).
- `to_dict()` / `from_dict()` round-trip preserves open intervals so
  a mid-pause crash can be resumed without double-counting.

ISO format: UTC with `Z` suffix, e.g. ``2026-05-08T12:34:56Z``. We use
`datetime.fromisoformat` after replacing trailing `Z` with `+00:00`
(Python 3.11+ understands `Z` natively but we stay defensive across
3.10/3.11).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import List, Optional


def now_iso() -> str:
    """UTC ISO-8601 with `Z` suffix (e.g. `2026-05-08T12:34:56.789012Z`)."""
    # `datetime.now(timezone.utc).isoformat()` yields `+00:00` suffix; we
    # convert to canonical `Z` for transcript/log consistency.
    raw = datetime.now(timezone.utc).isoformat()
    if raw.endswith("+00:00"):
        return raw[:-6] + "Z"
    return raw


def _parse_iso(iso: str) -> datetime:
    """Parse our ISO format (Z-suffixed UTC) to aware datetime."""
    if iso.endswith("Z"):
        iso = iso[:-1] + "+00:00"
    return datetime.fromisoformat(iso)


@dataclass
class PauseInterval:
    paused_at_iso: str
    resumed_at_iso: Optional[str]
    reason: str

    def is_open(self) -> bool:
        return self.resumed_at_iso is None

    def duration_seconds(self, fallback_now_iso: Optional[str] = None) -> float:
        """Closed interval -> exact duration. Open + fallback -> partial.

        If interval is open and `fallback_now_iso` is None, raises
        ValueError (open intervals cannot be measured without a now).
        """
        start = _parse_iso(self.paused_at_iso)
        if self.resumed_at_iso is not None:
            end = _parse_iso(self.resumed_at_iso)
        elif fallback_now_iso is not None:
            end = _parse_iso(fallback_now_iso)
        else:
            raise ValueError("open pause interval needs fallback_now_iso")
        return (end - start).total_seconds()


@dataclass
class PausedClock:
    start_iso: str
    intervals: List[PauseInterval] = field(default_factory=list)

    # ---- state queries -------------------------------------------------

    def is_paused(self) -> bool:
        return bool(self.intervals) and self.intervals[-1].is_open()

    # ---- transitions (idempotent — B-class blindspot mitigation) -------

    def pause(self, reason: str, now_iso: str) -> None:
        """Open a new pause interval. No-op if already paused."""
        if self.is_paused():
            return
        self.intervals.append(
            PauseInterval(
                paused_at_iso=now_iso,
                resumed_at_iso=None,
                reason=reason,
            )
        )

    def resume(self, now_iso: str) -> None:
        """Close the most-recent open interval. No-op if not paused."""
        if not self.is_paused():
            return
        self.intervals[-1].resumed_at_iso = now_iso

    # ---- measurement ---------------------------------------------------

    def active_seconds(self, now_iso: str) -> float:
        """Elapsed seconds since `start_iso`, minus all pause durations.

        Open interval (currently paused): the portion from its
        `paused_at_iso` up to `now_iso` is excluded too. So invoking
        `active_seconds` repeatedly while paused yields the SAME
        value (frozen at the moment of pause) — this is the property
        the crash-resume invariant relies on.
        """
        elapsed = (_parse_iso(now_iso) - _parse_iso(self.start_iso)).total_seconds()
        paused_total = 0.0
        for iv in self.intervals:
            paused_total += iv.duration_seconds(fallback_now_iso=now_iso)
        return elapsed - paused_total

    # ---- persistence ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "start_iso": self.start_iso,
            "intervals": [asdict(iv) for iv in self.intervals],
        }

    @classmethod
    def from_dict(cls, d: dict) -> "PausedClock":
        return cls(
            start_iso=d["start_iso"],
            intervals=[
                PauseInterval(
                    paused_at_iso=iv["paused_at_iso"],
                    resumed_at_iso=iv.get("resumed_at_iso"),
                    reason=iv["reason"],
                )
                for iv in d.get("intervals", [])
            ],
        )
