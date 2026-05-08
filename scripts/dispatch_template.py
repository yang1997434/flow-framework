"""v0.8.2 T4 — dispatch prompt template builders.

Two pure helpers used by the orchestrator's dispatch loop:

* ``build_implementer_prompt`` — composes the prompt sent to an
  implementer subagent. By default it auto-prepends the K-class
  sentinel prohibition (the "do not touch ``~/.claude/hooks/.review-passed``"
  rule). Doc-only dispatches opt out via ``is_doc_only=True`` because
  legitimate progress.md / sediment-note edits do not go through the
  pre-commit code-review hook anyway.

* ``build_reviewer_prompt`` — composes the prompt sent to the reviewer
  subagent. It mounts the 18-class blindspot framework BY REFERENCE
  (file path) plus a short inline summary (one line per class A-T) so
  the reviewer can self-check without flooding the prompt with the full
  framework body. The redaction rule is also stated inline: reviewer
  findings to the implementer must NOT include class letters (the
  caller — flow_orchestrator.redact_blindspot_index — enforces this on
  the path back).

Design rules:

* I-class: pure functions. No module-level mutable state, no I/O at
  import time. Every call composes a fresh string.
* G-class: no disk reads from inside the builders. The reviewer file
  reference is delivered as a path STRING; the reviewer agent reads
  the file itself. Tests verify the reference is not dangling.
* J-class: K-class implementer prepend + 18-class reviewer mount stay
  separate and do not overlap. The implementer must NEVER see the
  18-class framework (per redaction rule); the reviewer must always
  see the K-class history (it informs class-K self-check).
* E-class: no shell quoting concerns — these helpers return plain
  strings; the caller decides how to deliver them (subprocess args,
  HTTP body, etc.).

PRD reference: v0.8.2 P0 core T4 (R4.1 + R4.2).
"""
from __future__ import annotations

from typing import Optional

__all__ = [
    "K_CLASS_SENTINEL_PROHIBITION",
    "BLINDSPOT_18_CLASS_SUMMARY_REF",
    "build_implementer_prompt",
    "build_reviewer_prompt",
]


# ── Constants ───────────────────────────────────────────────────────

# Verbatim K-class sentinel prohibition. The phrasing is pinned by the
# T4 brief AND by tests/smoke/test_dispatch_template.py — silent edits
# that weaken the message (e.g. dropping the "2 real bugs" forensic
# anchor) will trip CI. To update this text, update the test fixture
# in lockstep AND the corresponding section in
# claude/skills/flow/flow-phase2-execute/SKILL.md.
K_CLASS_SENTINEL_PROHIBITION: str = (
    "DO NOT touch `~/.claude/hooks/.review-passed` for first-pass code commits.\n"
    "Doc-only fixes may; fixes-on-already-reviewed code may. First-pass code MUST\n"
    "go through the pre-commit review hook unmodified. Bypassing this hook hid\n"
    "2 real bugs in v0.8.1 that codex caught later (T22 round-0, estimator round-2).\n"
    "If the hook blocks your commit, report partial state — do not bypass."
)


# Repo-relative path to the full 18-class framework. Mounted by
# reference (not inlined) so the reviewer prompt stays compact and the
# framework can be promoted/edited without touching every prompt site.
BLINDSPOT_18_CLASS_SUMMARY_REF: str = ".flow/pitfalls/claude-review-blindspots.md"


# 1-line titles for each class A through T. Each line MUST be ≤ 80
# chars including any leading whitespace — see test_reviewer_prompt_
# summary_lines_under_80_chars. Letters mirror the section headers of
# .flow/pitfalls/claude-review-blindspots.md (A through T = 20 letters,
# no skips; M is "shared state cross-task pollution").
_BLINDSPOT_CLASS_LINES: tuple[str, ...] = (
    "A — Python falsy/truthy traps (.get + or, is None vs not in)",
    "B — design cross-reference semantics (enum × field cartesian product)",
    "C — architectural ordering / reachability (gate before exception swallow)",
    "D — bypass via fallback path (try/except return False; rc != 0 lying)",
    "E — shell=True + prefix-match = compound-command bypass; metachar guard",
    "F — identity check fail-open (missing hash → block, never skip)",
    "G — facts-from-disk: enumerate ALL state layers (HEAD/index/wt/untracked)",
    "H — external tool output parsing ambiguity (use -z / --json, not split)",
    "I — repeating earlier task's mistake; grep existing helpers before writing",
    "J — fix-chain paper-cuts (audit verdict / forensic / labels with happy path)",
    "K — plausible-justification trap; deviating from helper needs codex audit",
    "L — type-check vs presence-check (key in dict ≠ value is the right type)",
    "M — shared state file cross-task pollution (filter jsonl by task scope)",
    "N — disk identity vs ref identity (merge SHA, not branch ref)",
    "O — same-pid TOCTOU within-second (use µs ts for path-from-ts collisions)",
    "P — JSONL scope key must include task_id, not just run_id",
    "Q — filter + enumerate index drift (preserve original idx for audit)",
    "R — frontmatter / OSC injection (full splitlines() separator class)",
    "S — wire-up gap: helper exists but production never calls it",
    "T — codex counter-factual anchoring across review rounds",
)


