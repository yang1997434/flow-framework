"""Glob normalization and overlap detection for v0.7 wave planner.

Conservative semantics — when in doubt, return overlap=True (forces serial).
This is the inverse of build-system optimism: we'd rather serialize an
incorrectly-flagged independent pair than parallelize a hidden-dep pair.
"""
from __future__ import annotations

import fnmatch
import re
from pathlib import PurePosixPath


class GlobError(ValueError):
    """Raised when a glob pattern violates v0.7 hygiene rules."""


_BROAD_PATTERNS = frozenset({"*", "**", "**/*"})


def is_broad_glob(pattern: str) -> bool:
    """Return True if a glob is too broad to use in writes: declarations."""
    return pattern.strip() in _BROAD_PATTERNS


def validate_glob(pattern: str) -> None:
    """Raise GlobError on hygiene violation. No return value on success."""
    p = pattern.strip()
    if not p:
        raise GlobError("empty glob")
    if p.startswith("!"):
        raise GlobError(f"negation not allowed: {pattern!r}")
    if p.startswith("/"):
        raise GlobError(f"absolute path not allowed: {pattern!r}")
    if ".." in p.split("/"):
        raise GlobError(f"parent-dir traversal not allowed: {pattern!r}")
    if is_broad_glob(p):
        raise GlobError(f"glob too broad: {pattern!r}")


def _to_regex(pattern: str) -> str:
    """Convert a single glob to a regex matching repo-relative paths.

    Semantics:
      - '**' matches any number of path components (including zero)
      - '*' matches any characters except '/'
      - other characters are literal
    """
    p = pattern.strip()
    parts = p.split("/")
    regex_parts = []
    for part in parts:
        if part == "**":
            regex_parts.append("__DOUBLESTAR__")
        else:
            # fnmatch.translate gives e.g. '(?s:.*\\.py)\\Z' — strip the \\Z anchor
            translated = fnmatch.translate(part)
            # Remove the \Z (end anchor) that fnmatch adds
            translated = re.sub(r"\\Z$", "", translated)
            # Strip the (?s:...) wrapper if present (Python 3.x fnmatch)
            m = re.match(r"^\(\?s:(.*)\)$", translated)
            if m:
                translated = m.group(1)
            # The translated part must not match '/'
            # fnmatch's '.*' for '*' needs to be limited to non-slash chars
            translated = translated.replace(".*", "[^/]*")
            regex_parts.append(translated)

    # Now join the parts, handling ** specially
    # A ** segment expands to "anything (including nothing and slashes)"
    result_parts = []
    i = 0
    while i < len(regex_parts):
        part = regex_parts[i]
        if part == "__DOUBLESTAR__":
            # '**' at start or between slashes: match zero or more path segments
            # Represented as: (anything/)?  or  (/anything)*
            # We'll handle it by collapsing the surrounding slashes
            result_parts.append("__DOUBLESTAR__")
        else:
            result_parts.append(part)
        i += 1

    # Build the final regex by joining with "/" but collapsing ** segments
    # Strategy: build a list of tokens, then join
    tokens = []
    for idx, part in enumerate(result_parts):
        if part == "__DOUBLESTAR__":
            # Replace the slash before + ** + slash after with a single optional segment
            # e.g. "src/**/foo" → "src/" + "**" + "/foo" → "src/(?:.*/)?foo"
            tokens.append("__DOUBLESTAR__")
        else:
            tokens.append(part)

    # Join with "/" then handle ** collapse
    joined = "/".join(tokens)

    # Replace patterns:
    # "**/" at start → "(.+/)?"  (optional prefix path)
    # "/**" at end   → "(/.*)?$" (optional suffix path)
    # "/**/" in middle → "(/[^/]*/)*" — no, actually: "(?:/.*)?" (any path segment)
    # Simpler: replace __DOUBLESTAR__ occurrences in the joined string

    # After joining with "/":
    # leading: "__DOUBLESTAR__/"  → "(?:.+/)?"
    # trailing: "/__DOUBLESTAR__" → "(?:/.*)?"
    # middle: "/__DOUBLESTAR__/" → "(?:/.+)?"  (at least one segment between)
    # bare "**" (entire pattern) → ".*"

    # Handle the different positions:
    regex = joined

    # bare ** (entire pattern, already caught by is_broad_glob but be safe)
    regex = re.sub(r"^__DOUBLESTAR__$", ".*", regex)

    # leading __DOUBLESTAR__/ → match any prefix directory (or none)
    # e.g. "**/package.json" → "(?:.+/)?package\.json"
    regex = re.sub(r"^__DOUBLESTAR__/", "(?:.+/)?", regex)

    # trailing /__DOUBLESTAR__ → match any suffix (or none)
    # e.g. "src/**" → "src(?:/.*)?$"
    regex = re.sub(r"/__DOUBLESTAR__$", "(?:/.*)?", regex)

    # middle /__DOUBLESTAR__/ → match any middle segments
    # e.g. "src/**/foo" → "src/(?:.+/)?foo"
    regex = re.sub(r"/__DOUBLESTAR__/", "/(?:.+/)?", regex)

    return f"^{regex}$"


