#!/usr/bin/env python3
"""flow promote — copy a memory file from lower tier to higher tier.

Usage:
  flow_promote.py <source-file> <target-tier>
    target-tier: vault | rules

STUB: v0.3.1. Currently does a simple copy + status update without
full criteria validation. Full version will check promotion thresholds
(used N times across M projects) before allowing.
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path


def detect_kind(file: Path) -> str:
    """Return 'pattern' / 'pitfall' / 'ADR' based on path or content."""
    p = str(file)
    if "/pitfalls/" in p:
        return "pitfall"
    if "/patterns/" in p:
        return "pattern"
    if "/ADRs/" in p:
        return "ADR"
    return "pattern"  # default


def get_target_path(source: Path, target_tier: str) -> Path:
    kind = detect_kind(source)
    name = source.name
    home = Path.home()
    if target_tier == "vault":
        if kind == "pitfall":
            return home / "data" / "knowledge-base" / "pitfalls" / name
        elif kind == "ADR":
            return home / "data" / "knowledge-base" / "ADRs" / name
        else:
            return home / "data" / "knowledge-base" / "patterns" / name
    elif target_tier == "rules":
        prefix = "pitfalls-" if kind == "pitfall" else ""
        return home / ".claude" / "rules" / f"{prefix}{source.stem}.md"
    raise ValueError(f"Unknown target tier: {target_tier}")


def credential_grep(content: str) -> list[str]:
    pattern = re.compile(
        r"(password|secret|api[_-]?key|token).*[:=]\s*['\"][^'\"]+['\"]",
        re.IGNORECASE,
    )
    return pattern.findall(content)


def main():
    parser = argparse.ArgumentParser(description="Promote a memory file to higher tier")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", choices=["vault", "rules"])
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        print(f"ERROR: {source} not found", file=sys.stderr)
        sys.exit(1)

    content = source.read_text(encoding="utf-8")

    # Credential grep on source
    leaks = credential_grep(content)
    if leaks:
        print(f"ABORT: credential-shaped string found in {source}", file=sys.stderr)
        for l in leaks:
            print(f"  - matched: {l}", file=sys.stderr)
        sys.exit(2)

    target = get_target_path(source, args.target)
    if target.exists() and not args.force:
        print(f"ERROR: {target} exists. Use --force to overwrite.", file=sys.stderr)
        sys.exit(3)

    # Make target dir
    target.parent.mkdir(parents=True, exist_ok=True)

    # Add promotion frontmatter note to content (simple append before ---)
    today = date.today().isoformat()
    promotion_note = f"\n<!-- Promoted from {source} on {today} -->\n"
    target.write_text(content + promotion_note, encoding="utf-8")

    # Update source frontmatter to mark as promoted
    new_source_content = content
    if content.startswith("---"):
        # Append to frontmatter
        end_match = re.search(r"\n---\n", content[3:])
        if end_match:
            split_pos = 3 + end_match.start()
            frontmatter = content[3:split_pos]
            rest = content[split_pos + 5:]  # skip "\n---\n"
            new_frontmatter = frontmatter.rstrip() + f"\nstatus: promoted\npromoted_to: {target}\npromoted_date: {today}\n"
            new_source_content = f"---\n{new_frontmatter}\n---\n{rest}"
            source.write_text(new_source_content, encoding="utf-8")

    print(f"Promoted: {source} → {target}")
    print("Source frontmatter updated with status: promoted")


if __name__ == "__main__":
    main()
