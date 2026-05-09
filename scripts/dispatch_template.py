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
    "PREV_ROUND_DIFF_MAP_FRAMING",
    "build_implementer_prompt",
    "build_reviewer_prompt",
]


# v0.8.5 — verbatim framing string for the Round 2+ structural diff
# map enrichment section. Pinned by tests so silent rewrites that
# weaken the "structural map only / use reviewer feedback as primary"
# message trip CI. Codex round-2 framing (see
# ``research/codex-consult-r2-output.md``): the diff map enriches
# *localisation* and *cross-checking*; it is NOT a second source of
# implementation context. The framing line MUST stay paired with the
# section header in ``build_implementer_prompt`` below.
PREV_ROUND_DIFF_MAP_FRAMING: str = (
    "This is a structural map only; no code content. "
    "Use reviewer feedback as the primary signal for what to change."
)


# ── Constants ───────────────────────────────────────────────────────

# Verbatim K-class sentinel prohibition. The phrasing is pinned by the
# T4 brief AND by tests/smoke/test_dispatch_template.py — silent edits
# that weaken the message (e.g. dropping the "2 real bugs" forensic
# anchor) will trip CI. To update this text, update the test fixture
# in lockstep AND the corresponding section in
# claude/skills/flow/flow-phase2-execute/SKILL.md.
K_CLASS_SENTINEL_PROHIBITION: str = (
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
    prev_round_diff_summary: Optional[str] = None,
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

    v0.8.5 (PRD §R4): ``prev_round_diff_summary`` (Round 2+ only) is a
    structural diff map of the previous round's changes — file list +
    per-file +/- counts + top-level ``@@`` hunk headers. NO code
    lines. When supplied non-empty, an enrichment section is appended
    AFTER the reviewer feedback with explicit framing
    (``PREV_ROUND_DIFF_MAP_FRAMING``) so the implementer treats it as
    auxiliary localisation, not as a second source of context. Round 1
    callers MUST pass None / empty (there is no prev round). Empty
    string is treated as None — the section is omitted.

    pitfall:dispatch-shim-silent-kw-drop — every new kwarg uses an
    explicit named parameter (NOT ``**kw``) so misspelt callers raise
    ``TypeError`` rather than silently drop the field.

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

    # v0.8.5 R4 — Round 2+ structural diff map enrichment. Empty
    # string treated as None (Round 1 short-circuit + helper-returns-
    # empty path). The framing line + explicit "structural map only"
    # label sit BEFORE the body so the implementer reads the disclaimer
    # before any path / hunk header.
    if prev_round_diff_summary:
        parts.append("")
        parts.append("---")
        parts.append("## Round N-1 structural diff map (no code lines)")
        parts.append("")
        parts.append(PREV_ROUND_DIFF_MAP_FRAMING)
        parts.append("")
        parts.append(prev_round_diff_summary)

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