def _matches(pattern: str, path: str) -> bool:
    """Whether a glob matches a given path."""
    regex = _to_regex(pattern)
    return re.match(regex, path) is not None


def _enumerate_concrete_samples(pattern: str) -> list[str]:
    """Return a small set of concrete paths that the glob would match.

    Used for cross-glob overlap checking. Strategy: replace '**' with synthetic
    middle segments and '*' with synthetic filename, and emit a few variants.
    Includes typed variants (e.g. .py files) so cross-extension globs overlap.
    """
    samples = []
    p = pattern.strip()
    # Variant 1: minimal — '**' becomes empty
    minimal = p.replace("/**/", "/").replace("**/", "").replace("/**", "")
    minimal = minimal.replace("*", "X")
    samples.append(minimal)
    # Variant 2: medium — '**' becomes 'mid/sub'
    medium = p.replace("**", "mid/sub").replace("*", "X")
    samples.append(medium)
    # Variant 3: deep — '**' becomes 'a/b/c/d'
    deep = p.replace("**", "a/b/c/d").replace("*", "Y")
    samples.append(deep)
    # Variant 4: typed — if pattern ends with /**, add common-extension samples
    # so cross-extension globs (e.g. src/auth/** vs **/*.py) are detected.
    if p.endswith("/**"):
        prefix = p[:-3]  # strip trailing /**
        for ext in (".py", ".js", ".ts", ".json", ".yaml", ".md"):
            samples.append(f"{prefix}/file{ext}")
            samples.append(f"{prefix}/mid/sub/file{ext}")
    return [s for s in samples if s and not s.startswith("/")]


def globs_overlap(globs_a: list[str], globs_b: list[str]) -> bool:
    """Return True if any path could match a glob from each set.

    Strategy:
      1. Direct pattern equality → overlap
      2. For each (a, b) pair, generate concrete samples from each side
         and test the OTHER side's match. If any sample from a matches b,
         OR any sample from b matches a → overlap.
      3. Conservative bias: if a glob is broad, treat as overlap (would have
         been rejected by validate_glob upstream, but defensive).
    """
    for a in globs_a:
        for b in globs_b:
            a_norm, b_norm = a.strip(), b.strip()
            if a_norm == b_norm:
                return True
            if is_broad_glob(a_norm) or is_broad_glob(b_norm):
                return True
            # Check a's samples against b
            for sample in _enumerate_concrete_samples(a_norm):
                if _matches(b_norm, sample):
                    return True
            # Check b's samples against a
            for sample in _enumerate_concrete_samples(b_norm):
                if _matches(a_norm, sample):
                    return True
    return False
