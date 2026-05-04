#!/usr/bin/env python3
"""Smoke tests for `flow task` CLI — Issue #3 archive slug + finish ordering."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOW_INIT = REPO_ROOT / "scripts" / "flow_init.py"
FLOW_TASK = REPO_ROOT / "scripts" / "flow_task.py"


def _run(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    """Run flow_task.py with the given args inside cwd, FLOW_HOME pinned to repo."""
    env = os.environ.copy()
    env["FLOW_HOME"] = str(REPO_ROOT)
    return subprocess.run(
        [sys.executable, str(FLOW_TASK), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )


def _init(cwd: Path) -> None:
    env = os.environ.copy()
    env["FLOW_HOME"] = str(REPO_ROOT)
    subprocess.run(
        [sys.executable, str(FLOW_INIT)],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )


class ArchiveAcceptsBothSlugForms(unittest.TestCase):
    """Issue #3: `flow task archive` should accept both the bare slug
    (e.g. `foo`) and the dated form printed by `flow task list`
    (e.g. `05-04-foo`)."""

    def test_dated_slug_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _init(cwd)
            r = _run(cwd, "create", "smoke", "--slug", "smoke",
                     "--type", "backend", "--complexity", "simple")
            self.assertEqual(r.returncode, 0, r.stderr)
            # find the actual dir name (date prefix may shift across midnight)
            tasks = [p.name for p in (cwd / ".flow" / "tasks").iterdir()
                     if p.is_dir() and p.name != "archive"]
            self.assertEqual(len(tasks), 1)
            full_name = tasks[0]  # e.g. "05-04-smoke"
            self.assertTrue(full_name.endswith("-smoke"))

            _run(cwd, "finish")
            r = _run(cwd, "archive", full_name)
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Archived:", r.stdout)

    def test_bare_slug_form(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _init(cwd)
            r = _run(cwd, "create", "smoke", "--slug", "smoke",
                     "--type", "backend", "--complexity", "simple")
            self.assertEqual(r.returncode, 0, r.stderr)

            _run(cwd, "finish")
            r = _run(cwd, "archive", "smoke")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Archived:", r.stdout)


class FinishLeavesCurrentTaskIntact(unittest.TestCase):
    """Issue #3: `flow task finish` should NOT clear .current-task —
    archive needs it to find the task to move."""

    def test_finish_does_not_clear_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _init(cwd)
            _run(cwd, "create", "smoke", "--slug", "smoke",
                 "--type", "backend", "--complexity", "simple")
            ptr = cwd / ".flow" / ".current-task"
            self.assertTrue(ptr.is_file())

            r = _run(cwd, "finish")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue(ptr.is_file(),
                            "finish must leave .current-task intact")

    def test_archive_clears_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _init(cwd)
            _run(cwd, "create", "smoke", "--slug", "smoke",
                 "--type", "backend", "--complexity", "simple")
            ptr = cwd / ".flow" / ".current-task"
            _run(cwd, "finish")
            r = _run(cwd, "archive", "smoke")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertFalse(ptr.is_file(),
                             "archive should clear .current-task after a successful mv")


if __name__ == "__main__":
    unittest.main(verbosity=2)
