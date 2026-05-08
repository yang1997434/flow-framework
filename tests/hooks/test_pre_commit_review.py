#!/usr/bin/env python3
"""Smoke tests for v0.8.3 P0.0 pre-commit-review hook (D''''+SoleRoot+WrapperDetect).

Tests the user-installed hook at ~/.claude/hooks/pre-commit-review.py against
the 13-case acceptance matrix from PRD. Skipped when hook not installed.

Each test:
1. Sets up an isolated tmp git repo (with HOME pointing at a tmp dir so the
   marker path resolves under the tmp HOME, not the user's real home).
2. Optionally writes a marker (via _marker_writer or manually).
3. Invokes the hook with a stdin JSON input describing the Bash tool call.
4. Asserts stdout matches the expected PASS (empty) or BLOCK (JSON deny).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


REAL_HOME = Path.home()
HOOK_SRC = REAL_HOME / ".claude/hooks/pre-commit-review.py"
WRITER_SRC = REAL_HOME / ".claude/hooks/_marker_writer.py"
VENDOR_SRC = REAL_HOME / ".claude/hooks/_vendor"


def _has_git() -> bool:
    return shutil.which("git") is not None


def _hook_installed() -> bool:
    return HOOK_SRC.exists() and WRITER_SRC.exists() and VENDOR_SRC.exists()


@unittest.skipUnless(
    _has_git() and _hook_installed(),
    "hook not installed at ~/.claude/hooks/ — run install or skip",
)
class PreCommitReviewHook(unittest.TestCase):
    """Test the hook end-to-end via subprocess."""

    def setUp(self):
        self.tmp_home = Path(tempfile.mkdtemp(prefix="hook-test-home-")).resolve()
        self.tmp_repo = Path(tempfile.mkdtemp(prefix="hook-test-repo-")).resolve()
        # Mirror the user's hook layout into tmp_home so MARKER_PATH resolves there
        (self.tmp_home / ".claude/hooks/_vendor").mkdir(parents=True, exist_ok=True)
        # Symlink vendor so we don't duplicate 276KB
        target_vendor = self.tmp_home / ".claude/hooks/_vendor/bashlex"
        if not target_vendor.exists():
            target_vendor.symlink_to(VENDOR_SRC / "bashlex")
        # Copy hook + marker writer (so __file__ resolves to tmp_home and MARKER_PATH points there)
        shutil.copy(HOOK_SRC, self.tmp_home / ".claude/hooks/pre-commit-review.py")
        shutil.copy(WRITER_SRC, self.tmp_home / ".claude/hooks/_marker_writer.py")
        # Init git repo with a base commit
        env = self._git_env()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp_repo)], check=True, env=env)
        (self.tmp_repo / "README").write_text("base\n")
        subprocess.run(["git", "-C", str(self.tmp_repo), "add", "README"], check=True, env=env)
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "commit", "-q", "-m", "base"],
            check=True,
            env=env,
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

    def _stage_change(self, name="new.txt", content="hello\n"):
        (self.tmp_repo / name).write_text(content)
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "add", name],
            check=True,
            env=self._git_env(),
        )

    def _write_marker(self):
        env = self._git_env()
        result = subprocess.run(
            [sys.executable, str(self.tmp_home / ".claude/hooks/_marker_writer.py")],
            env=env,
            cwd=str(self.tmp_repo),
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, f"marker writer failed: {result.stderr}")

    def _write_marker_raw(self, payload: dict):
        marker_path = self.tmp_home / ".claude/hooks/.review-passed.json"
        marker_path.write_text(json.dumps(payload))

    def _run_hook(self, command: str):
        """Invoke the hook with a Bash tool-call JSON input; return (stdout, stderr, returncode)."""
        event = {"tool_input": {"command": command}}
        result = subprocess.run(
            [sys.executable, str(self.tmp_home / ".claude/hooks/pre-commit-review.py")],
            input=json.dumps(event),
            env=self._git_env(),
            cwd=str(self.tmp_repo),
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.stdout, result.stderr, result.returncode

    def _assert_pass(self, stdout):
        """PASS = empty stdout (no decision payload)."""
        self.assertEqual(stdout.strip(), "", f"expected PASS but got: {stdout}")

    def _assert_block(self, stdout, reason_substring=None):
        """BLOCK = JSON with permissionDecision=deny."""
        self.assertTrue(stdout.strip(), "expected BLOCK JSON output, got empty")
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError:
            self.fail(f"BLOCK output not JSON: {stdout!r}")
        spec = payload.get("hookSpecificOutput", {})
        self.assertEqual(spec.get("permissionDecision"), "deny", f"unexpected decision: {payload}")
        if reason_substring:
            reason = spec.get("permissionDecisionReason", "")
            self.assertIn(reason_substring, reason, f"reason mismatch: {reason!r}")

    # ---- cases ----

    def test_01_plain_commit_with_valid_marker_PASSES(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_pass(out)

    def test_02_plain_commit_without_marker_BLOCKS(self):
        self._stage_change()
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_block(out, "no review marker")

    def test_03_heredoc_body_with_git_commit_text_BLOCKS_failclosed(self):
        """Quoted heredoc body containing git words → bashlex parse fails → fail-closed BLOCK."""
        cmd = "python3 <<'EOF'\nprint('git commit -m foo')\nEOF\n"
        out, _, _ = self._run_hook(cmd)
        self._assert_block(out, "cannot safely analyze")

    def test_04_compound_commit_BLOCKS_sole_root(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("touch /tmp/x && git commit -m foo")
        self._assert_block(out, "single simple command")

    def test_05a_wrapper_command_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("command git commit -m foo")
        self._assert_block(out, "wrapper")

    def test_05b_wrapper_eval_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook('eval "git commit -m foo"')
        self._assert_block(out, "wrapper")

    def test_05c_wrapper_bash_dash_c_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook('bash -c "git commit -m foo"')
        self._assert_block(out, "wrapper")

    def test_06_amend_with_valid_marker_PASSES(self):
        # amend uses the existing HEAD; ensure marker is for current state
        self._write_marker()
        out, _, _ = self._run_hook("git commit --amend --no-edit")
        self._assert_pass(out)

    def test_07_marker_schema_99_BLOCKS(self):
        self._stage_change()
        self._write_marker_raw({"schema_version": 99, "ts": int(time.time())})
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_block(out, "schema_version")

    def test_08_inline_alias_via_dash_c_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git -c alias.ci=commit ci -m foo")
        self._assert_block(out, "alias")

    def test_09_command_substitution_in_message_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook('git commit -m "$(date)"')
        self._assert_block(out, "substitution")

    def test_10_HEAD_changed_since_marker_BLOCKS(self):
        # Write marker, then move HEAD via empty commit
        self._stage_change()
        self._write_marker()
        env = self._git_env()
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "commit", "-q", "--allow-empty", "-m", "intervening"],
            check=True,
            env=env,
        )
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_block(out, "HEAD")

    def test_11_tree_changed_since_marker_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        # Stage another change after marker
        (self.tmp_repo / "more.txt").write_text("more\n")
        subprocess.run(
            ["git", "-C", str(self.tmp_repo), "add", "more.txt"],
            check=True,
            env=self._git_env(),
        )
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_block(out, "staged content")

    def test_12_env_prefix_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("PATH=. git commit -m foo")
        self._assert_block(out, "env-prefixed")

    def test_13_pass_consumes_marker_single_use(self):
        self._stage_change()
        self._write_marker()
        marker_path = self.tmp_home / ".claude/hooks/.review-passed.json"
        self.assertTrue(marker_path.exists())
        out, _, _ = self._run_hook("git commit -m foo")
        self._assert_pass(out)
        self.assertFalse(marker_path.exists(), "marker should be unlinked after PASS")

    def test_14_non_git_command_PASSES(self):
        out, _, _ = self._run_hook("ls /tmp")
        self._assert_pass(out)

    def test_15_git_status_PASSES(self):
        out, _, _ = self._run_hook("git status")
        self._assert_pass(out)

    def test_16_dash_a_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git commit -a -m foo")
        self._assert_block(out, "white-list")

    def test_17_dash_am_compact_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git commit -am foo")
        self._assert_block(out, "white-list")

    def test_18_pathspec_BLOCKS(self):
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git commit -m foo README")
        self._assert_block(out, "white-list")

    def test_20a_git_dash_C_path_commit_BLOCKS(self):
        """v0.8.3 P0.4 fix — `git -C /path commit` must BLOCK (was bypassing)."""
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git -C /tmp/x commit -m foo")
        self._assert_block(out, "global options before")

    def test_20b_git_git_dir_commit_BLOCKS(self):
        """v0.8.3 P0.4 fix — `git --git-dir=.git commit` must BLOCK."""
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git --git-dir=.git commit -m foo")
        self._assert_block(out, "global options before")

    def test_20c_git_work_tree_commit_BLOCKS(self):
        """v0.8.3 P0.4 fix — `git --work-tree=. commit` must BLOCK."""
        self._stage_change()
        self._write_marker()
        out, _, _ = self._run_hook("git --work-tree=. commit -m foo")
        self._assert_block(out, "global options before")

    def test_20d_git_status_with_dash_C_PASSES(self):
        """v0.8.3 P0.4 — non-commit git with -C still passes (commit not in argv[2:])."""
        out, _, _ = self._run_hook("git -C /tmp/x status")
        self._assert_pass(out)

    def test_19_unlink_failure_BLOCKS(self):
        """When marker unlink fails (e.g. read-only parent dir), hook must BLOCK
        to preserve single-use semantics rather than silently PASS."""
        self._stage_change()
        self._write_marker()
        marker_dir = self.tmp_home / ".claude/hooks"
        original_mode = marker_dir.stat().st_mode
        os.chmod(marker_dir, 0o555)  # read+execute, no write → unlink will fail
        try:
            out, _, _ = self._run_hook("git commit -m foo")
            self._assert_block(out, "consume marker")
        finally:
            os.chmod(marker_dir, original_mode)


if __name__ == "__main__":
    unittest.main()
