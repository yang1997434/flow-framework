#!/usr/bin/env python3
"""Smoke tests for v0.5 hint_outbox — append-only hint queue."""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class HintOutbox(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._old_home = os.environ.get("FLOW_HOME")
        os.environ["FLOW_HOME"] = self._tmp.name
        # Re-import to pick up FLOW_HOME
        for m in list(sys.modules):
            if m.startswith("common.hint_outbox"):
                del sys.modules[m]

    def tearDown(self):
        if self._old_home is None:
            os.environ.pop("FLOW_HOME", None)
        else:
            os.environ["FLOW_HOME"] = self._old_home
        self._tmp.cleanup()

    def test_write_creates_file_in_hints_dir(self):
        from common.hint_outbox import write_hint
        path = write_hint({"task_slug": "abc", "phase": "phase-2"})
        self.assertTrue(path.is_file())
        self.assertEqual(path.parent.name, "hints")
        data = json.loads(path.read_text())
        self.assertEqual(data["task_slug"], "abc")

    def test_list_pending_returns_only_unprocessed(self):
        from common.hint_outbox import write_hint, list_pending, mark_processed
        p1 = write_hint({"task_slug": "a"})
        p2 = write_hint({"task_slug": "b"})
        self.assertEqual(set(list_pending()), {p1, p2})
        mark_processed(p1)
        self.assertEqual(list_pending(), [p2])

    def test_two_hints_in_same_second_get_unique_filenames(self):
        from common.hint_outbox import write_hint
        p1 = write_hint({"x": 1})
        p2 = write_hint({"x": 2})
        self.assertNotEqual(p1.name, p2.name)

    def test_mark_processed_moves_into_processed_subdir(self):
        from common.hint_outbox import write_hint, mark_processed
        p = write_hint({"x": 1})
        mark_processed(p)
        self.assertFalse(p.exists())
        moved = (p.parent / "processed" / p.name)
        self.assertTrue(moved.is_file())


if __name__ == "__main__":
    unittest.main(verbosity=2)
