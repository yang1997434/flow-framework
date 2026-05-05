#!/usr/bin/env python3
"""Smoke tests for `claude/hooks/user-prompt-submit.py` — phase determination.

Covers (per fix branch fix/sonnet-alias-and-phase-state):
  - is_section_filled correctly filters autosave breadcrumbs (all observed
    formats: bare, with trigger=, with trailing em-dash note)
  - determine_phase enforces sequential AND-chain (later-section content
    alone must NOT advance phase past earlier empty sections)
  - regression test for the "Sediment-only autosave → done" bug
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def load_ups():
    spec = importlib.util.spec_from_file_location(
        "ups", REPO_ROOT / "claude" / "hooks" / "user-prompt-submit.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_progress_md(content: str) -> Path:
    """Write `content` to a temp .md file and return its Path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".md", delete=False, encoding="utf-8")
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


class IsSectionFilled(unittest.TestCase):
    """`is_section_filled` must filter all autosave breadcrumb variants."""

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def assertNotFilled(self, content: str, label: str = ""):
        self.assertFalse(self.ups.is_section_filled(content), label)

    def assertFilled(self, content: str, label: str = ""):
        self.assertTrue(self.ups.is_section_filled(content), label)

    def test_template_comment_only_is_empty(self):
        self.assertNotFilled("<!-- TEMPLATE: placeholder -->\n")

    def test_blank_only_is_empty(self):
        self.assertNotFilled("\n\n   \n")

    def test_bare_breadcrumb_is_empty(self):
        self.assertNotFilled("- [2026-05-05 01:11] distill queued\n")

    def test_breadcrumb_with_trigger_is_empty(self):
        self.assertNotFilled("- [2026-05-05 01:11] distill queued (trigger=stop)\n")

    def test_breadcrumb_with_trigger_and_note_is_empty(self):
        # The exact format the codex review flagged as failing the prefix-only regex.
        content = "- [2026-05-05 01:18] distill queued (trigger=heartbeat) — after 70 tool calls\n"
        self.assertNotFilled(
            content,
            "regex must consume the FULL breadcrumb line including trailing trigger/note",
        )

    def test_multiple_breadcrumbs_all_empty(self):
        content = (
            "- [2026-05-05 01:11] distill queued\n"
            "- [2026-05-05 01:18] distill queued (trigger=stop)\n"
            "- [2026-05-05 01:42] distill queued (trigger=heartbeat) — after 70 tool calls\n"
        )
        self.assertNotFilled(content)

    def test_real_user_content_is_filled(self):
        self.assertFilled("- I added a real note here.\n")

    def test_breadcrumbs_plus_real_content_is_filled(self):
        # Breadcrumbs filtered, but the real line remains → filled.
        content = (
            "- [2026-05-05 01:11] distill queued (trigger=stop)\n"
            "- This is a real human-written note.\n"
        )
        self.assertFilled(content)

    def test_non_breadcrumb_with_timestamp_prefix_is_filled(self):
        # If the user happens to write a real note with a timestamp prefix that
        # doesn't say "distill queued", it must NOT be filtered out.
        content = "- [2026-05-05 02:00] manual: kicked off Phase 4 review.\n"
        self.assertFilled(content)


class DeterminePhaseSequential(unittest.TestCase):
    """`determine_phase` must require sequential filling. A later-section
    write alone must NOT advance phase past earlier empty sections."""

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def _phase_of(self, content: str) -> str:
        p = make_progress_md(content)
        try:
            return self.ups.determine_phase(p)
        finally:
            p.unlink()

    def test_no_progress_file_is_phase1(self):
        self.assertEqual(self.ups.determine_phase(Path("/tmp/__nonexistent")), "phase1-plan")

    def test_all_template_is_phase1(self):
        content = (
            "## Plan\n<!-- TEMPLATE -->\n"
            "## Execute Log\n<!-- TEMPLATE -->\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase1-plan")

    def test_plan_only_is_phase2(self):
        content = (
            "## Plan\n- Plan content.\n"
            "## Execute Log\n<!-- TEMPLATE -->\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase2-execute")

    def test_plan_and_execute_is_phase3(self):
        content = (
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Did the thing.\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase3-finish")

    def test_plan_execute_verify_is_phase4(self):
        content = (
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Done.\n"
            "## Verify Report\n- Verified.\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase4-sediment")

    def test_all_filled_is_done(self):
        content = (
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Done.\n"
            "## Verify Report\n- Verified.\n"
            "## Sediment Notes\n- Lessons.\n"
        )
        self.assertEqual(self._phase_of(content), "done")


class DeterminePhaseRegression(unittest.TestCase):
    """Regression tests for the bugs the fix branch addresses."""

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def _phase_of(self, content: str) -> str:
        p = make_progress_md(content)
        try:
            return self.ups.determine_phase(p)
        finally:
            p.unlink()

    def test_autosave_only_sediment_does_not_jump_to_done(self):
        """Original bug: Sediment Notes filled by autosave (Plan empty) returned `done`.
        After fix: defense-in-depth filter + sequential AND-chain → phase1-plan."""
        content = (
            "## Plan\n<!-- TEMPLATE -->\n"
            "## Execute Log\n<!-- TEMPLATE -->\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n"
            "- [2026-05-05 01:11] distill queued (trigger=stop)\n"
            "- [2026-05-05 01:18] distill queued (trigger=stop)\n"
        )
        self.assertEqual(self._phase_of(content), "phase1-plan")

    def test_autosave_with_full_trigger_note_does_not_jump_to_done(self):
        """Codex P1 finding: regex must consume the FULL breadcrumb line.
        Variant with `(trigger=heartbeat) — after 70 tool calls` must still
        be filtered (otherwise the trailing text would count as content)."""
        content = (
            "## Plan\n<!-- TEMPLATE -->\n"
            "## Execute Log\n<!-- TEMPLATE -->\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n"
            "- [2026-05-05 01:42] distill queued (trigger=heartbeat) — after 70 tool calls\n"
        )
        self.assertEqual(self._phase_of(content), "phase1-plan")

    def test_migrated_task_with_old_breadcrumbs_in_sediment_does_not_skip_phase4(self):
        """Migrated old task: Plan + Execute + Verify all human-filled, Sediment
        only has old autosave breadcrumbs (no real Phase 4 content). Must still
        report phase4-sediment (i.e. NOT prematurely advance to done)."""
        content = (
            "## Plan\n- Plan content.\n"
            "## Execute Log\n- Implementation log.\n"
            "## Verify Report\n- Tests passed.\n"
            "## Sediment Notes\n"
            "- [2026-05-05 01:11] distill queued (trigger=stop)\n"
            "- [2026-05-05 01:42] distill queued (trigger=heartbeat) — after 70 tool calls\n"
        )
        self.assertEqual(
            self._phase_of(content),
            "phase4-sediment",
            "old autosave breadcrumbs must NOT cause migrated tasks to skip Phase 4",
        )


if __name__ == "__main__":
    unittest.main(verbosity=2)
