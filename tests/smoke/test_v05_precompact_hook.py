#!/usr/bin/env python3
"""Smoke tests for v0.5 PreCompact hook."""
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
HOOK_PATH = REPO_ROOT / "claude" / "hooks" / "pre-compact.py"


def _has_git() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(_has_git(), "git not available")
class PreCompactHook(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-pre-")).resolve()
        self.runtime = Path(tempfile.mkdtemp(prefix="flow-rt-")).resolve()
        # Init project + git
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "test")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "test")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        # .flow + active task
        flow = self.tmp / ".flow"
        (flow / "tasks" / "01-01-demo").mkdir(parents=True)
        (flow / "tasks" / "01-01-demo" / "progress.md").write_text(
            "---\nphase: phase-2-execute\n---\n", encoding="utf-8"
        )
        (flow / ".current-task").write_text(
            str((flow / "tasks" / "01-01-demo").relative_to(self.tmp)), encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def _isolated_env(self) -> dict:
        """Pin FLOW_HOME so hook nudge state stays in tempdir, not real ~/.flow."""
        env = os.environ.copy()
        env["FLOW_HOME"] = str(self.runtime)
        return env

    def _run_hook(self, hook_input: dict) -> int:
        result = subprocess.run(
            ["python3", str(HOOK_PATH)],
            input=json.dumps(hook_input),
            capture_output=True,
            text=True,
            timeout=10,
            env=self._isolated_env(),
        )
        return result.returncode

    def test_writes_mechanical_json_and_history_entry(self):
        rc = self._run_hook({"cwd": str(self.tmp), "transcript_path": ""})
        self.assertEqual(rc, 0)
        cp = self.tmp / ".flow" / "tasks" / "01-01-demo" / ".checkpoint"
        mech = cp / "mechanical.json"
        history = cp / "history.jsonl"
        self.assertTrue(mech.is_file(), "mechanical.json must be written")
        self.assertTrue(history.is_file(), "history.jsonl must be written")
        data = json.loads(mech.read_text())
        self.assertEqual(data["trigger"], "precompact")
        self.assertEqual(data["task_slug"], "01-01-demo")
        # history line
        events = [json.loads(ln) for ln in history.read_text().splitlines() if ln.strip()]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "precompact")

    def test_no_active_task_exits_silently(self):
        (self.tmp / ".flow" / ".current-task").unlink()
        rc = self._run_hook({"cwd": str(self.tmp), "transcript_path": ""})
        self.assertEqual(rc, 0, "hook must never block on missing task")

    def test_no_flow_dir_exits_silently(self):
        outside = Path(tempfile.mkdtemp(prefix="not-flow-"))
        try:
            rc = self._run_hook({"cwd": str(outside), "transcript_path": ""})
            self.assertEqual(rc, 0)
        finally:
            shutil.rmtree(outside, ignore_errors=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