# ── Implementer prompt ──────────────────────────────────────────────

def build_implementer_prompt(
    *,
    task_brief: str,
    reviewer_feedback: Optional[str] = None,
    is_first_pass: bool = True,
    is_doc_only: bool = False,
) -> str:
    """Compose an implementer dispatch prompt.

    Always prepends the K-class sentinel prohibition unless
    ``is_doc_only=True``. The doc-only opt-out exists for progress.md /
    sediment-note dispatches that legitimately may touch the
    review-passed hook (the hook blocks first-pass CODE; not docs).

    If ``reviewer_feedback`` is provided, it is appended after a clear
    visual separator. The feedback is assumed already redacted of
    18-class blindspot triggers (caller's responsibility — see
    ``scripts/flow_orchestrator.py::redact_blindspot_index``). That
    redaction step strips the trigger lines so the implementer can fix
    the specific bug rather than cargo-cult the categorisation.

    Defaults are safe-by-default: ``is_first_pass=True`` and
    ``is_doc_only=False`` → prohibition prepended. Callers must
    explicitly opt out for doc-only paths.

    Returns the assembled prompt string.
    """
    parts: list[str] = []

    # K-class prepend (the only path that skips it is explicit
    # is_doc_only). The is_first_pass flag is reserved for future
    # callers (e.g. fix-on-already-reviewed code may also legitimately
    # touch the hook); v0.8.2 T4 ships the doc_only branch as the
    # primary opt-out. Both paths are tested.
    if not is_doc_only:
        parts.append(K_CLASS_SENTINEL_PROHIBITION)
        parts.append("")  # blank line separator before task brief

    # Task brief always appears.
    parts.append(task_brief)

    # Reviewer feedback (post-redaction) — visible separator + header
    # so the implementer can locate it in a long prompt.
    if reviewer_feedback:
        parts.append("")
        parts.append("---")
        parts.append("## Reviewer feedback (from previous round)")
        parts.append("")
        parts.append(reviewer_feedback)

    return "\n".join(parts)


# ── Reviewer prompt ─────────────────────────────────────────────────

def build_reviewer_prompt(
    *,
    task_brief: str,
    impl_output: str,
    blindspot_summary_path: str = BLINDSPOT_18_CLASS_SUMMARY_REF,
) -> str:
    """Compose a reviewer dispatch prompt with 18-class blindspot mount.

    The mount is a REFERENCE to the file at
    ``blindspot_summary_path`` (the framework body lives there and is
    promoted to the vault), plus a short inline summary (one line per
    class A through T) for fast self-check.

    Reviewer is instructed to use the framework for self-check only;
    findings sent back to the implementer must NOT include class
    letters. Class labels in impl-facing feedback would let the
    implementer cargo-cult the categorisation rather than fix the
    actual bug. ``redact_blindspot_index`` (in flow_orchestrator)
    enforces this on the return path; this prompt block tells the
    reviewer the rule directly so the redactor stays a defense in
    depth, not a single point of failure.

    Returns the assembled prompt string.
    """
    summary_block = "\n".join(f"  {line}" for line in _BLINDSPOT_CLASS_LINES)

    return (
        f"## Task brief\n"
        f"\n"
        f"{task_brief}\n"
        f"\n"
        f"## Implementer output to review\n"
        f"\n"
        f"{impl_output}\n"
        f"\n"
        f"---\n"
        f"\n"
        f"## Reviewer self-check — 18-class blindspot framework\n"
        f"\n"
        f"Full framework: `{blindspot_summary_path}` (read it; the\n"
        f"trigger checklists per class are not inlined here).\n"
        f"\n"
        f"18-class blindspot self-check (full doc: "
        f"{blindspot_summary_path}):\n"
        f"\n"
        f"{summary_block}\n"
        f"\n"
        f"Use these for review self-check. Do NOT include class letters\n"
        f"in your findings to the implementer — only specific concrete\n"
        f"issues (file refs, line refs, exact behaviours). Class labels\n"
        f"in impl-facing feedback would let the implementer cargo-cult\n"
        f"the categorisation rather than fix the actual bug.\n"
    )
