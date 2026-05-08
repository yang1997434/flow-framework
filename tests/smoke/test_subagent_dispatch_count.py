"""T1 — subagent_dispatches global counter test.

Covers Acceptance R2.5: `subagent_dispatches` is counted globally —
every dispatch (including nested subagent dispatches) increments the
single counter. No fanout escape hatch.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import budget_counter as bc  # noqa: E402  type: ignore


_LIMITS = {
    "tokens_in": 1000.0,
    "tokens_out": 1000.0,
    "cost_usd": 10.0,
    "active_wallclock_minutes": 60.0,
    "subagent_dispatches": 5.0,
}


class TestSubagentDispatchCount(unittest.TestCase):
    def test_register_dispatch_five_times_value_is_five(self):
        cs = bc.make_default_set(_LIMITS)
        for _ in range(5):
            bc.register_dispatch(counters=cs)
        self.assertEqual(cs["subagent_dispatches"].value, 5.0)

    def test_nested_dispatches_count_globally(self):
        # Parent registers; then a "child" subagent registers. Both must
        # count globally — counter == 2, not 1.
        cs = bc.make_default_set(_LIMITS)
        bc.register_dispatch(counters=cs, parent_id=None)
        bc.register_dispatch(counters=cs, parent_id="parent_subagent_id")
        self.assertEqual(cs["subagent_dispatches"].value, 2.0)

    def test_is_hit_triggers_at_limit_regardless_of_nesting(self):
        cs = bc.make_default_set(_LIMITS)
        # Mix of root-level + nested registrations.
        bc.register_dispatch(counters=cs)
        bc.register_dispatch(counters=cs, parent_id="p1")
        bc.register_dispatch(counters=cs, parent_id="p1")
        bc.register_dispatch(counters=cs, parent_id="p2")
        bc.register_dispatch(counters=cs)
        c = cs["subagent_dispatches"]
        self.assertEqual(c.value, 5.0)
        self.assertTrue(c.is_hit())

    def test_dispatch_above_limit_keeps_incrementing(self):
        # Counter still increments past hit; caller checks is_hit() to stop.
        cs = bc.make_default_set(_LIMITS)
        for _ in range(7):
            bc.register_dispatch(counters=cs)
        self.assertEqual(cs["subagent_dispatches"].value, 7.0)
        self.assertTrue(cs["subagent_dispatches"].is_hit())


if __name__ == "__main__":
    unittest.main()
