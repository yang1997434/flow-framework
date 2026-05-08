"""T2 — AFK timeout R1.2 test: 3 mechanical activity signals.

Covers Acceptance R1.2: each of file mtime / cmd issuance / subagent
heartbeat resets the AFK timer.

B-class: pause-during-activity does NOT auto-resume the clock.
Boundary tests: 79% / 80% / 100% of threshold.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.afk_monitor import AfkMonitor  # noqa: E402  type: ignore


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


class TestAfkActivitySignalsReset(unittest.TestCase):
    """Each of 3 note_* methods updates last_activity_iso and resets AFK."""

    def setUp(self):
        self.start = "2026-05-08T00:00:00Z"
        self.m = AfkMonitor(
            start_iso=self.start, mode="abort", idle_seconds_threshold=1800,
        )

    def test_file_mtime_tick_resets(self):
        # Right before timeout (1799s idle)
        late = _shift(self.start, 1799)
        # Tick activity at the brink — would have been "idle_warn"
        # without a tick; tick resets the idle clock.
        self.m.note_file_mtime_tick(late)
        self.assertEqual(self.m.last_activity_iso, late)
        # Now 30s after the tick — well within "ok" band (idle=30 of 1800)
        check = _shift(self.start, 1799 + 30)
        self.assertEqual(self.m.evaluate(check), "ok")

    def test_command_issuance_resets(self):
        late = _shift(self.start, 1700)
        self.m.note_command_issuance(late)
        self.assertEqual(self.m.last_activity_iso, late)
        check = _shift(self.start, 1700 + 200)
        # idle since tick = 200s, well under 1800 threshold
        self.assertEqual(self.m.evaluate(check), "ok")

    def test_subagent_heartbeat_resets(self):
        late = _shift(self.start, 1500)
        self.m.note_subagent_heartbeat(late)
        self.assertEqual(self.m.last_activity_iso, late)
        check = _shift(self.start, 1500 + 1000)
        # 1000s since tick — still under threshold
        self.assertEqual(self.m.evaluate(check), "ok")

    def test_initial_last_activity_is_start(self):
        self.assertEqual(self.m.last_activity_iso, self.start)


class TestAfkIdleWarnBoundary(unittest.TestCase):
    """79% -> ok, 80% -> idle_warn, 100% -> timeout."""

    def setUp(self):
        self.start = "2026-05-08T00:00:00Z"
        self.threshold = 1800
        self.m = AfkMonitor(
            start_iso=self.start, mode="abort",
            idle_seconds_threshold=self.threshold,
        )

    def test_79_percent_is_ok(self):
        # 79% of 1800 = 1422s (below 80%)
        now = _shift(self.start, 1421)
        self.assertEqual(self.m.evaluate(now), "ok")

    def test_80_percent_is_idle_warn(self):
        # exactly 80% = 1440s
        now = _shift(self.start, 1440)
        self.assertEqual(self.m.evaluate(now), "idle_warn")

    def test_100_percent_is_timeout(self):
        now = _shift(self.start, self.threshold)
        self.assertEqual(self.m.evaluate(now), "timeout")


class TestActivityDuringPause(unittest.TestCase):
    """B-class: activity signal during pause records the activity but does
    NOT auto-resume the clock."""

    def test_mtime_tick_during_pause_does_not_resume(self):
        start = "2026-05-08T00:00:00Z"
        m = AfkMonitor(start_iso=start, mode="abort")
        m.pause("operator paused", _shift(start, 100))
        self.assertTrue(m.clock.is_paused())
        # Activity tick while paused
        m.note_file_mtime_tick(_shift(start, 200))
        # Activity recorded
        self.assertEqual(m.last_activity_iso, _shift(start, 200))
        # But clock STILL paused
        self.assertTrue(m.clock.is_paused())

    def test_pause_resume_passthrough(self):
        start = "2026-05-08T00:00:00Z"
        m = AfkMonitor(start_iso=start, mode="abort")
        m.pause("operator paused", _shift(start, 100))
        m.resume(_shift(start, 200))
        self.assertFalse(m.clock.is_paused())
        # 100s of paused time -> active_seconds at t=300 = 200
        self.assertAlmostEqual(
            m.clock.active_seconds(_shift(start, 300)), 200, delta=0.001,
        )


if __name__ == "__main__":
    unittest.main()
