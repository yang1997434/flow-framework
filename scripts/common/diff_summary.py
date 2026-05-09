"""v0.8.5 — structural diff map (no code lines) for Round 2+ feedback
enrichment.

Purpose (PRD §R4): produce a compact, audit-only summary of what
Round N-1 changed, to attach to the Round N implementer prompt as
auxiliary context. The summary intentionally contains NO code content
— only structural metadata:

* ``git diff --stat`` output (file paths + per-file +N/-M counts)
* Top-level ``@@ ... @@`` hunk headers (function / class / method
  symbol identifiers) — typically populated for Python / JS / TS via
  git's default textconv; configuration files often produce no symbol.

It does NOT include:

* Added or removed code lines (``+`` / ``-`` prefixes)
* Hunk context lines (lines surrounding the change)
* Any code content beyond the symbol identifiers in hunk headers

Truncation rules (PRD §R4):

* Per-file hunk header cap: at most ``MAX_HUNK_HEADERS_PER_FILE`` (10)
  per file; excess marked ``[... +N more hunks in this file]``.
* Global line cap: at most ``MAX_TOTAL_LINES`` (200); excess marked
  ``[... truncated, N more files]``.
* Round 1 callers should NOT call this — there is no prev round.

Redaction (lightweight, only for accidental metadata leakage):

* Long hex / base64 tokens (>= 32 chars) in paths or hunk headers
* UUIDs (8-4-4-4-12 hex pattern)
* Email addresses
* URL-style ``?secret=...`` query strings

The redactor does NOT scrub general source content — by design, no
source content reaches this module.

Codex round-2 framing (see ``research/codex-consult-r2-output.md``):
this is a structural map, not a second-source-context delivery
mechanism. The reviewer feedback remains the primary signal for what
to change; the diff map enriches *localisation* and *cross-checking*.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Optional

__all__ = [
    "build_diff_summary",
    "MAX_HUNK_HEADERS_PER_FILE",
    "MAX_TOTAL_LINES",
    "REDACTED_TOKEN",
]


# ── Constants ───────────────────────────────────────────────────────

MAX_HUNK_HEADERS_PER_FILE: int = 10
MAX_TOTAL_LINES: int = 200
REDACTED_TOKEN: str = "<REDACTED-TOKEN>"


# ── Redaction patterns ──────────────────────────────────────────────
# Order matters — most specific first. UUID before generic-long-hex
# so the substitution lands once per occurrence with the correct
# token boundary.

_UUID_RE = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
# URL secret-style query strings — match the value side of common
# secret-bearing query params. Conservative — leaves most URLs intact.
_URL_SECRET_RE = re.compile(
    r"(?i)([?&](?:token|key|secret|password|api_key|access_token|auth)"
    r"=)[^\s&#]+"
)
# Long hex / base64-ish blob: >=32 chars from the alphabet that hex,
# base64 standard, and base64url all share (alphanumeric + ``-`` + ``_``).
# We require that ALL of [a-zA-Z0-9_+\-/=] are present and at least
# one digit + one letter to reduce false positives on simple 32-char
# words.
_LONG_TOKEN_RE = re.compile(
    r"\b(?=[A-Za-z0-9+/_=\-]*[0-9])(?=[A-Za-z0-9+/_=\-]*[A-Za-z])"
    r"[A-Za-z0-9+/_=\-]{32,}\b"
)


def _redact_line(line: str) -> str:
    """Apply the four redaction passes in order. Pure function."""
    line = _URL_SECRET_RE.sub(r"\1" + REDACTED_TOKEN, line)
    line = _EMAIL_RE.sub(REDACTED_TOKEN, line)
    line = _UUID_RE.sub(REDACTED_TOKEN, line)
    line = _LONG_TOKEN_RE.sub(REDACTED_TOKEN, line)
    return line


# ── Git driver ──────────────────────────────────────────────────────

def _git(cwd: Path, *args: str) -> str:
    """Subprocess wrapper. Returns stdout text. Raises on non-zero
    return code (caller decides; ``build_diff_summary`` catches
    ``CalledProcessError`` and returns empty)."""
    proc = subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )
    return proc.stdout


# ── Public API ──────────────────────────────────────────────────────

def build_diff_summary(
    *,
    worktree_path: Path,
    base_ref: str,
) -> str:
    """Build a structural diff map for ``base_ref..HEAD`` in
    ``worktree_path`` PLUS any uncommitted state on disk.

    v0.8.5 codex-review I3: Phase 2 implementer rounds frequently fail
    BEFORE committing their work. ``derive_task_facts`` already treats
    staged + unstaged + untracked as authoritative for manifest-verify
    purposes; the diff map for feedback enrichment must be aligned —
    otherwise Round 2 prompt loses the picture of what Round N-1
    actually did.

    Four states merged into a single summary:
        1. committed   — ``base..HEAD``
        2. staged      — ``--cached HEAD``
        3. unstaged    — ``HEAD`` (worktree vs HEAD, sans --cached)
        4. untracked   — ``git status --porcelain '??' lines (each
                          listed with a ``(new file)`` marker; line
                          count from ``wc -l``)

    Returns the rendered summary as a string (line-oriented). Empty
    string when there is no diff or when git fails.

    The output structure:

    ::

        This is a structural map only; no code content.

         path/file.py  | +N -M       # committed
         path/staged.js | +N -M      # staged
         path/wt.py    | +N -M       # unstaged
         path/new.py   | +N -0  (new file)  # untracked

         path/file.py:
           @@ def some_func ...
           @@ class SomeClass ...
           [... +K more hunks in this file]

         [... truncated, J more files]

    Failure modes:

    * ``worktree_path`` not a git repo → empty string.
    * ``base_ref`` not resolvable → degrade to uncommitted-only.
    * Any other ``CalledProcessError`` for one source → that source
      is skipped, others still tried (defence in depth — rather have
      a partial map than nothing).

    Caller responsibility (PRD R4):
    * Do NOT call on Round 1 — there is no prev round.
    * Wrap in fail-closed prompt builder so a None / empty return
      means the prompt section is omitted (not a blank header).
    """
    worktree_path = Path(worktree_path)

    # Each source contributes (file_stats list, hunk_map dict) tuples.
    # We concatenate stats in source order: committed → staged →
    # unstaged → untracked. Same path appearing in multiple sources
    # produces multiple stat lines (different states). Hunk maps merge
    # per-path (committed first, then later sources append).
    all_file_stats: list[tuple[str, str]] = []
    merged_hunk_map: dict[str, list[str]] = {}

    def _accumulate(stats: list[tuple[str, str]], hunks: dict[str, list[str]]) -> None:
        all_file_stats.extend(stats)
        for path, hs in hunks.items():
            merged_hunk_map.setdefault(path, []).extend(hs)

    # Source 1: committed (base..HEAD).
    cm_stats, cm_hunks = _collect_committed(worktree_path, base_ref)
    _accumulate(cm_stats, cm_hunks)

    # Source 2: staged (HEAD index vs HEAD tree). git diff --cached
    # --stat HEAD captures everything in the index but not committed.
    st_stats, st_hunks = _collect_staged(worktree_path)
    _accumulate(st_stats, st_hunks)

    # Source 3: unstaged (worktree vs HEAD, sans --cached). Already-
    # tracked files modified in the working tree.
    us_stats, us_hunks = _collect_unstaged(worktree_path)
    _accumulate(us_stats, us_hunks)

    # Source 4: untracked (git status --porcelain '??' lines). New
    # files with no git tracking yet — render each with a ``(new
    # file)`` marker; +N counts from a wc -l on the file body.
    ut_stats = _collect_untracked(worktree_path)
    all_file_stats.extend(ut_stats)
    # Untracked files have no diff hunks (no comparison baseline) so
    # no hunk_map contribution.

    if not all_file_stats and not merged_hunk_map:
        return ""

    return _render(file_stats=all_file_stats, hunk_map=merged_hunk_map)


def _collect_committed(
    worktree_path: Path, base_ref: str,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """Source 1: ``base..HEAD`` committed diff."""
    try:
        stat_raw = _git(
            worktree_path, "diff", "--stat", f"{base_ref}..HEAD",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [], {}
    if not stat_raw.strip():
        return [], {}
    try:
        diff_raw = _git(
            worktree_path, "diff", "-U0", "--no-color",
            f"{base_ref}..HEAD",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        diff_raw = ""
    return _parse_stat(stat_raw), _parse_hunk_headers(diff_raw)


def _collect_staged(
    worktree_path: Path,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """Source 2: index vs HEAD (what's staged-but-not-committed)."""
    try:
        stat_raw = _git(
            worktree_path, "diff", "--cached", "--stat", "HEAD",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [], {}
    if not stat_raw.strip():
        return [], {}
    try:
        diff_raw = _git(
            worktree_path, "diff", "--cached", "-U0", "--no-color", "HEAD",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        diff_raw = ""
    return _parse_stat(stat_raw), _parse_hunk_headers(diff_raw)


def _collect_unstaged(
    worktree_path: Path,
) -> tuple[list[tuple[str, str]], dict[str, list[str]]]:
    """Source 3: worktree vs INDEX (modifications not yet staged).

    v0.8.5 codex-review R2-I3A: previous implementation ran
    ``git diff [--stat] HEAD`` which is "working-tree vs HEAD" — that
    INCLUDES staged changes. With staged content present, the same
    file would appear in both ``_collect_staged`` (correctly) AND
    ``_collect_unstaged`` (wrongly), double-counting the staged
    portion in the merged stat block.

    Fix: bare ``git diff [--stat]`` without a ref is the canonical
    "working tree vs index" incantation per ``git help diff`` —
    returns ONLY changes not yet staged. Identical content in working
    tree and index → empty diff → file correctly absent from this
    source.
    """
    try:
        # Bare `git diff` (no ref) = working-vs-index = unstaged-only.
        stat_raw = _git(worktree_path, "diff", "--stat")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return [], {}
    if not stat_raw.strip():
        return [], {}
    try:
        diff_raw = _git(
            worktree_path, "diff", "-U0", "--no-color",
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        diff_raw = ""
    return _parse_stat(stat_raw), _parse_hunk_headers(diff_raw)


def _collect_untracked(worktree_path: Path) -> list[tuple[str, str]]:
    """Source 4: untracked files (``git status --porcelain '??'``).

    Each untracked file rendered as a stat-style line with a
    ``(new file)`` suffix. Line count comes from a quick wc-style
    read of the file (capped to avoid pathological huge files).
    """
    try:
        out = _git(worktree_path, "status", "--porcelain")
    except (subprocess.CalledProcessError, FileNotFoundError):
        return []
    stats: list[tuple[str, str]] = []
    for line in out.splitlines():
        # Porcelain v1 format: "XY path" where XY is the status.
        # '??' = untracked. We accept exactly the leading '?? '.
        if not line.startswith("?? "):
            continue
        path = line[3:].strip()
        # Strip leading/trailing quotes that git adds for paths with
        # special chars. Leave the path intact otherwise.
        if path.startswith('"') and path.endswith('"'):
            path = path[1:-1]
        # Compute +N as line count of the file body. Capped at 1000
        # to avoid surprises on accidental huge files.
        full_path = worktree_path / path
        added = 0
        try:
            with open(full_path, "rb") as f:
                added = sum(1 for _ in f)
        except (OSError, ValueError):
            added = 0
        if added > 1000:
            added = 1000
        rest = f"{added} +0  (new file)"
        stats.append((path, rest))
    return stats


# ── Parsing ─────────────────────────────────────────────────────────

# Stat line format (fixed-width columns):
#   ``  path/to/file.py | 12 +-``
# The path is left-aligned, separated from counts by ` | ``.
_STAT_LINE_RE = re.compile(r"^\s*(?P<path>\S(?:.*\S)?)\s+\|\s+(?P<rest>.+)$")


def _parse_stat(stat_raw: str) -> list[tuple[str, str]]:
    """Return ordered ``[(path, raw_count_str)]`` from ``git diff
    --stat`` output. Trailing summary line ('N files changed, ...')
    is filtered out."""
    out: list[tuple[str, str]] = []
    for line in stat_raw.splitlines():
        if not line.strip():
            continue
        if "|" not in line:
            # Summary line ('3 files changed, ...') — skip.
            continue
        m = _STAT_LINE_RE.match(line)
        if not m:
            continue
        out.append((m.group("path").strip(), m.group("rest").strip()))
    return out


# Hunk header marker: ``diff --git a/<path> b/<path>``
_DIFF_GIT_RE = re.compile(r"^diff --git a/(?P<path>.+?) b/(?P<bpath>.+)$")
# Hunk header line: ``@@ -L,N +L,N @@ symbol info``
_HUNK_RE = re.compile(r"^@@ .* @@(?P<tail>.*)$")


def _parse_hunk_headers(diff_raw: str) -> dict[str, list[str]]:
    """Return ``{path: [hunk_header_full_line, ...]}`` from a unified
    diff. Path is the ``b/<path>`` form (post-rename). Hunk header
    full line is preserved (the ``@@ ... @@ symbol`` form, without
    code lines after it)."""
    result: dict[str, list[str]] = {}
    cur: Optional[str] = None
    for line in diff_raw.splitlines():
        m = _DIFF_GIT_RE.match(line)
        if m:
            cur = m.group("bpath")
            result.setdefault(cur, [])
            continue
        if cur is None:
            continue
        if line.startswith("@@ ") and " @@" in line:
            # Keep only the ``@@ ... @@ symbol`` part — strip any
            # trailing context-line drift defensively (git -U0 should
            # not emit any but be safe).
            # Find the second ``@@`` and keep through the symbol tail.
            # We do NOT keep added/removed/context lines.
            result[cur].append(line.rstrip())
    return result


# ── Rendering ───────────────────────────────────────────────────────

def _render(
    *,
    file_stats: list[tuple[str, str]],
    hunk_map: dict[str, list[str]],
) -> str:
    """Render the final summary string under per-file + global caps."""
    out_lines: list[str] = []

    # Line 1 — explicit framing per codex R2 (anti "second source
    # context" misuse). The prompt builder also includes this on
    # its side, but a redundant line here prevents any caller that
    # forgets the framing from delivering an unlabelled blob.
    out_lines.append("This is a structural map only; no code content.")
    out_lines.append("")

    # Stat block.
    for path, rest in file_stats:
        line = f" {path} | {rest}"
        out_lines.append(_redact_line(line))

    # Per-file hunk blocks.
    truncated_files = 0
    for path, _rest in file_stats:
        if _would_exceed_total(out_lines):
            truncated_files = len(file_stats) - file_stats.index((path, _rest))
            break

        hunks = hunk_map.get(path) or []
        if not hunks:
            continue

        out_lines.append("")
        out_lines.append(f" {_redact_line(path)}:")
        shown = 0
        for h in hunks:
            if shown >= MAX_HUNK_HEADERS_PER_FILE:
                break
            if _would_exceed_total(out_lines):
                break
            out_lines.append(f"   {_redact_line(h)}")
            shown += 1
        if len(hunks) > shown:
            out_lines.append(
                f"   [... +{len(hunks) - shown} more hunks in this file]"
            )

    # Global hard cap with marker (PRD R4).
    if len(out_lines) > MAX_TOTAL_LINES or truncated_files > 0:
        # Reserve the last line for the marker.
        cap = MAX_TOTAL_LINES - 1 if len(out_lines) > MAX_TOTAL_LINES else len(out_lines)
        out_lines = out_lines[:cap]
        # Compute truncated count: prefer file-level marker when meaningful.
        if truncated_files > 0:
            out_lines.append(
                f" [... truncated, {truncated_files} more files]"
            )
        else:
            out_lines.append(" [... truncated, output exceeded line cap]")

    return "\n".join(out_lines)


def _would_exceed_total(out_lines: list[str]) -> bool:
    """Reserve at least 2 lines headroom for the truncation marker."""
    return len(out_lines) >= MAX_TOTAL_LINES - 2
