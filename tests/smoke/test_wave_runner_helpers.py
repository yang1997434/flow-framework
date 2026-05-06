"""Smoke tests for flow_wave_runner helpers."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_wave_runner import diff_names_between_shas, verify_subset_of_writes  # noqa: E402


def _run(cwd, *cmd):
    return subprocess.check_output(cmd, cwd=cwd, stderr=subprocess.STDOUT, text=True).strip()


class TestDiffBetweenShas(unittest.TestCase):
    """Test the per-task diff helper using a real throwaway repo."""

    def setUp(self):
        self.repo = tempfile.mkdtemp()
        _run(self.repo, "git", "init", "-q")
        _run(self.repo, "git", "config", "user.email", "t@t.test")
        _run(self.repo, "git", "config", "user.name", "Test")
        # Initial commit
        (Path(self.repo) / "README.md").write_text("hello")
        _run(self.repo, "git", "add", "README.md")
        _run(self.repo, "git", "commit", "-q", "-m", "init")

    def test_per_task_diff_isolates_one_commit(self):
        # SHA A: initial
        sha_a = _run(self.repo, "git", "rev-parse", "HEAD")
        # Commit 1: add foo.py
        (Path(self.repo) / "foo.py").write_text("print(1)")
        _run(self.repo, "git", "add", "foo.py")
        _run(self.repo, "git", "commit", "-q", "-m", "add foo")
        sha_b = _run(self.repo, "git", "rev-parse", "HEAD")
        # Commit 2: add bar.py
        (Path(self.repo) / "bar.py").write_text("print(2)")
        _run(self.repo, "git", "add", "bar.py")
        _run(self.repo, "git", "commit", "-q", "-m", "add bar")
        sha_c = _run(self.repo, "git", "rev-parse", "HEAD")

        # diff a..b shows only foo.py
        files_ab = diff_names_between_shas(self.repo, sha_a, sha_b)
        self.assertEqual(set(files_ab), {"foo.py"})

        # diff b..c shows only bar.py (NOT foo.py)
        files_bc = diff_names_between_shas(self.repo, sha_b, sha_c)
        self.assertEqual(set(files_bc), {"bar.py"})


class TestVerifySubset(unittest.TestCase):
    def test_strict_subset_pass(self):
        ok, violations = verify_subset_of_writes(
            actual=["src/auth/login.py", "src/auth/logout.py"],
            declared=["src/auth/**"],
        )
        self.assertTrue(ok)
        self.assertEqual(violations, [])

    def test_strict_subset_fail_undeclared_file(self):
        ok, violations = verify_subset_of_writes(
            actual=["src/auth/login.py", "package-lock.json"],  # undeclared
            declared=["src/auth/**"],
        )
        self.assertFalse(ok)
        self.assertEqual(violations, ["package-lock.json"])

    def test_empty_actual_pass(self):
        ok, violations = verify_subset_of_writes(actual=[], declared=["src/auth/**"])
        self.assertTrue(ok)


class TestWaveRunnerCLIPathResolution(unittest.TestCase):
    """Regression for v0.7.1 — `cli_diff_names` and `cli_waive` previously
    fell back to the framework REPO_ROOT (instead of the user's project)
    when invoked from outside the framework checkout, so `--repo`-omitted
    invocations and waiver logs were misdirected.
    """

    def setUp(self):
        import shutil
        self._cwd = Path.cwd()
        self.tmpdir = Path(tempfile.mkdtemp()).resolve()
        self.addCleanup(shutil.rmtree, self.tmpdir, ignore_errors=True)
        self.addCleanup(os.chdir, self._cwd)

    def test_cli_diff_names_defaults_to_project_root(self):
        from flow_wave_runner import cli_diff_names
        (self.tmpdir / ".flow").mkdir()
        os.chdir(self.tmpdir)
        _run(self.tmpdir, "git", "init", "-q")
        _run(self.tmpdir, "git", "config", "user.email", "t@t.test")
        _run(self.tmpdir, "git", "config", "user.name", "Test")
        (self.tmpdir / "a.txt").write_text("a")
        _run(self.tmpdir, "git", "add", "-A")
        _run(self.tmpdir, "git", "commit", "-q", "-m", "init")
        head = _run(self.tmpdir, "git", "rev-parse", "HEAD")

        class A:
            repo = None  # default → must fall back to project root, not framework
            pre = head
            post = head
        rc = cli_diff_names(A())
        self.assertEqual(rc, 0)

    def test_cli_waive_log_lands_in_project(self):
        from flow_wave_runner import cli_waive
        (self.tmpdir / ".flow").mkdir()
        os.chdir(self.tmpdir)

        class A:
            task_slug = "my-task"
            task_id = "T1"
            state = "failed_minor"
            rationale = "explained reason"
        rc = cli_waive(A())
        self.assertEqual(rc, 0)
        log_path = self.tmpdir / ".flow" / "tasks" / "my-task" / "wave-decisions.log"
        self.assertTrue(log_path.is_file(), f"waiver log should be in project: {log_path}")
        self.assertIn("WAIVE task=T1", log_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
