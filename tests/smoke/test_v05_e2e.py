#!/usr/bin/env python3
"""End-to-end test: simulate /flow:pause writes, then SessionStart on
`compact` matcher reads them and produces a resume block."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "session-start.py"


class E2EPauseCompactResume(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-e2e-")).resolve()
        self.runtime = Path(tempfile.mkdtemp(prefix="flow-rt-")).resolve()
        # init project
        if shutil.which("git"):
            subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        flow = self.tmp / ".flow"
        task = flow / "tasks" / "01-01-e2e"
        task.mkdir(parents=True)
        (task / "prd.md").write_text("# E2E Task\n", encoding="utf-8")
        (task / "progress.md").write_text("---\nphase: phase-2-execute\n---\n", encoding="utf-8")
        (flow / ".current-task").write_text(
            str(task.relative_to(self.tmp)), encoding="utf-8"
        )
        self.task = task

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def _isolated_env(self) -> dict:
        """Pin FLOW_HOME so hook reads/writes hint outbox + nudge state in
        tempdir, not the user's real ~/.flow."""
        env = os.environ.copy()
        env["FLOW_HOME"] = str(self.runtime)
        return env

    def test_pause_writes_then_sessionstart_compact_reads(self):
        # Simulate /flow:pause Step 6: write intent.md
        from common.safe_io import atomic_write_text
        from common.checkpoint_paths import intent_path
        intent_body = (
            "---\n"
            "schema_version: 1\n"
            "trigger: manual\n"
            "ts: 2026-05-04T15:30:00+08:00\n"
            "context_pct_estimated: 50\n"
            "task_slug: 01-01-e2e\n"
            "phase: phase-2-execute\n"
            "supersedes: none\n"
            "---\n\n"
            "## Current Intent\nshipping v0.5\n\n"
            "## Next Action\nrun final smoke suite\n"
        )
        atomic_write_text(intent_path(self.task), intent_body)

        # Simulate /flow:pause Step 7: write hint (using FLOW_HOME isolation).
        # Use the same self.runtime as the subprocess below so the hint
        # persists from the in-process write to the hook's read.
        os.environ["FLOW_HOME"] = str(self.runtime)
        try:
            # Force re-import so module-level FLOW_HOME pickup uses our value
            for m in [m for m in list(sys.modules) if "hint_outbox" in m or "nudge" in m]:
                del sys.modules[m]
            from common.hint_outbox import write_hint, list_pending
            write_hint({
                "task_slug": "01-01-e2e",
                "task_path": str(self.task),
                "phase": "phase-2-execute",
                "last_action": "wrote intent.md",
                "next_action": "verify SessionStart sees it",
                "pause_trigger": "manual",
            })
            self.assertEqual(len(list_pending()), 1)
        finally:
            os.environ.pop("FLOW_HOME", None)

        # Now simulate SessionStart with compact matcher.
        # FLOW_HOME pinned to self.runtime so the hook reads the hint we
        # just wrote AND any nudge-state writes stay in tempdir (not real ~/.flow).
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps({"cwd": str(self.tmp), "trigger": "compact"}),
            capture_output=True, text=True, timeout=10,
            env=self._isolated_env(),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = json.loads(result.stdout)
        ctx = out["hookSpecificOutput"]["additionalContext"]

        # Verify resume block contains intent body
        self.assertIn("flow-resumed-from-compact", ctx)
        self.assertIn("Current Intent", ctx)
        self.assertIn("shipping v0.5", ctx)
        self.assertIn("Next Action", ctx)
        self.assertIn("MANUAL", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
