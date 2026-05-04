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


class TaskPhaseAdvance(unittest.TestCase):
    """Issue #4: `flow task phase <name>` should advance the phase
    frontmatter field, append an Execute Log row, and validate input."""

    def _create_task(self, cwd: Path) -> Path:
        _init(cwd)
        r = _run(cwd, "create", "smoke", "--slug", "smoke",
                 "--type", "backend", "--complexity", "simple")
        self.assertEqual(r.returncode, 0, r.stderr)
        tasks = [p for p in (cwd / ".flow" / "tasks").iterdir()
                 if p.is_dir() and p.name != "archive"]
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def test_phase_valid_transition(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            task_dir = self._create_task(cwd)
            r = _run(cwd, "phase", "implement")
            self.assertEqual(r.returncode, 0, r.stderr)

            text = (task_dir / "progress.md").read_text(encoding="utf-8")
            # Frontmatter `phase:` field updated
            self.assertRegex(text, r"(?m)^phase:\s+implement\b")
            # Execute Log got the transition row
            self.assertIn("flow task phase", text)
            self.assertIn("triage", text)  # old phase referenced
            self.assertIn("implement", text)  # new phase referenced

    def test_phase_unknown_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            self._create_task(cwd)
            r = _run(cwd, "phase", "bogus-phase-name")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("unknown phase", r.stderr.lower())

    def test_phase_no_active_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _init(cwd)
            # No task created → no .current-task pointer
            r = _run(cwd, "phase", "implement")
            self.assertNotEqual(r.returncode, 0)
            self.assertIn("no active task", r.stderr.lower())

    def test_phase_idempotent_same_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            task_dir = self._create_task(cwd)
            # Default is `triage` per template
            before = (task_dir / "progress.md").read_text(encoding="utf-8")
            r = _run(cwd, "phase", "triage")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("phase already triage", r.stdout)
            after = (task_dir / "progress.md").read_text(encoding="utf-8")
            self.assertEqual(before, after,
                             "no-op phase should not modify progress.md")

    def test_phase_history_jsonl_event_when_checkpoint_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            task_dir = self._create_task(cwd)
            # Pre-create .checkpoint/ so phase command logs to history.jsonl.
            (task_dir / ".checkpoint").mkdir()
            r = _run(cwd, "phase", "research")
            self.assertEqual(r.returncode, 0, r.stderr)
            hist = task_dir / ".checkpoint" / "history.jsonl"
            self.assertTrue(hist.is_file())
            content = hist.read_text(encoding="utf-8")
            self.assertIn("phase_transition", content)
            self.assertIn("research", content)


if __name__ == "__main__":
    unittest.main(verbosity=2)
