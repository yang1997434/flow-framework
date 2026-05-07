"""T8 ship-required smoke: R2 enforcement.

Per design §7 line 315 — pins R2: in Phase 3 (post-merge verify),
behavior and e2e MUST always escalate; never enter LOCAL_FIX_ALLOWED,
regardless of status. Sibling assertion verifies Phase 2 behavior IS
allowed local fix (R1) — guards against an over-broad fix that would
break Phase 2 retry along with Phase 3 escalation.
"""
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_acceptance import (   # type: ignore  # noqa: E402
    AcceptanceRunner,
    RunResult,
    EvalDecision,
)
from flow_contract import AcceptanceCriterion  # type: ignore  # noqa: E402


class TestPhase3BehaviorE2eNoRetry(unittest.TestCase):
    """R2: in Phase 3 (post-merge verify), behavior and e2e MUST always
    escalate — never enter LOCAL_FIX_ALLOWED, regardless of status."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp))
        self.runner = AcceptanceRunner(
            worktree_root=Path(self.tmp), log_dir=Path(self.tmp) / "l",
            slug="d", task_id="T", run_id="r", worktree_id="w",
        )

    def _crit(self, type_):
        return AcceptanceCriterion(
            description="x", type=type_, method="cmd",
            command="bash tests/smoke/run.sh", timeout_sec=600,
        )

    def test_phase3_behavior_fail_never_local(self):
        for status in ("fail", "timed_out"):
            with self.subTest(status=status):
                result = RunResult(status=status, duration_ms=5)
                d = self.runner.evaluate_criterion(
                    self._crit("behavior"), phase=3, runner_result=result)
                self.assertNotEqual(d, EvalDecision.LOCAL_FIX_ALLOWED)
                self.assertEqual(d, EvalDecision.BLOCKED_ESCALATE_ROW6)

    def test_phase3_e2e_fail_never_local(self):
        for status in ("fail", "timed_out"):
            with self.subTest(status=status):
                # T7 sets escalate=True for e2e fail/timeout
                result = RunResult(status=status, duration_ms=5, escalate=True)
                d = self.runner.evaluate_criterion(
                    self._crit("e2e"), phase=3, runner_result=result)
                self.assertNotEqual(d, EvalDecision.LOCAL_FIX_ALLOWED)
                self.assertEqual(d, EvalDecision.BLOCKED_ESCALATE_ROW6)

    def test_phase2_behavior_fail_DOES_allow_local(self):
        """Sibling assertion: Phase 2 behavior IS allowed local fix (R1)."""
        result = RunResult(status="fail", duration_ms=5)
        d = self.runner.evaluate_criterion(
            self._crit("behavior"), phase=2, runner_result=result)
        self.assertEqual(d, EvalDecision.LOCAL_FIX_ALLOWED)


if __name__ == "__main__":
    unittest.main()
