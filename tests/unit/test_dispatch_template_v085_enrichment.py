"""v0.8.5 — dispatch_template ``build_implementer_prompt`` enrichment.

Tests the new ``prev_round_diff_summary`` parameter:

- Default (None) → output identical to v0.8.4 (no enrichment section)
- Round 1 caller passes None → no section emitted
- Round 2+ caller passes structural map text → section emitted with
  explicit "structural map only; no code content" framing AND a
  "use reviewer feedback as primary signal" note
- The diff map text appears AFTER the K-class prepend + task brief +
  reviewer feedback (so the implementer reads in the right order)
- Empty string is treated as None (no section)
- The framing label is verbatim-pinned (silent rewrites trip CI)

PRD: ``.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md``
§R4 + AC3.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dispatch_template import (  # noqa: E402  type: ignore
    build_implementer_prompt,
    PREV_ROUND_DIFF_MAP_FRAMING,
)


class DefaultIsBackwardCompatible(unittest.TestCase):
    def test_no_enrichment_param_matches_v084_behaviour(self) -> None:
        a = build_implementer_prompt(task_brief="do thing")
        b = build_implementer_prompt(
            task_brief="do thing", prev_round_diff_summary=None,
        )
        self.assertEqual(a, b)

    def test_empty_string_treated_as_none(self) -> None:
        a = build_implementer_prompt(task_brief="x")
        b = build_implementer_prompt(
            task_brief="x", prev_round_diff_summary="",
        )
        self.assertEqual(a, b)


class EnrichmentSectionEmitted(unittest.TestCase):
    def test_summary_present_emits_section(self) -> None:
        diff_map = (
            "This is a structural map only; no code content.\n\n"
            " src/parser.py | 12 +-\n\n"
            " src/parser.py:\n"
            "   @@ def normalize_task_name ...\n"
        )
        out = build_implementer_prompt(
            task_brief="implement validation",
            reviewer_feedback="Validation still accepts empty task names.",
            prev_round_diff_summary=diff_map,
        )
        # Section header + framing line both present.
        self.assertIn("Round N-1 structural diff map", out)
        self.assertIn(PREV_ROUND_DIFF_MAP_FRAMING, out)
        # The diff map body itself is included.
        self.assertIn("src/parser.py | 12 +-", out)
        self.assertIn("@@ def normalize_task_name", out)


class FramingTextStable(unittest.TestCase):
    def test_framing_pinned_verbatim(self) -> None:
        # PRD R4: "This is a structural map only; no code content. Use
        # reviewer feedback as the primary signal for what to change."
        expected = (
            "This is a structural map only; no code content. "
            "Use reviewer feedback as the primary signal for what to change."
        )
        self.assertEqual(PREV_ROUND_DIFF_MAP_FRAMING, expected)


class OrderingOfSections(unittest.TestCase):
    def test_diff_map_appears_after_brief_and_feedback(self) -> None:
        diff_map = " src/x.py | 1 +"
        out = build_implementer_prompt(
            task_brief="brief here",
            reviewer_feedback="feedback here",
            prev_round_diff_summary=diff_map,
        )
        # Locate section indexes.
        i_brief = out.find("brief here")
        i_feedback = out.find("feedback here")
        i_map_section = out.find("Round N-1 structural diff map")
        self.assertGreaterEqual(i_brief, 0)
        self.assertGreaterEqual(i_feedback, 0)
        self.assertGreaterEqual(i_map_section, 0)
        # Ordering: brief < feedback < diff map.
        self.assertLess(i_brief, i_feedback)
        self.assertLess(i_feedback, i_map_section)


class FailClosedKwargAssertion(unittest.TestCase):
    """pitfall:dispatch-shim-silent-kw-drop — any new kwarg on a
    prompt builder MUST appear in the function signature so a
    misspelt call raises TypeError, not silently drops."""

    def test_unknown_kwarg_raises_typeerror(self) -> None:
        # Misspelt name should explode.
        with self.assertRaises(TypeError):
            build_implementer_prompt(  # type: ignore[call-arg]
                task_brief="x",
                prev_round_diff_summarie=" foo",  # typo on purpose
            )


if __name__ == "__main__":
    unittest.main()
