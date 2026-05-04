#!/usr/bin/env python3
"""Smoke tests for `flow sediment` CLI — Issue #5."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
FLOW_INIT = REPO_ROOT / "scripts" / "flow_init.py"
FLOW_TASK = REPO_ROOT / "scripts" / "flow_task.py"
FLOW_SEDIMENT = REPO_ROOT / "scripts" / "flow_sediment.py"


def _env() -> dict:
    e = os.environ.copy()
    e["FLOW_HOME"] = str(REPO_ROOT)
    return e


def _run_init(cwd: Path) -> None:
    subprocess.run(
        [sys.executable, str(FLOW_INIT)],
        cwd=str(cwd), env=_env(),
        capture_output=True, text=True, check=True,
    )


def _run_task(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FLOW_TASK), *args],
        cwd=str(cwd), env=_env(),
        capture_output=True, text=True,
    )


def _run_sediment(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(FLOW_SEDIMENT), *args],
        cwd=str(cwd), env=_env(),
        capture_output=True, text=True,
    )


def _ensure_active_task(cwd: Path, slug: str = "smoke") -> Path:
    _run_init(cwd)
    r = _run_task(cwd, "create", "smoke", "--slug", slug,
                  "--type", "backend", "--complexity", "simple")
    assert r.returncode == 0, r.stderr
    tasks = [p for p in (cwd / ".flow" / "tasks").iterdir()
             if p.is_dir() and p.name != "archive"]
    assert len(tasks) == 1
    return tasks[0]


class SedimentRendering(unittest.TestCase):
    """Render pitfall / pattern / ADR templates to .flow/<dir>/<slug>.md."""

    def test_pitfall_renders_to_pitfalls_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _ensure_active_task(cwd)
            r = _run_sediment(cwd, "pitfall", "ascii-fold-bug",
                              "--severity", "high",
                              "--trigger-paths", "**/slugify*.py,**/normalize*.py")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = cwd / ".flow" / "pitfalls" / "ascii-fold-bug.md"
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn("ascii-fold-bug", text)
            self.assertRegex(text, r"(?m)^severity:\s+high\b")
            self.assertIn("**/slugify*.py", text)

    def test_pattern_renders_to_patterns_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _ensure_active_task(cwd)
            r = _run_sediment(cwd, "pattern", "use-charclass-not-w",
                              "--tier", "project")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = cwd / ".flow" / "patterns" / "use-charclass-not-w.md"
            self.assertTrue(out.is_file())
            text = out.read_text(encoding="utf-8")
            self.assertIn("use-charclass-not-w", text)
            self.assertRegex(text, r"(?m)^tier:\s+project\b")


class AdrAutoNumbering(unittest.TestCase):
    """ADRs auto-prefix with `\\d{4}-` when slug doesn't already have one."""

    def test_adr_auto_numbers_to_next_available(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _ensure_active_task(cwd)
            adr_dir = cwd / ".flow" / "ADRs"
            adr_dir.mkdir(parents=True, exist_ok=True)
            (adr_dir / "0001-foo.md").write_text("stub", encoding="utf-8")
            (adr_dir / "0002-bar.md").write_text("stub", encoding="utf-8")
            r = _run_sediment(cwd, "adr", "baz")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertTrue((adr_dir / "0003-baz.md").is_file(),
                            f"ADRs dir contents: {list(adr_dir.iterdir())}")

    def test_adr_explicit_prefix_respected(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _ensure_active_task(cwd)
            r = _run_sediment(cwd, "adr", "0042-explicit")
            self.assertEqual(r.returncode, 0, r.stderr)
            out = cwd / ".flow" / "ADRs" / "0042-explicit.md"
            self.assertTrue(out.is_file())
            # No auto-renumbering: only the requested file exists.
            adr_files = [p.name for p in out.parent.iterdir()
                         if p.is_file() and p.name.endswith(".md")]
            self.assertEqual(adr_files, ["0042-explicit.md"])


class SedimentProgressLink(unittest.TestCase):
    """Active task's progress.md `## Sediment Notes` gets a link entry."""

    def test_progress_md_link_appended_when_active_task_exists(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            task_dir = _ensure_active_task(cwd)
            r = _run_sediment(cwd, "pitfall", "linked-pitfall")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("Linked to active task progress.md", r.stdout)
            text = (task_dir / "progress.md").read_text(encoding="utf-8")
            # Link must appear under the Sediment Notes heading.
            sn_idx = text.find("## Sediment Notes")
            self.assertGreaterEqual(sn_idx, 0)
            tail = text[sn_idx:]
            self.assertIn("linked-pitfall", tail)
            self.assertIn("pitfall:", tail)
            # Relative path used so link is portable.
            self.assertIn("../../pitfalls/linked-pitfall.md", tail)

    def test_no_active_task_just_creates_file_without_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _run_init(cwd)
            # Don't create a task — no .current-task pointer.
            r = _run_sediment(cwd, "pitfall", "no-task-pitfall")
            self.assertEqual(r.returncode, 0, r.stderr)
            self.assertIn("no active task", r.stdout)
            self.assertTrue(
                (cwd / ".flow" / "pitfalls" / "no-task-pitfall.md").is_file()
            )


class SedimentValidation(unittest.TestCase):
    def test_unknown_type_exits_with_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            cwd = Path(tmp)
            _ensure_active_task(cwd)
            r = _run_sediment(cwd, "foobar", "baz")
            self.assertNotEqual(r.returncode, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
