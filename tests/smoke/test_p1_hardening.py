#!/usr/bin/env python3
"""Smoke tests for v0.4.1 P1 hardening pass.

Covers the 5 issues raised in pre-merge review:
  P1-1 flow_install._resolves_into_source detects nested symlinks into REPO_ROOT
  P1-2 flow_task.cmd_archive aborts on dirty worktree without --force
  P1-3 hook heartbeat is fire-and-forget (Popen, not run+timeout)
  P1-4 post-tool-bash.is_git_commit_command handles multi-space + `git -C` forms
  P1-5 flow_doctor.check_hook_isolation flags cross-entry siblings under same matcher
"""
from __future__ import annotations

import importlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _has_git() -> bool:
    return shutil.which("git") is not None


# ----------------------------------------------------------------------
# P1-1: nested symlink guard in flow_install
# ----------------------------------------------------------------------

class P1_1_NestedSymlinkGuard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_install", None)
        cls.mod = importlib.import_module("flow_install")

    def test_resolves_into_source_detects_direct_symlink(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            link = Path(tmp) / "dst"
            link.symlink_to(src)
            result = self.mod._resolves_into_source(link, src.resolve())
            self.assertEqual(result, src.resolve())

    def test_resolves_into_source_detects_nested_dir_symlink(self):
        """A symlink several levels deep that points back into source must be detected."""
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            (src / "templates").mkdir()
            dst_root = Path(tmp) / "dst"
            dst_root.mkdir()
            # dst/sub is a symlink to src/templates
            (dst_root / "sub").symlink_to(src / "templates")
            target_file = dst_root / "sub" / "would-clobber.md"
            result = self.mod._resolves_into_source(target_file, src.resolve())
            self.assertIsNotNone(result, "nested symlink into source must be detected")
            self.assertTrue(str(result).startswith(str(src.resolve())))

    def test_resolves_into_source_returns_none_for_safe_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            src = Path(tmp) / "src"
            src.mkdir()
            safe = Path(tmp) / "elsewhere" / "fine.md"
            self.assertIsNone(self.mod._resolves_into_source(safe, src.resolve()))


# ----------------------------------------------------------------------
# P1-2: archive must not silently discard uncommitted work
# ----------------------------------------------------------------------

@unittest.skipUnless(_has_git(), "git not available")
class P1_2_ArchiveDirtyWorktreeGuard(unittest.TestCase):
    """Set up a real git repo + worktree-isolated task, dirty the worktree,
    then verify archive aborts unless --force is given."""

    def setUp(self):
        # Reset modules so each test gets fresh paths
        for m in ("flow_task", "common.config", "common.paths", "common"):
            sys.modules.pop(m, None)
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p1-2-")).resolve()
        self.proj = self.tmp / "myproj"
        self.proj.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.proj)], check=True)
        (self.proj / "README").write_text("hello\n")
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "test")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "test")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        subprocess.run(
            ["git", "-C", str(self.proj), "add", "."],
            check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        subprocess.run(
            ["git", "-C", str(self.proj), "commit", "-q", "-m", "init"],
            check=True, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        flow = self.proj / ".flow"
        (flow / "tasks").mkdir(parents=True)
        (flow / "config.yaml").write_text("task_isolation: worktree\n", encoding="utf-8")
        # Switch CWD so flow_task picks up this project
        self._old_cwd = Path.cwd()
        os.chdir(self.proj)
        self.mod = importlib.import_module("flow_task")

    def tearDown(self):
        os.chdir(self._old_cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _make_args(self, **kw):
        class A: pass
        a = A()
        for k, v in kw.items():
            setattr(a, k, v)
        return a

    def _run(self, fn, **kw):
        buf, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = buf, err
        rc = 0
        try:
            try:
                fn(self._make_args(**kw))
            except SystemExit as e:
                rc = e.code or 0
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        return rc, buf.getvalue(), err.getvalue()

    def test_dirty_worktree_aborts_without_force(self):
        # Create a worktree-isolated task
        self._run(
            self.mod.cmd_create,
            title="Dirty",
            slug="dirty-test",
            type="backend",
            complexity="simple",
        )
        wt = self.tmp / "myproj-flow-dirty-test"
        self.assertTrue(wt.is_dir(), "precondition: worktree must exist")

        # Dirty the worktree with an untracked file
        (wt / "uncommitted.txt").write_text("would be lost\n")
        self.assertTrue(self.mod._worktree_is_dirty(wt))

        # Archive without --force should exit non-zero
        rc, _out, errtxt = self._run(self.mod.cmd_archive, slug="dirty-test", force=False)
        self.assertNotEqual(rc, 0, "archive must abort on dirty worktree")
        self.assertIn("uncommitted", errtxt.lower())
        # Worktree still present (not removed)
        self.assertTrue(wt.exists(), "worktree must NOT be removed on dirty-abort")

    def test_dirty_worktree_proceeds_with_force(self):
        self._run(
            self.mod.cmd_create,
            title="Dirty2",
            slug="dirty2",
            type="backend",
            complexity="simple",
        )
        wt = self.tmp / "myproj-flow-dirty2"
        (wt / "uncommitted.txt").write_text("would be lost\n")
        rc, _out, _err = self._run(self.mod.cmd_archive, slug="dirty2", force=True)
        self.assertEqual(rc, 0)
        self.assertFalse(wt.exists(), "worktree should be force-removed")

    def test_clean_worktree_archives_without_force(self):
        self._run(
            self.mod.cmd_create,
            title="Clean",
            slug="clean-test",
            type="backend",
            complexity="simple",
        )
        wt = self.tmp / "myproj-flow-clean-test"
        rc, _out, _err = self._run(self.mod.cmd_archive, slug="clean-test", force=False)
        self.assertEqual(rc, 0, "clean worktree must archive without --force")
        self.assertFalse(wt.exists())


# ----------------------------------------------------------------------
# P1-3: heartbeat is fire-and-forget — verify it uses Popen, not run().
# We import the hook modules and assert bump_heartbeat doesn't call run.
# ----------------------------------------------------------------------

class P1_3_HeartbeatFireAndForget(unittest.TestCase):
    """Static check: the source of bump_heartbeat must use Popen, not run().
    Functional check: invocation returns essentially instantly."""

    def test_post_tool_bash_uses_popen(self):
        src = (REPO_ROOT / "claude" / "hooks" / "post-tool-bash.py").read_text()
        # Find bump_heartbeat body
        start = src.index("def bump_heartbeat(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("Popen", body, "bump_heartbeat must use subprocess.Popen")
        self.assertNotIn("subprocess.run", body, "bump_heartbeat must NOT use subprocess.run")

    def test_post_tool_edit_uses_popen(self):
        src = (REPO_ROOT / "claude" / "hooks" / "post-tool-edit.py").read_text()
        start = src.index("def bump_heartbeat(")
        end = src.index("\ndef ", start + 1)
        body = src[start:end]
        self.assertIn("Popen", body)
        self.assertNotIn("subprocess.run", body)

    def test_bump_heartbeat_invokes_popen_with_detached_stdio(self):
        """Behavioural: load post-tool-bash as a module and verify bump_heartbeat
        actually calls subprocess.Popen with start_new_session + DEVNULL stdio."""
        import importlib.util
        path = REPO_ROOT / "claude" / "hooks" / "post-tool-bash.py"
        spec = importlib.util.spec_from_file_location("post_tool_bash_pop", path)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)

        # Make FLOW_AUTOSAVE point to a real file so bump_heartbeat doesn't early-return
        with tempfile.TemporaryDirectory() as tmp:
            fake_autosave = Path(tmp) / "flow_autosave.py"
            fake_autosave.write_text("# stub\n")
            with mock.patch.object(mod, "FLOW_AUTOSAVE", fake_autosave), \
                 mock.patch.object(mod.subprocess, "Popen") as mPopen:
                mod.bump_heartbeat(Path(tmp))
                self.assertEqual(mPopen.call_count, 1, "Popen must be called exactly once")
                _args, kwargs = mPopen.call_args
                self.assertTrue(
                    kwargs.get("start_new_session"),
                    "must detach via start_new_session=True",
                )
                self.assertEqual(kwargs.get("stdin"), mod.subprocess.DEVNULL)
                self.assertEqual(kwargs.get("stdout"), mod.subprocess.DEVNULL)
                self.assertEqual(kwargs.get("stderr"), mod.subprocess.DEVNULL)


# ----------------------------------------------------------------------
# P1-4: shlex-based git commit detection
# ----------------------------------------------------------------------

class P1_4_GitCommitDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # Import the hook as a module (despite the dash in filename)
        import importlib.util
        path = REPO_ROOT / "claude" / "hooks" / "post-tool-bash.py"
        spec = importlib.util.spec_from_file_location("post_tool_bash", path)
        cls.mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.mod)

    def test_simple_form(self):
        self.assertTrue(self.mod.is_git_commit_command("git commit -m 'fix'"))

    def test_multi_space(self):
        self.assertTrue(self.mod.is_git_commit_command("git   commit -m foo"))
        self.assertTrue(self.mod.is_git_commit_command("git\tcommit -m foo"))

    def test_git_dash_C_form(self):
        self.assertTrue(self.mod.is_git_commit_command("git -C /tmp/repo commit -m bar"))

    def test_compound_command(self):
        self.assertTrue(self.mod.is_git_commit_command("cd foo && git commit -m baz"))

    def test_negative_branch(self):
        self.assertFalse(self.mod.is_git_commit_command("git status"))
        self.assertFalse(self.mod.is_git_commit_command("ls && cat README"))

    def test_unbalanced_quote_falls_back(self):
        # Should not crash; falls back to whitespace split
        result = self.mod.is_git_commit_command("git commit -m \"unfinished")
        self.assertTrue(result)

    def test_ignores_substring_in_unrelated_token(self):
        # Things that *contain* "git commit" as substring but aren't the call
        self.assertFalse(self.mod.is_git_commit_command("echo 'git committed earlier'"))

    def test_echo_unquoted_is_not_a_commit(self):
        """Unquoted `echo git commit foo` shlex-tokenises to ['echo','git','commit','foo'].
        The segment-start guard must reject it because `git` follows `echo`, not a
        shell separator."""
        self.assertFalse(self.mod.is_git_commit_command("echo git commit -m foo"))
        self.assertFalse(self.mod.is_git_commit_command("printf %s git commit"))

    def test_compound_with_echo_then_real_commit(self):
        """`echo hi && git commit` — the `git` token IS at segment start (after `&&`)."""
        self.assertTrue(self.mod.is_git_commit_command("echo hi && git commit -m foo"))


# ----------------------------------------------------------------------
# P1-5: doctor flags cross-entry siblings under the same matcher
# ----------------------------------------------------------------------

class P1_5_DoctorCrossEntrySibling(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_doctor", None)
        cls.mod = importlib.import_module("flow_doctor")

    def _patched_doctor(self, settings: dict):
        """Run check_hook_isolation against an in-memory settings dict."""
        with tempfile.TemporaryDirectory() as tmp:
            user_settings = Path(tmp) / "settings.json"
            user_settings.write_text(json.dumps(settings), encoding="utf-8")
            with mock.patch.object(self.mod, "USER_SETTINGS", user_settings):
                buf = io.StringIO()
                old = sys.stdout
                sys.stdout = buf
                try:
                    rc = self.mod.check_hook_isolation()
                finally:
                    sys.stdout = old
                return rc, buf.getvalue()

    def test_cross_entry_sibling_violation_detected(self):
        repo = str(self.mod.REPO_ROOT)
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": f"python3 {repo}/claude/hooks/post-tool-bash.py"}],
                    },
                    {
                        "matcher": "Bash",  # SAME matcher — sibling
                        "hooks": [{"type": "command",
                                   "command": "/usr/bin/some-other-hook"}],
                    },
                ],
            }
        }
        rc, out = self._patched_doctor(settings)
        self.assertEqual(rc, 2, f"expected violation exit code 2; out:\n{out}")
        self.assertIn("sibling", out.lower())

    def test_isolated_layout_passes(self):
        repo = str(self.mod.REPO_ROOT)
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": f"python3 {repo}/claude/hooks/post-tool-bash.py"}],
                    },
                    {
                        "matcher": "Edit",  # different matcher — fine
                        "hooks": [{"type": "command",
                                   "command": "/usr/bin/some-other-hook"}],
                    },
                ],
            }
        }
        rc, _out = self._patched_doctor(settings)
        self.assertEqual(rc, 0, "isolated entries should pass")

    def test_two_flow_siblings_under_same_matcher_pass(self):
        """Two flow-owned entries under the same matcher are fine — they're
        both ours, and Issue #415 is about flow + non-flow co-residency."""
        repo = str(self.mod.REPO_ROOT)
        settings = {
            "hooks": {
                "PostToolUse": [
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": f"python3 {repo}/claude/hooks/post-tool-bash.py"}],
                    },
                    {
                        "matcher": "Bash",
                        "hooks": [{"type": "command",
                                   "command": f"python3 {repo}/claude/hooks/post-tool-edit.py"}],
                    },
                ],
            }
        }
        rc, _out = self._patched_doctor(settings)
        self.assertEqual(rc, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
