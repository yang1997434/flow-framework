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


class FrontmatterPhaseParse(unittest.TestCase):
    """`parse_frontmatter_phase` extracts and maps the YAML `phase:` field."""

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def test_no_frontmatter_returns_none(self):
        self.assertIsNone(self.ups.parse_frontmatter_phase("## Plan\n- foo\n"))

    def test_frontmatter_without_phase_field_returns_none(self):
        text = "---\nslug: foo\nstatus: active\n---\n## Plan\n"
        self.assertIsNone(self.ups.parse_frontmatter_phase(text))

    def test_unknown_phase_value_returns_none(self):
        text = "---\nphase: bogus\n---\n"
        self.assertIsNone(self.ups.parse_frontmatter_phase(text))

    def test_each_known_phase_maps_correctly(self):
        cases = {
            "triage":    "phase1-plan",
            "research":  "phase1-plan",
            "implement": "phase2-execute",
            "check":     "phase3-finish",
            "verify":    "phase3-finish",
            "sediment":  "phase4-sediment",
        }
        for fm_value, expected in cases.items():
            with self.subTest(fm_value=fm_value):
                text = f"---\nphase: {fm_value}\n---\n"
                self.assertEqual(self.ups.parse_frontmatter_phase(text), expected)

    def test_phase_with_inline_comment_still_parses(self):
        text = "---\nphase: triage    # triage | research | implement | check | verify | sediment\n---\n"
        self.assertEqual(self.ups.parse_frontmatter_phase(text), "phase1-plan")


class MinPhase(unittest.TestCase):
    """`min_phase` returns whichever canonical phase comes earlier."""

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def test_basic_ordering(self):
        self.assertEqual(self.ups.min_phase("phase1-plan", "phase3-finish"), "phase1-plan")
        self.assertEqual(self.ups.min_phase("phase3-finish", "phase1-plan"), "phase1-plan")
        self.assertEqual(self.ups.min_phase("phase2-execute", "phase4-sediment"), "phase2-execute")

    def test_equal_phases(self):
        self.assertEqual(self.ups.min_phase("phase2-execute", "phase2-execute"), "phase2-execute")

    def test_done_is_latest(self):
        self.assertEqual(self.ups.min_phase("done", "phase4-sediment"), "phase4-sediment")
        self.assertEqual(self.ups.min_phase("done", "phase1-plan"), "phase1-plan")

    def test_unknown_phase_falls_safe(self):
        # Unknown phase string — must not raise, returns first arg.
        self.assertEqual(self.ups.min_phase("phase1-plan", "bogus-phase"), "phase1-plan")


class FrontmatterPhaseCap(unittest.TestCase):
    """Frontmatter `phase:` declaration caps section-based advancement.

    Regression test for: brainstorm milestones logged to Execute Log during
    phase 1 caused determine_phase to return phase3-finish even though the
    user was still in brainstorm (frontmatter said `phase: triage`).
    """

    @classmethod
    def setUpClass(cls):
        cls.ups = load_ups()

    def _phase_of(self, content: str) -> str:
        p = make_progress_md(content)
        try:
            return self.ups.determine_phase(p)
        finally:
            p.unlink()

    def test_brainstorm_milestones_in_execute_log_dont_jump_past_phase1(self):
        """The actual bug user hit: frontmatter says triage (still brainstorming),
        Plan + Execute Log both filled with brainstorm artifacts. Section-only
        heuristic returns phase3-finish; frontmatter cap pulls back to phase1-plan."""
        content = (
            "---\n"
            "slug: geo-framework\n"
            "status: active\n"
            "phase: triage    # triage | research | implement | check | verify | sediment\n"
            "---\n"
            "## Plan\n"
            "Phase 2 拆为 6 个 sub-task...\n"
            "## Execute Log\n"
            "| time | agent | scope | outcome |\n"
            "| 2026-05-05 02:50 | PAUSE | Phase 1 brainstorm | sub-agent dispatched |\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(
            self._phase_of(content),
            "phase1-plan",
            "frontmatter `phase: triage` must cap section advancement at phase1-plan",
        )

    def test_research_phase_caps_at_phase1(self):
        """`phase: research` is still phase 1 territory."""
        content = (
            "---\nphase: research\n---\n"
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Brainstorm log.\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase1-plan")

    def test_implement_caps_at_phase2_when_execute_log_has_content(self):
        """User advanced frontmatter to `implement` but Execute Log has work
        logged: cap at phase2-execute (not phase3-finish)."""
        content = (
            "---\nphase: implement\n---\n"
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Implementation step done.\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase2-execute")

    def test_frontmatter_does_not_promote_past_section_reality(self):
        """Inverse direction: stale frontmatter `implement` but Plan is empty.
        Sections (phase1-plan) must win — frontmatter is a CAP not a floor."""
        content = (
            "---\nphase: implement\n---\n"
            "## Plan\n<!-- TEMPLATE -->\n"
            "## Execute Log\n<!-- TEMPLATE -->\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase1-plan")

    def test_no_frontmatter_falls_back_to_section_logic(self):
        """No frontmatter at all → pure section-based heuristic (legacy behavior)."""
        content = (
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Did the thing.\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase3-finish")

    def test_frontmatter_without_phase_falls_back_to_section_logic(self):
        """Frontmatter present but no `phase:` field → section logic only."""
        content = (
            "---\nslug: foo\nstatus: active\n---\n"
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Did the thing.\n"
            "## Verify Report\n<!-- TEMPLATE -->\n"
            "## Sediment Notes\n<!-- TEMPLATE -->\n"
        )
        self.assertEqual(self._phase_of(content), "phase3-finish")

    def test_completed_task_with_sediment_frontmatter_reaches_done(self):
        """Codex P2 finding: PHASE_FRONTMATTER_MAP has no `done` value, so
        a fully completed task with `phase: sediment` would forever report
        phase4-sediment instead of done. Section heuristic must win when
        all 4 sections are filled (= done state)."""
        content = (
            "---\nphase: sediment\n---\n"
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Implementation log.\n"
            "## Verify Report\n- Tests pass.\n"
            "## Sediment Notes\n- ADR captured.\n"
        )
        self.assertEqual(
            self._phase_of(content),
            "done",
            "fully filled sections must reach done even with frontmatter cap",
        )

    def test_completed_task_with_implement_frontmatter_still_reaches_done(self):
        """Same finding, additional case: user forgot to advance frontmatter
        past `implement` but actually completed all phases. Sections (done)
        must still win — the artifact reality is what counts when truly done."""
        content = (
            "---\nphase: implement\n---\n"
            "## Plan\n- Plan.\n"
            "## Execute Log\n- Done.\n"
            "## Verify Report\n- Verified.\n"
            "## Sediment Notes\n- Lessons.\n"
        )
        self.assertEqual(self._phase_of(content), "done")


if __name__ == "__main__":
    unittest.main(verbosity=2)
