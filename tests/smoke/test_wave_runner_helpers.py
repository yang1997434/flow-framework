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


if __name__ == "__main__":
    unittest.main()
