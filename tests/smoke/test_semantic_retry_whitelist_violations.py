"""T13 — semantic-diff retry-whitelist violation detection.

Design refs:
  §3 line 141: retry-whitelist BEFORE-vs-AFTER comparison detects
    suppressed verification (tests skipped/deleted, fixture narrowed,
    command flags neutralized) → escalate without consuming retry
    budget.
  §7 line 315: ship-required smoke; this file is the canonical
    detector test.

Trust boundary: caller (gate 4 / retry orchestrator) supplies the
``before_*`` / ``after_*`` lists and diff strings. The helper itself
performs no I/O — G-class watch: callers are responsible for sourcing
the inputs from a single, consistent disk-state snapshot.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_orchestrator import detect_semantic_violations  # type: ignore


class TestSemanticRetryWhitelistViolations(unittest.TestCase):
    def test_test_file_deleted_escalates(self) -> None:
        before = "tests/test_foo.py\nsrc/foo.py\n"
        after = "src/foo.py\n"  # test file gone
        v = detect_semantic_violations(
            before_files=before.splitlines(),
            after_files=after.splitlines(),
            before_diff="def test_foo(): pass\n",
            after_diff="",
        )
        self.assertTrue(v.escalate)
        self.assertIn("test_file_deleted", v.violations)

    def test_skip_decorator_added_escalates(self) -> None:
        before_diff = "def test_x(): assert False"
        after_diff = "@unittest.skip\ndef test_x(): assert False"
        v = detect_semantic_violations(
            before_files=["tests/t.py"], after_files=["tests/t.py"],
            before_diff=before_diff, after_diff=after_diff,
        )
        self.assertTrue(v.escalate)
        self.assertIn("test_skipped", v.violations)

    def test_command_flag_no_fail_fast_escalates(self) -> None:
        before_diff = "pytest tests/"
        after_diff = "pytest --no-fail-fast --ignore=tests/auth tests/"
        v = detect_semantic_violations(
            before_files=["Makefile"], after_files=["Makefile"],
            before_diff=before_diff, after_diff=after_diff,
        )
        self.assertTrue(v.escalate)
        self.assertIn("flag_suppression", v.violations)

    def test_clean_diff_no_escalate(self) -> None:
        v = detect_semantic_violations(
            before_files=["src/foo.py"], after_files=["src/foo.py"],
            before_diff="def x(): return 1",
            after_diff="def x(): return 2",
        )
        self.assertFalse(v.escalate)
        self.assertEqual(v.violations, [])

    def test_fixture_narrowing_escalates(self) -> None:
        # pre-diff fixture has 1000 rows; post-diff has 10 → 100x shrink.
        before = "row\n" * 1000
        after = "row\n" * 10
        v = detect_semantic_violations(
            before_files=["tests/fixtures/data.csv"],
            after_files=["tests/fixtures/data.csv"],
            before_diff=before, after_diff=after,
        )
        self.assertTrue(v.escalate)
        self.assertIn("fixture_narrowing", v.violations)


if __name__ == "__main__":
    unittest.main()
