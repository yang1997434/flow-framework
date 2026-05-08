"""T4 R4.1 — dispatch_template.build_implementer_prompt tests.

Covers the K-class sentinel auto-prepend semantics:

- First-pass code dispatches MUST get the K-class prohibition prepended
  verbatim.
- Doc-only dispatches MUST NOT get it prepended (progress.md updates,
  sediment notes, etc. — these legitimately may touch the review-passed
  hook because they aren't first-pass code).
- Reviewer feedback (already redacted of 18-class triggers by
  flow_orchestrator.redact_blindspot_index) is appended after a clear
  visual separator so the implementer can locate it.
- Defaults are safe-by-default: is_first_pass=True, is_doc_only=False
  → prohibition prepended. The "doc-only skip" path is opt-in.
- The verbatim K-class prohibition string is pinned (string equality)
  to defend against silent rewrites that weaken the message.

PRD reference: v0.8.2 P0 core T4 §R4.1.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from dispatch_template import (  # noqa: E402  type: ignore
    K_CLASS_SENTINEL_PROHIBITION,
    build_implementer_prompt,
)


# Verbatim text from the brief — pinned so a silent edit that softens
# the message (e.g. dropping the "2 real bugs" forensic anchor) trips
# this test in CI.
EXPECTED_K_CLASS_TEXT = (
    "**K-class red line — code review marker integrity (v0.8.3 P0.0+)**\n"
    "\n"
    "Subagents and main session must NEVER:\n"
    "1. Wrap, hide, or indirect-invoke `git commit` in any way that defeats the hook\n"
    "   (compound `&&`/`;`/`||`, subshells `(…)`, background `&`, pipelines `|`,\n"
    "   command/process substitutions `$(…)`/`<(…)`, `eval`, `bash -c`, `python -c`,\n"
    "   `command/env/nice/...` wrappers, command aliases like `git ci`).\n"
    "2. Run a non-plain `git commit` form: `-a/--all`, `--include`, `--only`,\n"
    "   `-p/--patch`, pathspec, `-c <key>=<val>`, `-C <path>`, `--git-dir=...`,\n"
    "   `--work-tree=...`, prefix env assignments (`PATH=. git commit`, etc.).\n"
    "3. Manipulate marker / hook / vendor files: write, append, copy, chmod, touch,\n"
    "   rename, atomic-replace `~/.claude/hooks/.review-passed.json`,\n"
    "   `~/.claude/hooks/_vendor/*`, `~/.claude/hooks/pre-commit-review.{sh,py}`,\n"
    "   `~/.claude/hooks/_marker_writer.py`.\n"
    "4. Use `--no-verify`, `core.hooksPath` override, `-c alias.*=`, or any other\n"
    "   git-config-based hook bypass.\n"
    "\n"
    "Approved flow: reviewer agent runs → marker writer helper writes\n"
    "~/.claude/hooks/.review-passed.json → main session runs PLAIN `git commit -m\n"
    "\"...\"` (or `-F`/`--amend`) in target repo CWD → hook validates and consumes\n"
    "marker (single-use).\n"
    "\n"
    "Forensic anchor: 2 real bugs in v0.8.1 hidden by hook bypass (T22 round-0,\n"
    "estimator round-2); 1 real K-class bypass via `touch && git commit` in v0.8.2\n"
    "T6.3. Bypassing this hook is a process violation even when accidental — report\n"
    "partial state instead.\n"
    "\n"
    "Exception: user-authorized hook/vendor maintenance (e.g. v0.8.3 P0.0) lifts\n"
    "clause 3 for that scope only, with user attestation in PRD."
)


class KClassProhibitionTextStable(unittest.TestCase):
    def test_K_class_sentinel_prohibition_text_stable(self) -> None:
        """Verbatim text equality — guards against silent rewrites."""
        self.assertEqual(K_CLASS_SENTINEL_PROHIBITION, EXPECTED_K_CLASS_TEXT)


class ImplementerPromptFirstPassPrepend(unittest.TestCase):
    def test_implementer_prompt_prepends_k_class_prohibition_first_pass(
        self,
    ) -> None:
        """First-pass code dispatch → K-class prohibition prepended verbatim.

        startswith() check: the prohibition is the FIRST thing the
        implementer sees, ahead of the task brief.
        """
        out = build_implementer_prompt(
            task_brief="implement gate 1 baseline",
            is_first_pass=True,
            is_doc_only=False,
        )
        self.assertTrue(
            out.startswith(K_CLASS_SENTINEL_PROHIBITION),
            f"output should start with K-class prohibition; got first 200 "
            f"chars: {out[:200]!r}",
        )
        # And the task brief is also present after.
        self.assertIn("implement gate 1 baseline", out)


class ImplementerPromptDocOnlySkip(unittest.TestCase):
    def test_implementer_prompt_doc_only_skips_prohibition(self) -> None:
        """Doc-only dispatch → prohibition NOT prepended.

        Doc-only fixes (progress.md, sediment notes) may legitimately
        touch the review-passed hook. is_doc_only=True is the explicit
        opt-out for this path.
        """
        out = build_implementer_prompt(
            task_brief="update progress.md execute log",
            is_first_pass=False,
            is_doc_only=True,
        )
        self.assertNotIn(K_CLASS_SENTINEL_PROHIBITION, out)
        # But the task brief still flows through.
        self.assertIn("update progress.md execute log", out)


class ImplementerPromptAppendsFeedback(unittest.TestCase):
    def test_implementer_prompt_appends_reviewer_feedback_with_separator(
        self,
    ) -> None:
        """Reviewer feedback appended after K-class prohibition with a
        visible separator so the implementer can locate it.

        Brief calls for "---" line OR "## Reviewer feedback" header.
        """
        out = build_implementer_prompt(
            task_brief="implement X",
            reviewer_feedback="Fix tests",
            is_first_pass=True,
            is_doc_only=False,
        )
        # Both anchors present.
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, out)
        self.assertIn("Fix tests", out)
        # K-class prohibition comes BEFORE the reviewer feedback.
        proh_idx = out.index(K_CLASS_SENTINEL_PROHIBITION)
        feedback_idx = out.index("Fix tests")
        self.assertLess(
            proh_idx,
            feedback_idx,
            "K-class prohibition must precede reviewer feedback",
        )
        # Visible separator: either an "---" line OR a header containing
        # "Reviewer feedback" must appear between the prohibition end
        # and the feedback start.
        between = out[proh_idx + len(K_CLASS_SENTINEL_PROHIBITION):feedback_idx]
        has_hr = "\n---\n" in between or between.lstrip().startswith("---")
        has_header = "Reviewer feedback" in between or "## Reviewer" in between
        self.assertTrue(
            has_hr or has_header,
            f"need a visible separator (--- or '## Reviewer feedback') "
            f"between prohibition and feedback; got: {between!r}",
        )


class ImplementerPromptDefaults(unittest.TestCase):
    def test_implementer_prompt_first_pass_default(self) -> None:
        """Defaults are safe-by-default: is_first_pass=True,
        is_doc_only=False → prohibition prepended.

        This protects against caller forgetting the kwargs and
        silently degrading to the doc-only path.
        """
        # Only task_brief specified — all other args at default.
        out = build_implementer_prompt(task_brief="x")
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, out)
        self.assertTrue(out.startswith(K_CLASS_SENTINEL_PROHIBITION))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
