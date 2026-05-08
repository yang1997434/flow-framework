"""T4 R4.2 — dispatch_template.build_reviewer_prompt tests.

Covers the 18-class blindspot framework mount semantics:

- Reviewer prompt mounts the framework BY REFERENCE (file path) plus a
  short inline summary so the reviewer can use class letters for
  self-check without flooding the prompt.
- The summary lists ALL classes A through T (20 letters total).
- The redaction rule is documented in the prompt: reviewer findings to
  the implementer must NOT include class letters (those would make the
  implementer cargo-cult the categorisation rather than fix the issue;
  flow_orchestrator.redact_blindspot_index enforces this on the path
  back).
- Each summary line is ≤ 80 chars (so the mount stays compact).
- The referenced file actually exists and is non-empty (sanity check
  the reference is not dangling).

PRD reference: v0.8.2 P0 core T4 §R4.2.
"""
from __future__ import annotations

import string
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dispatch_template import (  # noqa: E402  type: ignore
    BLINDSPOT_18_CLASS_SUMMARY_REF,
    build_reviewer_prompt,
)


# A through T — 20 letters total.
EXPECTED_CLASS_LETTERS = list(string.ascii_uppercase[:20])


class ReviewerPromptMountsSummary(unittest.TestCase):
    def test_reviewer_prompt_mounts_18_class_summary(self) -> None:
        """Output contains the literal anchor and references the file path."""
        out = build_reviewer_prompt(
            task_brief="review T7 dispatch",
            impl_output="diff: scripts/x.py",
        )
        self.assertIn("18-class blindspot", out)
        self.assertIn(BLINDSPOT_18_CLASS_SUMMARY_REF, out)
        # Sanity: the path is the documented constant.
        self.assertEqual(
            BLINDSPOT_18_CLASS_SUMMARY_REF,
            ".flow/pitfalls/claude-review-blindspots.md",
        )


class ReviewerPromptListsAllClasses(unittest.TestCase):
    def test_reviewer_prompt_lists_classes_A_through_T(self) -> None:
        """A — through T — lines all present (20 letters total)."""
        out = build_reviewer_prompt(
            task_brief="review T7",
            impl_output="diff",
        )
        for letter in EXPECTED_CLASS_LETTERS:
            anchor = f"{letter} — "
            self.assertIn(
                anchor,
                out,
                f"class letter line {letter!r} missing from reviewer prompt",
            )


class ReviewerPromptRedactionRule(unittest.TestCase):
    def test_reviewer_prompt_includes_redaction_rule(self) -> None:
        """Reviewer is told NOT to include class letters in findings."""
        out = build_reviewer_prompt(
            task_brief="review T7",
            impl_output="diff",
        )
        # Accept either of the documented phrasings.
        ok = (
            "Do NOT include class letters" in out
            or "do not include class letters" in out.lower()
            or "not to transfer" in out.lower()
        )
        self.assertTrue(
            ok,
            f"reviewer prompt must instruct against transferring class "
            f"letters into impl-facing feedback; got:\n{out}",
        )


class ReviewerPromptSummaryLineWidth(unittest.TestCase):
    def test_reviewer_prompt_summary_lines_under_80_chars(self) -> None:
        """Each A-T summary line ≤ 80 chars — keeps the mount compact."""
        out = build_reviewer_prompt(
            task_brief="review T7",
            impl_output="diff",
        )
        for line in out.splitlines():
            stripped = line.strip()
            # Only check the class summary lines (start with a letter
            # in A..T followed by " — ").
            if (
                len(stripped) >= 2
                and stripped[0] in EXPECTED_CLASS_LETTERS
                and stripped[1:4] == " — "
            ):
                self.assertLessEqual(
                    len(line),
                    80,
                    f"summary line exceeds 80 chars ({len(line)}): {line!r}",
                )


class BlindspotFileExists(unittest.TestCase):
    def test_blindspot_file_exists_and_is_readable(self) -> None:
        """Reference is not dangling: file exists, non-empty, mentions
        the framework signature."""
        path = REPO_ROOT / BLINDSPOT_18_CLASS_SUMMARY_REF
        self.assertTrue(
            path.exists(),
            f"blindspot framework file missing at {path}",
        )
        text = path.read_text(encoding="utf-8")
        self.assertGreater(
            len(text), 1000,
            f"blindspot file unexpectedly short ({len(text)} chars)",
        )
        # Sanity: at least the K-class entry (the trap referenced by
        # the implementer prompt) is present.
        self.assertIn("K.", text)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
