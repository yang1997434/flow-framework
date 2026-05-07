"""T8 ship-required smoke: (type × phase × status) routing matrix.

Per design §7 line 314 — fuzzes every cell in the §3 line 130–137 retry
table + Y1 e2e routing. Asserts ``AcceptanceRunner.evaluate_criterion``
verdicts exactly match the design row.

Scope: T8 owns the runner-side decision; orchestrator dispatch (which
actually consumes ``LOCAL_FIX_ALLOWED`` and re-runs the criterion) lives
in T13 (codex review whitelist) + T15 (gate harness loop). This smoke
pins the runner contract so T13/T15 can build on a stable verdict.
"""
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


class TestAcceptanceRetryMatrix(unittest.TestCase):
    """Iterate every (type, phase, status) cell from §3 line 130–137 +
    Y1 e2e routing. Asserts EvalDecision exactly matches the design row."""

    MATRIX = [
        # (type, method, phase, status, escalate, expected)
        # Phase 2 — local-fix-allowed types
        ("unit",        "cmd", 2, "fail",      False, EvalDecision.LOCAL_FIX_ALLOWED),
        ("integration", "cmd", 2, "fail",      False, EvalDecision.LOCAL_FIX_ALLOWED),
        ("behavior",    "cmd", 2, "fail",      False, EvalDecision.LOCAL_FIX_ALLOWED),
        ("smoke",       "cmd", 2, "fail",      False, EvalDecision.LOCAL_FIX_ALLOWED),
        # Phase 2 — never-local types
        ("regression",  "cmd", 2, "fail",      False, EvalDecision.BLOCK_ROW5),
        ("e2e",         "cmd", 2, "fail",      True,  EvalDecision.BLOCKED_ESCALATE_ROW6),
        # Phase 2 — timeout always blocks (e2e escalates per Y1)
        ("unit",        "cmd", 2, "timed_out", False, EvalDecision.BLOCK_ROW5),
        ("e2e",         "cmd", 2, "timed_out", True,  EvalDecision.BLOCKED_ESCALATE_ROW6),
        # Phase 3 — never-local types: behavior + e2e + regression escalate (R2)
        ("behavior",    "cmd", 3, "fail",      False, EvalDecision.BLOCKED_ESCALATE_ROW6),
        ("e2e",         "cmd", 3, "fail",      True,  EvalDecision.BLOCKED_ESCALATE_ROW6),
        # Phase 3 regression — codex round-1 found this cell was unverified
        # and the branch ordering was routing it to BLOCK_ROW5 instead of
        # row 6. PHASE3_NEVER_LOCAL_TYPES includes regression; this cell
        # pins the corrected ordering.
        ("regression",  "cmd", 3, "fail",      False, EvalDecision.BLOCKED_ESCALATE_ROW6),
        ("regression",  "cmd", 3, "timed_out", False, EvalDecision.BLOCKED_ESCALATE_ROW6),
        # Phase 3 — unit/integration block (no retry post-merge)
        ("unit",        "cmd", 3, "fail",      False, EvalDecision.BLOCK_ROW5),
        ("integration", "cmd", 3, "fail",      False, EvalDecision.BLOCK_ROW5),
        # Pass propagates regardless of type/phase
        ("unit",        "cmd", 2, "pass",      False, EvalDecision.PASS),
        ("e2e",         "cmd", 3, "pass",      False, EvalDecision.PASS),
    ]

    def test_full_matrix(self):
        with tempfile.TemporaryDirectory() as td:
            runner = AcceptanceRunner(
                worktree_root=Path(td), log_dir=Path(td) / "l",
                slug="d", task_id="T", run_id="r", worktree_id="w",
            )
            for tup in self.MATRIX:
                type_, method, phase, status, escalate, expected = tup
                with self.subTest(cell=tup):
                    crit = AcceptanceCriterion(
                        description="x", type=type_, method=method,
                        command="x" if method == "cmd" else None,
                        timeout_sec=30,
                    )
                    result = RunResult(
                        status=status,
                        exit_code=(0 if status == "pass" else 1),
                        duration_ms=5,
                        escalate=escalate,
                    )
                    self.assertEqual(
                        runner.evaluate_criterion(
                            crit, phase=phase, runner_result=result),
                        expected,
                    )


if __name__ == "__main__":
    unittest.main()
