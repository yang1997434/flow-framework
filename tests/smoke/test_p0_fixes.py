#!/usr/bin/env python3
"""Smoke tests for v0.3.1 P0 fixes (audit-flow-issues task #0).

Covers:
  P0-1  pre-tool-task.py:62  — pick_jsonl `or True` removed; default branch preserved
  P0-2  flow_task.py archive — pointer cleared only when archived task IS current
  P0-3  flow_promote.py      — frontmatter rewrite is clean and idempotent

Run:
  python3 -m unittest tests.smoke.test_p0_fixes -v
  or
  bash tests/smoke/run.sh
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_pre_tool_task():
    """`pre-tool-task.py` has a hyphen — needs importlib."""
    spec = importlib.util.spec_from_file_location(
        "pre_tool_task", REPO_ROOT / "claude" / "hooks" / "pre-tool-task.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class P01PickJsonl(unittest.TestCase):
    """P0-1: `or True` removed; pick_jsonl must still default to implement when
    no keywords match (preserve documented behavior)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p01-"))
        self.module = load_pre_tool_task()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, name: str, content: str = "[]"):
        (self.tmp / name).write_text(content, encoding="utf-8")

    def test_no_keywords_defaults_to_implement(self):
        self._write("implement.jsonl")
        result = self.module.pick_jsonl(self.tmp, "do something random and obscure")
        self.assertEqual(result, self.tmp / "implement.jsonl")

    def test_check_keyword_returns_check(self):
        self._write("check.jsonl")
        self._write("implement.jsonl")
        result = self.module.pick_jsonl(self.tmp, "please verify the patch")
        self.assertEqual(result, self.tmp / "check.jsonl")

    def test_check_keyword_falls_back_to_implement_when_no_check_jsonl(self):
        self._write("implement.jsonl")
        result = self.module.pick_jsonl(self.tmp, "please verify the patch")
        self.assertEqual(result, self.tmp / "implement.jsonl")

    def test_no_jsonl_files_returns_none(self):
        result = self.module.pick_jsonl(self.tmp, "implement something")
        self.assertIsNone(result)


class P02ArchivePointer(unittest.TestCase):
    """P0-2: archive must only clear .current-task when the archived task IS
    the current task. Previously, archiving any task wiped the pointer."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p02-")).resolve()
        self.flow = self.tmp / ".flow"
        (self.flow / "tasks").mkdir(parents=True)
        (self.tmp / ".git").mkdir()  # so get_project_root anchors here
        self.task_a = self.flow / "tasks" / "01-01-task-a"
        self.task_b = self.flow / "tasks" / "01-01-task-b"
        self.task_a.mkdir()
        self.task_b.mkdir()
        (self.flow / ".current-task").write_text(str(self.task_a), encoding="utf-8")

        self._cwd = os.getcwd()
        os.chdir(self.tmp)

        # Force fresh import so paths re-resolve under our tmp cwd
        for mod in ("flow_task", "common.paths", "common"):
            sys.modules.pop(mod, None)
        import flow_task  # noqa: E402
        self.flow_task = flow_task

    def tearDown(self):
        os.chdir(self._cwd)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _archive(self, slug: str):
        class _Args:
            pass
        args = _Args()
        args.slug = slug
        # silence cmd_archive's print
        from io import StringIO
        old_stdout = sys.stdout
        sys.stdout = StringIO()
        try:
            self.flow_task.cmd_archive(args)
        finally:
            sys.stdout = old_stdout

    def test_archive_non_current_keeps_pointer(self):
        self._archive("task-b")
        ptr = self.flow / ".current-task"
        self.assertTrue(ptr.is_file(), "pointer must remain when non-current task archived")
        self.assertEqual(ptr.read_text(encoding="utf-8").strip(), str(self.task_a))

    def test_archive_current_clears_pointer(self):
        self._archive("task-a")
        ptr = self.flow / ".current-task"
        self.assertFalse(ptr.exists(), "pointer must be cleared when current task archived")


class P03FrontmatterRewrite(unittest.TestCase):
    """P0-3: rewriting frontmatter must not accumulate blank lines, and must be
    idempotent across re-promotions."""

    @classmethod
    def setUpClass(cls):
        for mod in ("flow_promote", "common.paths", "common"):
            sys.modules.pop(mod, None)
        import flow_promote  # noqa: E402
        cls.fn = staticmethod(flow_promote.rewrite_frontmatter_for_promotion)

    def test_basic_rewrite_has_no_extra_blank_lines(self):
        content = "---\nname: foo\nversion: 1\n---\n# Body\n\nlorem ipsum\n"
        out = self.fn(content, "/path/to/target.md", "2026-05-04")
        self.assertIsNotNone(out)
        self.assertNotIn("---\n\nname:", out, "no blank line after opening ---")
        self.assertNotIn("\n\n---\n", out, "no blank line before closing ---")
        self.assertIn("# Body\n\nlorem ipsum\n", out, "body preserved verbatim")
        self.assertIn("status: promoted", out)
        self.assertIn("promoted_to: /path/to/target.md", out)
        self.assertIn("promoted_date: 2026-05-04", out)

    def test_idempotent_no_blank_accumulation(self):
        once = self.fn("---\nname: foo\n---\nbody\n", "/t1", "2026-05-04")
        twice = self.fn(once, "/t2", "2026-05-05")
        self.assertNotIn("---\n\n", twice)
        self.assertNotIn("\n\n---", twice)
        # Both promotions recorded
        self.assertEqual(twice.count("status: promoted"), 2)

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(self.fn("# just a heading\n", "/t", "2026-05-04"))

    def test_unclosed_frontmatter_returns_none(self):
        self.assertIsNone(self.fn("---\nname: foo\n", "/t", "2026-05-04"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
