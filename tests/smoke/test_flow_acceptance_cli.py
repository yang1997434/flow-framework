"""T22 Step 22.4c — smoke for the new ``flow acceptance --run <slug>``
CLI subcommand.

Exit codes:
  0 = all criteria PASS (or contract has no acceptance_criteria).
  1 = first non-PASS criterion (FAIL diagnostic on stderr).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLOW = REPO_ROOT / "scripts" / "flow.py"


class TestFlowAcceptanceCLI(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="flow-acc-"))
        self.addCleanup(lambda: shutil.rmtree(self.repo, ignore_errors=True))
        subprocess.run(
            ["git", "init", "-q", str(self.repo)],
            check=True,
            capture_output=True,
        )
        self.slug_dir = self.repo / ".flow" / "tasks" / "demo"
        self.slug_dir.mkdir(parents=True)

    def _write_contract(self, *, criteria_passing: bool, with_criteria: bool = True):
        cmd = "true" if criteria_passing else "false"
        criteria = []
        if with_criteria:
            criteria = [{
                "description": "fixture",
                "type": "smoke",
                "method": "cmd",
                "command": cmd,
                "timeout_sec": 5,
            }]
        (self.slug_dir / "contract.json").write_text(json.dumps({
            "contract_schema_version": 1,
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
            "acceptance_criteria": criteria,
        }), encoding="utf-8")

    def _run(self, *args, env=None):
        merged = dict(__import__("os").environ)
        if env:
            merged.update(env)
        return subprocess.run(
            [sys.executable, str(FLOW), "acceptance", *args],
            cwd=str(self.repo),
            capture_output=True,
            text=True,
            timeout=60,
            env=merged,
        )

    def test_run_pass_returns_zero(self):
        self._write_contract(criteria_passing=True)
        r = self._run("--run", "demo")
        self.assertEqual(
            r.returncode, 0,
            msg=f"stderr={r.stderr!r} stdout={r.stdout!r}",
        )

    def test_run_fail_returns_nonzero(self):
        self._write_contract(criteria_passing=False)
        r = self._run("--run", "demo")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("FAIL", r.stderr)

    def test_empty_criteria_returns_zero(self):
        self._write_contract(criteria_passing=True, with_criteria=False)
        r = self._run("--run", "demo")
        self.assertEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main()
