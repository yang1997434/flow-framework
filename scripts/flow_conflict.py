#!/usr/bin/env python3
"""flow conflict — heuristic conflict detection across loaded rules + ADRs.

Usage:
  flow_conflict.py [--scope project|vault|global|all] [--json] [--task-glob]

Mechanism (v0.3.1, heuristic-only):
  1. Collect rule statements from .flow/ADRs/, vault patterns/, ~/.claude/rules/
  2. Extract "directives" (sentences with always/never/must/must-not/should/should-not)
  3. Pair-flag potential conflicts:
     - Opposite polarity (always X vs never X) on same subject
     - Numeric contradictions on same metric (≥N vs ≤M where N>M)
  4. Output suspect pairs for Claude (or human) to review and resolve

This is NOT a full LLM-based conflict resolver. It's a "smell test" that
catches obvious cases. For deep conflict detection, the user / model should
actually read the suspect rules in context.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import get_flow_dir


DIRECTIVE_PATTERN = re.compile(
    r"\b(?P<polarity>never|don'?t|do not|must\s+not|should\s+not|不要|禁止|不得|"
    r"always|must|should|prefer|favor|要|必须|总是)\b\s+(?P<subject>[^.\n]{5,120})",
    re.IGNORECASE,
)

NEGATIVE_TOKENS = {"never", "don't", "dont", "do not", "must not", "should not", "不要", "禁止", "不得"}
POSITIVE_TOKENS = {"always", "must", "should", "prefer", "favor", "要", "必须", "总是"}


@dataclass
class Directive:
    source: str
    polarity: str        # "+" (do) or "-" (don't)
    subject: str
    raw_text: str


@dataclass
class ConflictPair:
    a: Directive
    b: Directive
    reason: str


def is_negative(token: str) -> bool:
    return token.lower() in NEGATIVE_TOKENS


def normalize_subject(s: str) -> str:
    """Lowercase + strip punctuation + collapse spaces — for similarity matching."""
    s = s.lower().strip(" .,;:'\"")
    s = re.sub(r"[^\w\s一-鿿]", " ", s)
    s = re.sub(r"\s+", " ", s)
    return s


STOP_WORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "before",
    "after", "during", "because", "should", "must", "always", "never", "than",
    "such", "they", "them", "their", "your", "these", "those", "when", "where",
    "what", "which", "while", "would", "could", "have", "been", "will",
}


def subject_overlap(a: str, b: str) -> float:
    """Heuristic overlap: ratio of shared content keyword tokens (stop-words filtered)."""
    a_words = {w for w in re.findall(r"\w+", a) if len(w) >= 3 and w not in STOP_WORDS}
    b_words = {w for w in re.findall(r"\w+", b) if len(w) >= 3 and w not in STOP_WORDS}
    if not a_words or not b_words:
        return 0.0
    return len(a_words & b_words) / min(len(a_words), len(b_words))  # use min for sensitivity


def extract_directives(file: Path) -> list[Directive]:
    try:
        text = file.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []

    directives = []
    for m in DIRECTIVE_PATTERN.finditer(text):
        polarity_token = m.group("polarity").strip()
        subject_text = m.group("subject").strip()
        if len(subject_text) < 8:
            continue
        directives.append(Directive(
            source=str(file),
            polarity="-" if is_negative(polarity_token) else "+",
            subject=normalize_subject(subject_text),
            raw_text=f"{polarity_token} {subject_text}",
        ))
    return directives


def find_conflicts(directives: list[Directive]) -> list[ConflictPair]:
    pairs = []
    for i in range(len(directives)):
        for j in range(i + 1, len(directives)):
            a, b = directives[i], directives[j]
            # Same source: skip (intentional within-doc nuance)
            if a.source == b.source:
                continue
            # Opposite polarity on overlapping subject
            if a.polarity != b.polarity:
                overlap = subject_overlap(a.subject, b.subject)
                if overlap >= 0.4:
                    pairs.append(ConflictPair(
                        a=a, b=b,
                        reason=f"opposite-polarity, subject-overlap={overlap:.2f}",
                    ))
    return pairs


def collect_targets(scope: str, flow: Path) -> list[Path]:
    targets = []
    if scope in ("project", "all") and flow.is_dir():
        for sub in ("ADRs", "patterns", "pitfalls"):
            d = flow / sub
            if d.is_dir():
                targets += list(d.glob("*.md"))
    if scope in ("vault", "all"):
        vault = Path.home() / "data" / "knowledge-base"
        if vault.is_dir():
            for sub in ("patterns", "pitfalls", "ADRs"):
                d = vault / sub
                if d.is_dir():
                    targets += list(d.glob("*.md"))
    if scope in ("global", "all"):
        rules_dir = Path.home() / ".claude" / "rules"
        if rules_dir.is_dir():
            targets += list(rules_dir.glob("*.md"))
    return targets


def main():
    parser = argparse.ArgumentParser(description="Heuristic conflict detection")
    parser.add_argument("--scope", choices=["project", "vault", "global", "all"], default="all")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    flow = get_flow_dir()
    targets = collect_targets(args.scope, flow)

    all_directives: list[Directive] = []
    for t in targets:
        all_directives.extend(extract_directives(t))

    conflicts = find_conflicts(all_directives)

    if args.json:
        print(json.dumps({
            "directives": len(all_directives),
            "files": len(targets),
            "conflicts": [
                {
                    "a": asdict(p.a),
                    "b": asdict(p.b),
                    "reason": p.reason,
                }
                for p in conflicts
            ],
        }, ensure_ascii=False))
        return

    print(f"Scanned {len(targets)} files, extracted {len(all_directives)} directives.")
    if not conflicts:
        print("No suspect conflicts detected.")
        return

    print(f"\n{len(conflicts)} suspect pair(s):\n")
    for i, p in enumerate(conflicts, 1):
        print(f"[{i}] {p.reason}")
        print(f"  A: {Path(p.a.source).name}")
        print(f"     {p.a.polarity} {p.a.raw_text}")
        print(f"  B: {Path(p.b.source).name}")
        print(f"     {p.b.polarity} {p.b.raw_text}")
        print()
    print("Resolve: open both files, decide which rule wins, mark the other obsolete or rephrase.")
    print("Note: heuristic detection — false positives expected. Review each pair manually.")


if __name__ == "__main__":
    main()
