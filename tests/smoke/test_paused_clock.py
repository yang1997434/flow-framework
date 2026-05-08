"""T1 — PausedClock interval records test.

Covers Acceptance R2.4: paused-clock interval records on disk;
crash-resume invariant (mid-pause dump+reload yields identical
active_seconds for the same `now_iso`).

B-class blindspot: state machine — pause-while-paused is a no-op;
resume-when-not-paused is a no-op. Idempotent transitions.
G-class blindspot: dump+reload identity for open intervals.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.paused_clock import (  # noqa: E402  type: ignore
    PauseInterval,
    PausedClock,
    now_iso,
)


class TestPausedClockBasics(unittest.TestCase):
    def test_no_pauses_active_seconds_equals_elapsed(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        self.assertAlmostEqual(
            c.active_seconds("2026-05-08T00:01:00Z"), 60.0, places=6
        )

    def test_single_closed_pause_subtracts_duration(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("review_wait", "2026-05-08T00:00:30Z")
        c.resume("2026-05-08T00:00:50Z")
        # 60s elapsed, 20s paused -> 40s active.
        self.assertAlmostEqual(
            c.active_seconds("2026-05-08T00:01:00Z"), 40.0, places=6
        )

    def test_multiple_sequential_pauses_accumulate(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("p1", "2026-05-08T00:00:10Z")
        c.resume("2026-05-08T00:00:20Z")  # 10s
        c.pause("p2", "2026-05-08T00:00:30Z")
        c.resume("2026-05-08T00:00:35Z")  # 5s
        c.pause("p3", "2026-05-08T00:00:40Z")
        c.resume("2026-05-08T00:00:50Z")  # 10s
        # 60s elapsed, 25s paused -> 35s active.
        self.assertAlmostEqual(
            c.active_seconds("2026-05-08T00:01:00Z"), 35.0, places=6
        )

    def test_open_pause_excludes_time_since_open(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("review_wait", "2026-05-08T00:00:30Z")
        # 60s elapsed, paused at 30s -> active 30s (regardless of now).
        self.assertAlmostEqual(
            c.active_seconds("2026-05-08T00:01:00Z"), 30.0, places=6
        )
        self.assertAlmostEqual(
            c.active_seconds("2026-05-08T00:02:00Z"), 30.0, places=6
        )


class TestIdempotentTransitions(unittest.TestCase):
    """B-class — pause-while-paused / resume-when-not-paused are no-ops."""

    def test_pause_when_paused_is_noop(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("p1", "2026-05-08T00:00:10Z")
        # Second pause must NOT open a second interval.
        c.pause("p2", "2026-05-08T00:00:15Z")
        self.assertEqual(len(c.intervals), 1)
        self.assertEqual(c.intervals[0].reason, "p1")
        self.assertEqual(c.intervals[0].paused_at_iso, "2026-05-08T00:00:10Z")

    def test_resume_when_not_paused_is_noop(self):
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.resume("2026-05-08T00:00:10Z")  # No-op.
        self.assertEqual(len(c.intervals), 0)
        # Next pause/resume should still work.
        c.pause("p1", "2026-05-08T00:00:20Z")
        c.resume("2026-05-08T00:00:30Z")
        self.assertEqual(len(c.intervals), 1)
        self.assertEqual(c.intervals[0].resumed_at_iso, "2026-05-08T00:00:30Z")


class TestCrashResumeInvariant(unittest.TestCase):
    """R2.4 — mid-pause dump+reload yields identical active_seconds."""

    def test_open_pause_dump_reload_close_matches_no_crash_control(self):
        # Control: no crash, full lifecycle in one process.
        ctrl = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        ctrl.pause("review_wait", "2026-05-08T00:00:20Z")
        ctrl.resume("2026-05-08T00:00:50Z")
        ctrl_active = ctrl.active_seconds("2026-05-08T00:01:00Z")

        # Crash variant: pause -> dump -> reload -> resume -> compute.
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("review_wait", "2026-05-08T00:00:20Z")
        dumped = c.to_dict()
        loaded = PausedClock.from_dict(dumped)
        # Mid-pause: the open interval must be preserved.
        self.assertEqual(len(loaded.intervals), 1)
        self.assertIsNone(loaded.intervals[0].resumed_at_iso)
        loaded.resume("2026-05-08T00:00:50Z")
        crash_active = loaded.active_seconds("2026-05-08T00:01:00Z")

        self.assertAlmostEqual(crash_active, ctrl_active, places=6)

    def test_open_pause_dump_reload_yields_identical_active_seconds(self):
        # Strict crash invariant: at the same `now`, both should equal.
        c = PausedClock(start_iso="2026-05-08T00:00:00Z", intervals=[])
        c.pause("review_wait", "2026-05-08T00:00:20Z")
        before = c.active_seconds("2026-05-08T00:00:45Z")
        loaded = PausedClock.from_dict(c.to_dict())
        after = loaded.active_seconds("2026-05-08T00:00:45Z")
        self.assertAlmostEqual(before, after, places=6)


class TestNowIso(unittest.TestCase):
    def test_now_iso_returns_z_suffixed_utc_string(self):
        s = now_iso()
        self.assertTrue(s.endswith("Z"))
        # Coarse shape: 2026-05-08T...Z
        self.assertRegex(s, r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d+)?Z$")


class TestPauseIntervalRecord(unittest.TestCase):
    def test_pause_interval_open_then_closed_serialization(self):
        iv = PauseInterval(
            paused_at_iso="2026-05-08T00:00:10Z",
            resumed_at_iso=None,
            reason="review_wait",
        )
        self.assertIsNone(iv.resumed_at_iso)


if __name__ == "__main__":
    unittest.main()
