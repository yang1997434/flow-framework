"""T7 ship-required smoke (design §7 line 318).

Pins the runner-side half of the criterion-timeout block contract:
  - long-running cmd exceeding `timeout_sec` returns status `timed_out`
  - non-e2e timeout → `escalate=False` (T8 will route to §1 row 5 `blocked`)
  - e2e timeout    → `escalate=True`  (T8 will route to §1 row 6
                                       `blocked_escalate` per Y1)

T8 wires `evaluate_criterion()` for the §1 row 5 vs row 6 routing decision.
T15 wires the orchestrator's block emission. This smoke pins the contract
between T7's `RunResult` and the downstream block-routing layers.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_acceptance import AcceptanceRunner       # type: ignore  # noqa: E402
from flow_contract import AcceptanceCriterion      # type: ignore  # noqa: E402


class TestCriterionTimeoutBlocks(unittest.TestCase):
    def _runner(self, td: str) -> AcceptanceRunner:
        return AcceptanceRunner(
            worktree_root=Path(td),
            log_dir=Path(td) / "logs",
            slug="demo", task_id="T1", run_id="r1", worktree_id="w",
        )

    def test_unit_cmd_timeout_blocks_without_escalate(self):
        with tempfile.TemporaryDirectory() as td:
            crit = AcceptanceCriterion(
                description="long unit", type="unit", method="cmd",
                command="sleep 5", timeout_sec=1,
            )
            r = self._runner(td).run_one(
                crit, criterion_idx=0, attempt_id="a", retry_idx=0,
                task_dir=Path(td),
            )
            self.assertEqual(r.status, "timed_out")
            self.assertFalse(r.escalate)  # §1 row 5 (block)

    def test_e2e_cmd_timeout_blocks_with_escalate(self):
        """Y1: e2e timeout MUST escalate; T8 will route to §1 row 6."""
        with tempfile.TemporaryDirectory() as td:
            crit = AcceptanceCriterion(
                description="long e2e", type="e2e", method="cmd",
                command="sleep 5", timeout_sec=1,
            )
            r = self._runner(td).run_one(
                crit, criterion_idx=0, attempt_id="a", retry_idx=0,
                task_dir=Path(td),
            )
            self.assertEqual(r.status, "timed_out")
            self.assertTrue(r.escalate)  # §1 row 6 (blocked_escalate)


if __name__ == "__main__":
    unittest.main()
