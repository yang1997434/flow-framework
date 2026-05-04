#!/usr/bin/env python3
"""Smoke tests for v0.5 SessionStart compact-matcher resume injection."""
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
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "session-start.py"


class SessionStartCompact(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-ss-")).resolve()
        flow = self.tmp / ".flow"
        task = flow / "tasks" / "01-01-demo"
        task.mkdir(parents=True)
        (task / "prd.md").write_text("# Demo Task\n\nstuff\n", encoding="utf-8")
        (task / "progress.md").write_text(
            "---\nphase: phase-2-execute\n---\n", encoding="utf-8"
        )
        (flow / ".current-task").write_text(
            str(task.relative_to(self.tmp)), encoding="utf-8"
        )
        # Pre-existing checkpoint files
        cp = task / ".checkpoint"
        cp.mkdir()
        (cp / "intent.md").write_text(
            "---\nschema_version: 1\ntrigger: manual\nts: 2026-05-04T15:30:00+08:00\n"
            "context_pct_estimated: 50\ntask_slug: 01-01-demo\nphase: phase-2-execute\n"
            "supersedes: none\n---\n\n## Current Intent\nworking on it\n",
            encoding="utf-8",
        )
        (cp / "mechanical.json").write_text(json.dumps({
            "schema_version": 1,
            "ts": "2026-05-04T15:35:00+08:00",
            "trigger": "precompact",
            "task_slug": "01-01-demo",
            "phase": "phase-2-execute",
            "git": {"branch": "main", "head": "abc1234", "dirty_files": 0,
                    "recent_commits": [{"hash": "abc1234", "subject": "wip"}]},
            "files_touched_recent": ["foo.py", "bar.py"],
            "context_pct_estimated": 88,
            "transcript_path_size_bytes": 800000,
        }), encoding="utf-8")
        self.task = task

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_hook(self, matcher: str) -> dict:
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps({"cwd": str(self.tmp), "trigger": matcher}),
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_compact_matcher_injects_resume_block(self):
        out = self._run_hook("compact")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("flow-resumed-from-compact", ctx)
        self.assertIn("Last Intent", ctx)
        self.assertIn("Current Intent", ctx)  # body of intent.md is in
        self.assertIn("Latest Mechanical State", ctx)
        self.assertIn("abc1234", ctx)
        self.assertIn("MANUAL", ctx)  # Resume Mode

    def test_startup_matcher_does_not_inject_resume_block(self):
        out = self._run_hook("startup")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("flow-resumed-from-compact", ctx)

    def test_compact_with_no_checkpoint_falls_back(self):
        shutil.rmtree(self.task / ".checkpoint")
        out = self._run_hook("compact")
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertNotIn("flow-resumed-from-compact", ctx)
        # but should still have active task in standard quick-guide
        self.assertIn("Active Task", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
