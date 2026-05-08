"""AFK timeout monitor (v0.8.2 T2).

Wires the v0.8.1 schema-only ``afk_on_timeout`` field to actual
runtime enforcement. Built on top of T1 ``PausedClock`` (we DO NOT
reimplement the clock — only consume it).

Semantics (PRD §R1):

- ``afk_on_timeout: wait`` (default) — autonomy norm. On idle
  timeout: ``evaluate`` returns ``"timeout"``, but ``to_snapshot``
  returns ``None``. Caller stays parked (not killed). Only the 24 h
  hard cap can override and force termination.
- ``afk_on_timeout: abort`` — on idle timeout: ``evaluate`` returns
  ``"timeout"`` AND ``to_snapshot`` returns a ``HardStopSnapshot``
  with ``reason="afk_timeout"``.
- 24 h hard cap: ``active_seconds(now) >= hard_cap_seconds`` ALWAYS
  produces a snapshot, regardless of mode (it overrides ``wait``).

Activity signals (any one resets the AFK timer; PRD §R1.2):

- ``note_file_mtime_tick`` — monitored dir's mtime advanced.
- ``note_command_issuance`` — operator/subagent issued a command.
- ``note_subagent_heartbeat`` — progress.md / heartbeat updated.

Activity during pause: signal is recorded (``last_activity_iso``
updated) but the clock is NOT auto-resumed. Pause/resume goes
through the explicit ``pause`` / ``resume`` passthrough.

Persistence: ``to_dict`` / ``from_dict`` round-trip preserves mode +
idle_seconds_threshold + hard_cap_seconds + last_activity_iso +
clock state (intervals + start_iso).

No I/O on import. No subprocess / shell. Pure-Python wallclock math.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Optional

from common.paused_clock import PausedClock, _parse_iso  # type: ignore
from common.snapshot import HardStopSnapshot


VALID_MODES: frozenset[str] = frozenset({"wait", "abort"})

DEFAULT_IDLE_SECONDS_THRESHOLD: float = 1800.0  # 30 min
DEFAULT_HARD_CAP_SECONDS: float = 86400.0  # 24 h
IDLE_WARN_FRACTION: float = 0.80  # PRD R1.2 boundary


EvalResult = Literal["ok", "idle_warn", "timeout", "hard_cap"]
ToSnapshotReason = Literal["timeout", "hard_cap"]


def _seconds_between(earlier_iso: str, later_iso: str) -> float:
    """Seconds elapsed from ``earlier_iso`` to ``later_iso``.

    Both must be ISO-8601 with ``Z`` suffix (canonical Flow format).
    Negative result is allowed (caller is responsible for ordering);
    we do not clamp because callers like ``evaluate`` need the raw
    delta.
    """
    return (_parse_iso(later_iso) - _parse_iso(earlier_iso)).total_seconds()


@dataclass
class AfkMonitor:
    """AFK monitor consuming a ``PausedClock``.

    Construction:
        AfkMonitor(start_iso, mode="wait", idle_seconds_threshold=1800)

    The clock is constructed internally with the same ``start_iso``.
    To round-trip an existing clock state, use ``to_dict`` /
    ``from_dict``.

    Attributes:
        clock: ``PausedClock`` — pause/resume + active_seconds.
        idle_seconds_threshold: timeout threshold in seconds (default
            1800 = 30 min).
        mode: ``"wait"`` | ``"abort"``.
        hard_cap_seconds: 24 h default; overrides wait mode on hit.
        last_activity_iso: most recent activity tick (initialised to
            ``start_iso``).
    """

    clock: PausedClock
    idle_seconds_threshold: float
    mode: str
    hard_cap_seconds: float
    last_activity_iso: str

    # ---- construction --------------------------------------------------

    def __init__(
        self,
        start_iso: str,
        mode: str = "wait",
        idle_seconds_threshold: float = DEFAULT_IDLE_SECONDS_THRESHOLD,
        hard_cap_seconds: float = DEFAULT_HARD_CAP_SECONDS,
        clock: Optional[PausedClock] = None,
        last_activity_iso: Optional[str] = None,
    ) -> None:
        if mode not in VALID_MODES:
            raise ValueError(
                f"AfkMonitor: invalid mode {mode!r} "
                f"(allowed: {sorted(VALID_MODES)})"
            )
        if idle_seconds_threshold <= 0:
            raise ValueError(
                f"AfkMonitor: idle_seconds_threshold must be > 0, "
                f"got {idle_seconds_threshold!r}"
            )
        if hard_cap_seconds <= 0:
            raise ValueError(
                f"AfkMonitor: hard_cap_seconds must be > 0, "
                f"got {hard_cap_seconds!r}"
            )
        self.mode = mode
        self.idle_seconds_threshold = float(idle_seconds_threshold)
        self.hard_cap_seconds = float(hard_cap_seconds)
        self.clock = clock if clock is not None else PausedClock(
            start_iso=start_iso, intervals=[],
        )
        # First tick = start of task; "fresh task = fresh AFK clock"
        # (I-class: no leakage from prior task counter).
        self.last_activity_iso = (
            last_activity_iso if last_activity_iso is not None else start_iso
        )

    # ---- activity signals (3 mechanical channels per PRD §R1.2) -------

    def note_file_mtime_tick(self, now_iso: str) -> None:
        """Monitored dir mtime advanced — record activity, do NOT resume."""
        self._record_activity(now_iso)

    def note_command_issuance(self, now_iso: str) -> None:
        """Operator/subagent issued a command — record activity."""
        self._record_activity(now_iso)

    def note_subagent_heartbeat(self, now_iso: str) -> None:
        """progress.md / heartbeat updated — record activity."""
        self._record_activity(now_iso)

    def _record_activity(self, now_iso: str) -> None:
        # B-class: activity during pause MUST NOT auto-resume the clock.
        # We only update last_activity_iso here. Pause/resume is an
        # explicit operator action via ``pause`` / ``resume``.
        self.last_activity_iso = now_iso

    # ---- pause/resume passthrough --------------------------------------

    def pause(self, reason: str, now_iso: str) -> None:
        self.clock.pause(reason, now_iso)

    def resume(self, now_iso: str) -> None:
        self.clock.resume(now_iso)

    # ---- evaluation ----------------------------------------------------

    def evaluate(self, now_iso: str) -> EvalResult:
        """Return the current AFK state.

        Priority:
          1. ``hard_cap`` if ``clock.active_seconds(now) >=
             hard_cap_seconds``. ALWAYS wins, regardless of mode.
          2. ``timeout`` if seconds-since-``last_activity_iso`` >=
             ``idle_seconds_threshold``.
          3. ``idle_warn`` if at >= 80 % of threshold.
          4. ``ok`` otherwise.
        """
        # Hard cap: cumulative active wallclock — independent of
        # idle activity (a 24 h budget for the whole run).
        if self.clock.active_seconds(now_iso) >= self.hard_cap_seconds:
            return "hard_cap"
        idle = _seconds_between(self.last_activity_iso, now_iso)
        if idle >= self.idle_seconds_threshold:
            return "timeout"
        if idle >= self.idle_seconds_threshold * IDLE_WARN_FRACTION:
            return "idle_warn"
        return "ok"

    # ---- terminal action ----------------------------------------------

    def to_snapshot(
        self,
        task_slug: str,
        now_iso: str,
        reason: ToSnapshotReason,
    ) -> Optional[HardStopSnapshot]:
        """Convert an AFK terminal event into a ``HardStopSnapshot``.

        - ``reason="timeout"`` + ``mode="wait"`` -> returns ``None``.
          Caller stays parked; only ``hard_cap`` can override wait
          mode. This is the load-bearing semantic from PRD §R1.
        - ``reason="timeout"`` + ``mode="abort"`` -> snapshot.
        - ``reason="hard_cap"`` -> snapshot regardless of mode (24 h
          overrides wait).
        """
        if reason == "timeout" and self.mode == "wait":
            return None
        if reason not in ("timeout", "hard_cap"):
            raise ValueError(
                f"AfkMonitor.to_snapshot: invalid reason {reason!r} "
                f"(allowed: 'timeout' | 'hard_cap')"
            )
        extra = {
            "mode": self.mode,
            "idle_seconds_threshold": self.idle_seconds_threshold,
            "trigger": reason,
            "active_seconds": self.clock.active_seconds(now_iso),
            "last_activity_iso": self.last_activity_iso,
        }
        if reason == "hard_cap":
            extra["hard_cap_seconds"] = self.hard_cap_seconds
        return HardStopSnapshot(
            reason="afk_timeout",
            counter_name=None,
            value=None,
            limit=None,
            hit_at_iso=now_iso,
            estimated=False,  # AFK is mechanical wallclock, NOT estimated
            extra=extra,
            task_slug=task_slug,
        )

    # ---- persistence ---------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "idle_seconds_threshold": self.idle_seconds_threshold,
            "hard_cap_seconds": self.hard_cap_seconds,
            "last_activity_iso": self.last_activity_iso,
            "clock": self.clock.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> "AfkMonitor":
        clock = PausedClock.from_dict(d["clock"])
        return cls(
            start_iso=clock.start_iso,
            mode=d["mode"],
            idle_seconds_threshold=float(d["idle_seconds_threshold"]),
            hard_cap_seconds=float(d.get(
                "hard_cap_seconds", DEFAULT_HARD_CAP_SECONDS,
            )),
            clock=clock,
            last_activity_iso=d["last_activity_iso"],
        )


# ---- orchestrator wire-up helper (consumed by flow_orchestrator) -----


def apply_afk_check(
    monitor: AfkMonitor,
    task_slug: str,
    now_iso: str,
) -> Optional[HardStopSnapshot]:
    """Single dispatch-tick entrypoint for the orchestrator.

    Returns a ``HardStopSnapshot`` if the run must terminate (abort
    timeout OR 24 h hard cap), or ``None`` if the run should continue
    (incl. wait mode parked-on-timeout — caller stays in its current
    paused state).

    T2 ships this helper dormant; T3 wires it into the Phase 2 retry
    loop alongside budget enforcement.
    """
    state = monitor.evaluate(now_iso)
    if state == "hard_cap":
        return monitor.to_snapshot(
            task_slug=task_slug, now_iso=now_iso, reason="hard_cap",
        )
    if state == "timeout":
        # mode='wait' returns None here (park); mode='abort' returns
        # a snapshot (kill).
        return monitor.to_snapshot(
            task_slug=task_slug, now_iso=now_iso, reason="timeout",
        )
    return None


def now_iso_utc() -> str:
    """UTC ISO-8601 with ``Z`` suffix.

    Provided so callers don't need to import from
    ``common.paused_clock`` separately. Determinism note: tests must
    pass an explicit ``now_iso`` and not call this.
    """
    raw = datetime.now(timezone.utc).isoformat()
    if raw.endswith("+00:00"):
        return raw[:-6] + "Z"
    return raw
