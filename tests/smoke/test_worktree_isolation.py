#!/usr/bin/env python3
"""Smoke tests for v0.4 multi-task isolation (sub-project #4).

Covers:
  1. Non-git repo + task_isolation=worktree → fall back to shared, no crash
  2. Git repo + task_isolation=worktree → real worktree created + .location written
  3. cmd_status output covers all tasks + reflects blocked_by dependencies
  4. cmd_switch emits valid shell containing `cd <dir>`
  5. archive on worktree-isolated task removes the worktree

Run:
  python3 -m unittest tests.smoke.test_worktree_isolation -v
"""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _reset_modules():
    for mod in (
        "flow_task",
        "common.config",
        "common.paths",
        "common",
    ):
        sys.modules.pop(mod, None)


def _call(fn, **kwargs):
    """Call a flow_task cmd_* with mocked args + capture stdout."""
    class _A:
        pass
    a = _A()
    for k, v in kwargs.items():
        setattr(a, k, v)
    buf = io.StringIO()
    err = io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = buf, err
    try:
        fn(a)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return buf.getvalue(), err.getvalue()


def _has_git() -> bool:
    return shutil.which("git") is not None


class _BaseFlow(unittest.TestCase):
    """Set up a tmp project root with .flow/ + optional git init."""

    is_git: bool = False
    isolation: str = "shared"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-wt-")).resolve()
        # IMPORTANT: parent dir must exist + be writable for sibling worktrees
        self.parent = self.tmp
        self.proj = self.tmp / "myproj"
        self.proj.mkdir()
        if self.is_git:
            subprocess.run(
                ["git", "init", "-q", "-b", "main", str(self.proj)],
                check=True,
            )
            # Need a commit before `worktree add` works
            (self.proj / "README").write_text("hello\n")
            subprocess.run(
                ["git", "-C", str(self.proj), "add", "."],
                check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            env = os.environ.copy()
            env.setdefault("GIT_AUTHOR_NAME", "test")
            env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
            env.setdefault("GIT_COMMITTER_NAME", "test")
            env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
            subprocess.run(
                ["git", "-C", str(self.proj), "commit", "-q", "-m", "init"],
                check=True, env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )

        # .flow/ skeleton
        flow = self.proj / ".flow"
        (flow / "tasks").mkdir(parents=True)
        cfg = f"task_isolation: {self.isolation}\n"
        (flow / "config.yaml").write_text(cfg, encoding="utf-8")

        self._cwd = os.getcwd()
        os.chdir(self.proj)
        _reset_modules()
        import flow_task  # noqa: E402
        self.flow_task = flow_task

    def tearDown(self):
        os.chdir(self._cwd)
        # Clean any sibling worktrees created
        for sib in self.parent.iterdir():
            if sib.name.startswith("myproj-flow-"):
                shutil.rmtree(sib, ignore_errors=True)
        shutil.rmtree(self.tmp, ignore_errors=True)


class NonGitFallback(_BaseFlow):
    """Test 1: non-git repo + worktree mode → falls back to shared (no crash)."""
    is_git = False
    isolation = "worktree"

    def test_create_falls_back_to_shared(self):
        out, err = _call(
            self.flow_task.cmd_create,
            title="Demo",
            slug="demo",
            type="backend",
            complexity="simple",
        )
        # Task dir created
        task_dirs = list((self.proj / ".flow" / "tasks").iterdir())
        task_dirs = [t for t in task_dirs if t.is_dir() and t.name != "archive"]
        self.assertEqual(len(task_dirs), 1, "exactly one task dir should exist")
        td = task_dirs[0]
        self.assertTrue(td.name.endswith("-demo"))
        # .location should point to project root (shared fallback)
        loc_file = td / ".location"
        self.assertTrue(loc_file.is_file(), ".location must be written")
        self.assertEqual(
            Path(loc_file.read_text(encoding="utf-8").strip()).resolve(),
            self.proj.resolve(),
        )
        # Warn line on stderr
        self.assertIn("WARN", err)
        # No sibling worktree dir created
        for sib in self.parent.iterdir():
            self.assertFalse(
                sib.name.startswith("myproj-flow-"),
                f"unexpected worktree {sib}",
            )


@unittest.skipUnless(_has_git(), "git not available")
class GitWorktreeCreated(_BaseFlow):
    """Test 2: git repo + worktree mode → real worktree dir + .location written."""
    is_git = True
    isolation = "worktree"

    def test_worktree_created_and_location_recorded(self):
        out, err = _call(
            self.flow_task.cmd_create,
            title="Feature X",
            slug="feature-x",
            type="backend",
            complexity="moderate",
        )
        # Sibling worktree should exist
        wt = self.parent / "myproj-flow-feature-x"
        self.assertTrue(wt.is_dir(), f"worktree dir must exist: {wt}")
        # .location records its abs path
        task_dirs = [
            t for t in (self.proj / ".flow" / "tasks").iterdir()
            if t.is_dir() and t.name != "archive"
        ]
        self.assertEqual(len(task_dirs), 1)
        td = task_dirs[0]
        loc_file = td / ".location"
        self.assertTrue(loc_file.is_file())
        self.assertEqual(
            Path(loc_file.read_text(encoding="utf-8").strip()).resolve(),
            wt.resolve(),
        )
        # Branch flow/feature-x exists
        r = subprocess.run(
            ["git", "-C", str(self.proj), "rev-parse", "--verify", "flow/feature-x"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        self.assertEqual(r.returncode, 0, "branch flow/feature-x must exist")


class StatusOutput(_BaseFlow):
    """Test 3: cmd_status lists all tasks + reflects blocked_by deps."""
    is_git = False
    isolation = "shared"

    def _make_task(self, slug: str, blockers: list[str] | None = None, status: str = "active", phase: str = "implement"):
        flow = self.proj / ".flow"
        td = flow / "tasks" / f"05-04-{slug}"
        td.mkdir(parents=True)
        # Minimal frontmatter
        bl = ""
        if blockers:
            lines = "\n".join(f"  - {b}" for b in blockers)
            bl = f"blocked_by:\n{lines}\n"
        else:
            bl = "blocked_by: []\n"
        (td / "progress.md").write_text(
            f"---\nslug: {slug}\nstatus: {status}\nphase: {phase}\n{bl}---\n\n# progress\n",
            encoding="utf-8",
        )
        (td / ".location").write_text(str(self.proj) + "\n", encoding="utf-8")
        return td

    def test_status_lists_tasks_and_dependencies(self):
        # base depends on nothing; child blocked_by base
        self._make_task("base", blockers=[], phase="research")
        self._make_task("child", blockers=["base"], phase="triage")
        self._make_task("orphan")

        out, err = _call(self.flow_task.cmd_status)
        # All three task dirs are listed
        self.assertIn("05-04-base", out)
        self.assertIn("05-04-child", out)
        self.assertIn("05-04-orphan", out)
        # child must appear AFTER base (rendered as nested)
        idx_base = out.index("05-04-base")
        idx_child = out.index("05-04-child")
        self.assertLess(idx_base, idx_child, "child should render after its blocker")
        # Status + phase rendered (e.g., "[active/research]")
        self.assertIn("[active/research]", out)


class SwitchOutput(_BaseFlow):
    """Test 4: cmd_switch outputs a valid shell command containing `cd <dir>`."""
    is_git = False
    isolation = "shared"

    def test_switch_emits_cd(self):
        # create a task first (use cmd_create — minimal harness)
        _call(
            self.flow_task.cmd_create,
            title="Switch Test",
            slug="switch-test",
            type="backend",
            complexity="simple",
        )
        out, err = _call(self.flow_task.cmd_switch, slug="switch-test")
        out = out.strip()
        # Single-line `cd <quoted-path>`
        self.assertTrue(out.startswith("cd "), f"unexpected: {out!r}")
        # Should reference a real existing dir (project_root in shared mode)
        # Strip `cd ` prefix and trailing quote handling
        target = out[3:].strip()
        # remove possible single quotes from shlex.quote
        if target.startswith("'") and target.endswith("'"):
            target = target[1:-1].replace("'\\''", "'")
        self.assertTrue(Path(target).is_dir(), f"cd target must exist: {target}")
        self.assertEqual(Path(target).resolve(), self.proj.resolve())
        # current-task pointer should now be set to switch-test
        ptr = (self.proj / ".flow" / ".current-task").read_text(encoding="utf-8").strip()
        self.assertIn("switch-test", ptr)


@unittest.skipUnless(_has_git(), "git not available")
class ArchiveCleansWorktree(_BaseFlow):
    """Test 5: archive removes the managed worktree dir."""
    is_git = True
    isolation = "worktree"

    def test_archive_removes_worktree(self):
        _call(
            self.flow_task.cmd_create,
            title="Cleanup",
            slug="cleanup",
            type="backend",
            complexity="simple",
        )
        wt = self.parent / "myproj-flow-cleanup"
        self.assertTrue(wt.is_dir(), "precondition: worktree must exist")

        _call(self.flow_task.cmd_archive, slug="cleanup")

        # Worktree dir gone after archive
        self.assertFalse(
            wt.exists(),
            f"worktree should have been removed by archive: {wt}",
        )
        # Task moved to archive/
        archive_root = self.proj / ".flow" / "tasks" / "archive"
        archived = list(archive_root.rglob("*-cleanup"))
        self.assertEqual(len(archived), 1, "task dir should be in archive/")


if __name__ == "__main__":
    unittest.main(verbosity=2)
