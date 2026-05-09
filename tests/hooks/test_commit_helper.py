#!/usr/bin/env python3
"""Smoke tests for v0.8.4 P0.6 commit-helper utility.

Tests the user-installed helper at ~/.claude/hooks/_commit_helper.py
end-to-end via subprocess. Same isolation model as
test_pre_commit_review.py: tmp HOME + tmp git repo so MARKER_PATH
resolves under the tmp HOME (never touches the user's real marker).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


REAL_HOME = Path.home()
HELPER_SRC = REAL_HOME / ".claude/hooks/_commit_helper.py"
WRITER_SRC = REAL_HOME / ".claude/hooks/_marker_writer.py"


def _has_git() -> bool:
    return shutil.which("git") is not None


def _helper_installed() -> bool:
    return HELPER_SRC.exists() and WRITER_SRC.exists()


@unittest.skipUnless(
    _has_git() and _helper_installed(),
    "helper not installed at ~/.claude/hooks/_commit_helper.py",
)
class CommitHelper(unittest.TestCase):
    def setUp(self):
        self.tmp_home = Path(
            tempfile.mkdtemp(prefix="helper-test-home-")
        ).resolve()
        self.tmp_repo = Path(
            tempfile.mkdtemp(prefix="helper-test-repo-")
        ).resolve()
        # Mirror hook layout into tmp_home so MARKER_PATH resolves there.
        (self.tmp_home / ".claude/hooks").mkdir(parents=True, exist_ok=True)
        # Copy (not symlink) so __file__ resolves under tmp_home and the
        # helper's `from _marker_writer import write_marker` finds the
        # tmp_home copy of the writer (sibling import).
        shutil.copy(
            HELPER_SRC, self.tmp_home / ".claude/hooks/_commit_helper.py",
        )
        shutil.copy(
            WRITER_SRC, self.tmp_home / ".claude/hooks/_marker_writer.py",
        )
        # Init git repo with a base commit
        env = self._git_env()
        subprocess.run(
            ["git", "init", "-q", "-b", "main", str(self.tmp_repo)],
            check=True, env=env,
        )
        (self.tmp_repo / "README").write_text("base\n")
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "add", "README"],
            check=True, env=env,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "commit", "-q", "-m", "base"],
            check=True, env=env,
        )

    def tearDown(self):
        shutil.rmtree(self.tmp_home, ignore_errors=True)
        shutil.rmtree(self.tmp_repo, ignore_errors=True)

    # ---- helpers ----

    def _git_env(self):
        env = os.environ.copy()
        env["HOME"] = str(self.tmp_home)
        env["GIT_AUTHOR_NAME"] = "test"
        env["GIT_AUTHOR_EMAIL"] = "test@example.com"
        env["GIT_COMMITTER_NAME"] = "test"
        env["GIT_COMMITTER_EMAIL"] = "test@example.com"
        return env

    def _helper_path(self) -> Path:
        return self.tmp_home / ".claude/hooks/_commit_helper.py"

    def _marker_path(self) -> Path:
        return self.tmp_home / ".claude/hooks/.review-passed.json"

    def _stage_change(self, name="new.txt", content="hello\n"):
        (self.tmp_repo / name).write_text(content)
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "add", name],
            check=True, env=self._git_env(),
        )

    def _run(self, *args, stdin: str = "", cwd: str = None):
        cmd = [sys.executable, str(self._helper_path())] + list(args)
        return subprocess.run(
            cmd,
            input=stdin,
            env=self._git_env(),
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=15,
        )

    # ---- mark subcommand ----

    def test_mark_writes_marker_in_cwd_repo(self):
        """`mark` with no --repo uses CWD."""
        self._stage_change()
        result = self._run("mark", cwd=str(self.tmp_repo))
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r} stdout={result.stdout!r}",
        )
        self.assertTrue(self._marker_path().exists())
        payload = json.loads(self._marker_path().read_text())
        self.assertEqual(payload["schema_version"], 1)
        self.assertEqual(len(payload["tree_sha"]), 40)
        # repo_id should resolve to the tmp_repo's .git
        self.assertIn(str(self.tmp_repo), payload["repo_id"])

    def test_mark_writes_marker_in_named_repo(self):
        """`mark --repo /path` uses that repo regardless of CWD."""
        self._stage_change()
        # Run from /tmp (not from tmp_repo) — --repo points at tmp_repo
        result = self._run(
            "mark", "--repo", str(self.tmp_repo), cwd="/tmp",
        )
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r}",
        )
        self.assertTrue(self._marker_path().exists())
        payload = json.loads(self._marker_path().read_text())
        # Critical: repo_id reflects tmp_repo, NOT /tmp
        self.assertIn(str(self.tmp_repo), payload["repo_id"])

    def test_mark_outputs_tree_sha_to_stdout(self):
        """`mark` prints tree_sha as the only stdout line."""
        self._stage_change()
        result = self._run("mark", cwd=str(self.tmp_repo))
        self.assertEqual(result.returncode, 0)
        sha = result.stdout.strip()
        self.assertEqual(len(sha), 40)
        # Confirm it matches the marker payload
        payload = json.loads(self._marker_path().read_text())
        self.assertEqual(sha, payload["tree_sha"])

    def test_mark_rejects_non_repo_path(self):
        """`mark --repo /tmp` (no .git) → exit 2."""
        non_repo = Path(tempfile.mkdtemp(prefix="not-a-repo-"))
        try:
            result = self._run("mark", "--repo", str(non_repo))
            self.assertEqual(result.returncode, 2, f"stdout={result.stdout!r}")
            self.assertIn(b"no .git" if isinstance(result.stderr, bytes)
                          else "no .git", result.stderr)
        finally:
            shutil.rmtree(non_repo, ignore_errors=True)

    # ---- commit subcommand ----

    def test_commit_with_msg_file(self):
        """`commit --repo /path -F msg-file` runs git commit in repo."""
        self._stage_change()
        # Marker required for the actual git commit (would be blocked
        # without it normally); but here we're testing helper behavior,
        # not the hook integration. Write marker first via `mark`.
        self._run("mark", "--repo", str(self.tmp_repo))
        # Create commit message file
        msg_file = self.tmp_home / "msg.txt"
        msg_file.write_text("test commit subject\n\ntest body\n")
        result = self._run(
            "commit", "--repo", str(self.tmp_repo),
            "-F", str(msg_file),
        )
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r}",
        )
        # Verify the commit landed
        log = subprocess.run(
            ["git", "-C", str(self.tmp_repo), "log", "--oneline", "-1"],
            env=self._git_env(), capture_output=True, text=True,
        )
        self.assertIn("test commit subject", log.stdout)

    def test_commit_with_stdin_message(self):
        """`commit --repo /path --message-stdin` reads stdin → tmpfile."""
        self._stage_change()
        self._run("mark", "--repo", str(self.tmp_repo))
        msg = "stdin commit subject\n\nbody\n"
        result = self._run(
            "commit", "--repo", str(self.tmp_repo),
            "--message-stdin",
            stdin=msg,
        )
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r}",
        )
        log = subprocess.run(
            ["git", "-C", str(self.tmp_repo), "log", "--oneline", "-1"],
            env=self._git_env(), capture_output=True, text=True,
        )
        self.assertIn("stdin commit subject", log.stdout)

    def test_commit_uses_subprocess_cwd_not_git_dash_C(self):
        """Helper must run `git commit` via cwd= subprocess, NOT git -C.

        Inspect the source file: argv passed to subprocess.run for the
        commit subcommand must NOT contain '-C' (which is hook-blocked
        per v0.8.3 P0.4).
        """
        src = self._helper_path().read_text()
        # The actual git commit invocation lives in _do_commit
        self.assertIn('"git", "commit"', src)
        # Confirm no `-C` anywhere in the git argv list
        # (would appear as '"-C"' in the source string)
        # Allow `-C` in docstring/comments (those are clearly not argv).
        # Strict check: no `["git", "-C"` or `"git", "-C"` patterns.
        self.assertNotIn('"git", "-C"', src)
        self.assertNotIn("['git', '-C'", src)

    # ---- mark-commit subcommand ----

    def test_mark_commit_atomic(self):
        """`mark-commit` writes marker AND commits in one call."""
        self._stage_change()
        msg_file = self.tmp_home / "msg.txt"
        msg_file.write_text("atomic mark-commit\n")
        result = self._run(
            "mark-commit", "--repo", str(self.tmp_repo),
            "-F", str(msg_file),
        )
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r}",
        )
        log = subprocess.run(
            ["git", "-C", str(self.tmp_repo), "log", "--oneline", "-1"],
            env=self._git_env(), capture_output=True, text=True,
        )
        self.assertIn("atomic mark-commit", log.stdout)

    # ---- error paths ----

    def test_commit_fails_loud_on_git_error(self):
        """No staged changes → git commit fails → exit 1 + stderr msg."""
        # Write marker (so no marker-related block) but stage NOTHING
        self._run("mark", "--repo", str(self.tmp_repo))
        msg_file = self.tmp_home / "msg.txt"
        msg_file.write_text("empty\n")
        result = self._run(
            "commit", "--repo", str(self.tmp_repo),
            "-F", str(msg_file),
        )
        self.assertEqual(result.returncode, 1)
        self.assertIn("git commit exited", result.stderr)

    def test_commit_unlinks_tmpfile_on_stdin_path(self):
        """After --message-stdin commit, the helper's tmpfile is removed."""
        self._stage_change()
        self._run("mark", "--repo", str(self.tmp_repo))
        # Snapshot tmpdir before
        tmpdir = Path(tempfile.gettempdir())
        before = set(tmpdir.glob("commit-msg-*"))
        result = self._run(
            "commit", "--repo", str(self.tmp_repo),
            "--message-stdin",
            stdin="cleanup test commit\n",
        )
        self.assertEqual(
            result.returncode, 0,
            f"stderr={result.stderr!r}",
        )
        after = set(tmpdir.glob("commit-msg-*"))
        new_files = after - before
        self.assertEqual(
            new_files, set(),
            f"tmpfile leak: {new_files}",
        )


if __name__ == "__main__":
    unittest.main()
