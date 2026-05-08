"""T2 — AFK timeout R1.1 test.

Covers Acceptance R1.1: wait/abort modes + 24h hard cap.

Semantic load-bearing assertions:
- mode="wait" + idle past threshold -> evaluate returns "timeout",
  but to_snapshot(reason="timeout") returns None (park, NOT kill).
- mode="abort" + idle past threshold -> snapshot is produced.
- 24h hard cap overrides wait mode -> snapshot is produced regardless
  of mode.
- mode validation -> ValueError on bogus input.
- Round-trip to_dict/from_dict preserves mode + last_activity_iso +
  clock state.

B-class: state machine — wait vs abort vs hard_cap transitions,
all unit-tested.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.afk_monitor import AfkMonitor  # noqa: E402  type: ignore
from common.snapshot import HardStopSnapshot  # noqa: E402  type: ignore


def _iso(dt: datetime) -> str:
    raw = dt.astimezone(timezone.utc).isoformat()
    if raw.endswith("+00:00"):
        return raw[:-6] + "Z"
    return raw


def _shift(start_iso: str, seconds: float) -> str:
    if start_iso.endswith("Z"):
        d = datetime.fromisoformat(start_iso[:-1] + "+00:00")
    else:
        d = datetime.fromisoformat(start_iso)
    return _iso(d + timedelta(seconds=seconds))


class TestAfkModeValidation(unittest.TestCase):
    def test_wait_is_default(self):
        m = AfkMonitor(start_iso="2026-05-08T00:00:00Z")
        self.assertEqual(m.mode, "wait")

    def test_explicit_abort_ok(self):
        m = AfkMonitor(start_iso="2026-05-08T00:00:00Z", mode="abort")
        self.assertEqual(m.mode, "abort")

    def test_bogus_mode_raises(self):
        with self.assertRaises(ValueError):
            AfkMonitor(start_iso="2026-05-08T00:00:00Z", mode="bogus")

    def test_threshold_default_30min(self):
        m = AfkMonitor(start_iso="2026-05-08T00:00:00Z")
        self.assertEqual(m.idle_seconds_threshold, 1800)


class TestAfkWaitMode(unittest.TestCase):
    """mode='wait' — idle timeout returns "timeout" but DOES NOT terminate."""

    def setUp(self):
        self.start = "2026-05-08T00:00:00Z"
        self.m = AfkMonitor(
            start_iso=self.start, mode="wait", idle_seconds_threshold=1800,
        )

    def test_wait_idle_timeout_evaluates_timeout(self):
        # 31 min idle -> past threshold
        now = _shift(self.start, 31 * 60)
        self.assertEqual(self.m.evaluate(now), "timeout")

    def test_wait_timeout_to_snapshot_returns_none(self):
        """Load-bearing: wait mode parks, does NOT kill on idle timeout.

        Only hard_cap overrides wait. Caller stays paused.
        """
        now = _shift(self.start, 31 * 60)
        snap = self.m.to_snapshot(
            task_slug="t1", now_iso=now, reason="timeout",
        )
        self.assertIsNone(snap)


class TestAfkAbortMode(unittest.TestCase):
    """mode='abort' — idle timeout terminates with snapshot."""

    def setUp(self):
        self.start = "2026-05-08T00:00:00Z"
        self.m = AfkMonitor(
            start_iso=self.start, mode="abort", idle_seconds_threshold=1800,
        )

    def test_abort_idle_timeout_evaluates_timeout(self):
        now = _shift(self.start, 31 * 60)
        self.assertEqual(self.m.evaluate(now), "timeout")

    def test_abort_timeout_to_snapshot_returns_snapshot(self):
        now = _shift(self.start, 31 * 60)
        snap = self.m.to_snapshot(
            task_slug="t1", now_iso=now, reason="timeout",
        )
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.reason, "afk_timeout")
        self.assertEqual(snap.task_slug, "t1")
        self.assertEqual(snap.hit_at_iso, now)
        self.assertEqual(snap.extra.get("mode"), "abort")
        self.assertEqual(snap.extra.get("idle_seconds_threshold"), 1800)


class TestAfkHardCap(unittest.TestCase):
    """24h hard cap — ALWAYS wins, regardless of mode."""

    def setUp(self):
        self.start = "2026-05-08T00:00:00Z"

    def test_wait_hard_cap_evaluates_hard_cap(self):
        m = AfkMonitor(
            start_iso=self.start, mode="wait", idle_seconds_threshold=1800,
        )
        # 24h elapsed
        now = _shift(self.start, 86400)
        self.assertEqual(m.evaluate(now), "hard_cap")

    def test_wait_hard_cap_to_snapshot_terminates(self):
        m = AfkMonitor(
            start_iso=self.start, mode="wait", idle_seconds_threshold=1800,
        )
        now = _shift(self.start, 86400)
        snap = m.to_snapshot(task_slug="t1", now_iso=now, reason="hard_cap")
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.reason, "afk_timeout")
        self.assertEqual(snap.extra.get("trigger"), "hard_cap")

    def test_abort_hard_cap_terminates(self):
        m = AfkMonitor(
            start_iso=self.start, mode="abort", idle_seconds_threshold=1800,
        )
        now = _shift(self.start, 86400)
        self.assertEqual(m.evaluate(now), "hard_cap")
        snap = m.to_snapshot(task_slug="t1", now_iso=now, reason="hard_cap")
        self.assertIsInstance(snap, HardStopSnapshot)

    def test_hard_cap_beats_idle_timeout_priority(self):
        """If both 24h elapsed AND idle past threshold, hard_cap wins."""
        m = AfkMonitor(
            start_iso=self.start, mode="wait", idle_seconds_threshold=1800,
        )
        # No activity recorded since start; 24h+ elapsed
        now = _shift(self.start, 86400 + 60)
        self.assertEqual(m.evaluate(now), "hard_cap")


class TestAfkRoundTrip(unittest.TestCase):
    def test_to_from_dict_preserves_state(self):
        start = "2026-05-08T00:00:00Z"
        m = AfkMonitor(
            start_iso=start, mode="abort", idle_seconds_threshold=600,
        )
        # advance + activity tick
        m.note_command_issuance(_shift(start, 100))
        m.pause("user paused", _shift(start, 200))
        m.resume(_shift(start, 250))
        d = m.to_dict()
        m2 = AfkMonitor.from_dict(d)
        self.assertEqual(m2.mode, "abort")
        self.assertEqual(m2.idle_seconds_threshold, 600)
        self.assertEqual(m2.last_activity_iso, m.last_activity_iso)
        # clock state preserved
        self.assertEqual(
            m2.clock.active_seconds(_shift(start, 300)),
            m.clock.active_seconds(_shift(start, 300)),
        )

    def test_round_trip_preserves_hard_cap_seconds(self):
        m = AfkMonitor(start_iso="2026-05-08T00:00:00Z")
        m.hard_cap_seconds = 3600  # custom (test-only override)
        d = m.to_dict()
        m2 = AfkMonitor.from_dict(d)
        self.assertEqual(m2.hard_cap_seconds, 3600)


class TestAfkSnapshotShape(unittest.TestCase):
    def test_snapshot_extra_carries_mode_and_threshold(self):
        m = AfkMonitor(
            start_iso="2026-05-08T00:00:00Z",
            mode="abort",
            idle_seconds_threshold=900,
        )
        now = _shift("2026-05-08T00:00:00Z", 1000)
        snap = m.to_snapshot(task_slug="t-x", now_iso=now, reason="timeout")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.reason, "afk_timeout")
        self.assertEqual(snap.extra["mode"], "abort")
        self.assertEqual(snap.extra["idle_seconds_threshold"], 900)
        # estimated=False — AFK is wallclock-mechanical, not estimated.
        self.assertFalse(snap.estimated)


if __name__ == "__main__":
    unittest.main()
