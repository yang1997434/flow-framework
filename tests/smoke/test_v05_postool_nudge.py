#!/usr/bin/env python3
"""Smoke tests for v0.5 nudge helper used by PostToolUse hooks."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class NudgeDecide(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("FLOW_HOME")
        os.environ["FLOW_HOME"] = self._tmp.name
        for m in list(sys.modules):
            if m.startswith("common.nudge"):
                del sys.modules[m]

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FLOW_HOME", None)
        else:
            os.environ["FLOW_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_below_threshold_no_nudge(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=30, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNone(text)

    def test_low_confidence_skips_nudge(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=80, confidence="low",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNone(text)

    def test_at_threshold_emits_nudge_first_time(self):
        from common.nudge import maybe_nudge_text
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNotNone(text)
        self.assertIn("55", text)
        self.assertIn("/flow:pause", text)

    def test_already_acknowledged_skips_in_same_window(self):
        from common.nudge import maybe_nudge_text, acknowledge
        # First nudge fires
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w1", min_seconds_between=60)
        self.assertIsNotNone(text)
        acknowledge(task_slug="t", via="manual_pause")
        # Second call same window — suppressed
        text2 = maybe_nudge_text(task_slug="t", pct=60, confidence="high",
                                  window_id="w1", min_seconds_between=0)
        self.assertIsNone(text2)

    def test_new_window_re_arms(self):
        from common.nudge import maybe_nudge_text, acknowledge
        maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                         window_id="w1", min_seconds_between=60)
        acknowledge(task_slug="t", via="manual_pause")
        # New window after compact
        text = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                                 window_id="w2", min_seconds_between=0)
        self.assertIsNotNone(text)

    def test_min_seconds_between_throttles(self):
        from common.nudge import maybe_nudge_text
        t1 = maybe_nudge_text(task_slug="t", pct=55, confidence="high",
                               window_id="w1", min_seconds_between=300)
        self.assertIsNotNone(t1)
        # Same call within throttle window
        t2 = maybe_nudge_text(task_slug="t", pct=60, confidence="high",
                               window_id="w1", min_seconds_between=300)
        self.assertIsNone(t2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
