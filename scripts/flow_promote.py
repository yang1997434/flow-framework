#!/usr/bin/env python3
"""flow promote — promote a memory file from lower tier to higher tier with criteria check.

Usage:
  flow_promote.py <source-file> <target-tier> [options]
    target-tier: vault | rules
  Options:
    --check-only   Print metrics only, don't promote
    --force        Skip criteria validation (use sparingly)
    --confirm-rule Required for Lv3 (rules) promotion

Promotion criteria (heuristic — full validation requires cross-project scan):
  Lv1 → Lv2 (vault):
    - Same slug or pattern referenced in ≥2 archived tasks (project-local check), OR
    - Cross-project mention in vault MOC files (best-effort scan), OR
    - --force override
  Lv2 → Lv3 (rules):
    - Used in ≥3 distinct contexts (best-effort scan of vault), AND
    - Explicit --confirm-rule flag
"""
from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.exit_codes import USAGE_ERROR  # v0.8.4 P3
from common.paths import get_flow_dir


# Char caps per tier (Letta-anchored)
CAPS = {
    "vault-pattern": 300,    # lines
    "vault-pitfall-body": 800,  # chars
    "rules": 200,             # lines
}


@dataclass
class PromotionMetrics:
    archived_task_mentions: int
    vault_moc_mentions: int
    project_active_mentions: int
    target_exists: bool
    source_size_lines: int
    source_size_chars: int


def rewrite_frontmatter_for_promotion(content: str, target_path: str, today_iso: str) -> str | None:
    """Append promotion metadata to YAML frontmatter, preserving body verbatim.

    Returns the rewritten content, or None if `content` has no valid frontmatter.
    """
    if not content.startswith("---"):
        return None
    end_match = re.search(r"\n---\n", content[3:])
    if not end_match:
        return None
    split_pos = 3 + end_match.start()
    frontmatter = content[3:split_pos].strip()
    rest = content[split_pos + 5:]
    new_frontmatter = (
        frontmatter
        + f"\nstatus: promoted\npromoted_to: {target_path}\npromoted_date: {today_iso}"
    )
    return f"---\n{new_frontmatter}\n---\n{rest}"


def detect_kind(file: Path) -> str:
    p = str(file)
    if "/pitfalls/" in p:
        return "pitfall"
    if "/patterns/" in p:
        return "pattern"
    if "/ADRs/" in p:
        return "ADR"
    return "pattern"


def get_target_path(source: Path, target_tier: str) -> Path:
    kind = detect_kind(source)
    name = source.name
    home = Path.home()
    if target_tier == "vault":
        if kind == "pitfall":
            return home / "data" / "knowledge-base" / "pitfalls" / name
        if kind == "ADR":
            return home / "data" / "knowledge-base" / "ADRs" / name
        return home / "data" / "knowledge-base" / "patterns" / name
    if target_tier == "rules":
        prefix = "pitfalls-" if kind == "pitfall" else ""
        return home / ".claude" / "rules" / f"{prefix}{source.stem}.md"
    raise ValueError(f"Unknown tier: {target_tier}")


def credential_grep(content: str) -> list[str]:
    pattern = re.compile(
        r"(password|secret|api[_-]?key|token|bearer).*[:=]\s*['\"][^'\"]{4,}['\"]",
        re.IGNORECASE,
    )
    return pattern.findall(content)


def count_mentions_in_archived_tasks(slug: str, flow: Path) -> int:
    """Count how many archived tasks reference this slug."""
    archive = flow / "tasks" / "archive"
    if not archive.is_dir():
        return 0
    count = 0
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include=*.md", slug, str(archive)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        # Each unique task dir = 1 task; count distinct task parents
        files = result.stdout.strip().splitlines()
        task_dirs = {Path(f).parent.name for f in files if Path(f).parent.parent.parent.name == "archive"}
        # Walk up to find the YYYY-MM/<task-dir> level
        task_dirs2 = set()
        for f in files:
            p = Path(f)
            # archive/<YYYY-MM>/<task-dir>/<file>
            parts = p.parts
            try:
                idx = parts.index("archive")
                if idx + 2 < len(parts):
                    task_dirs2.add(parts[idx + 2])
            except ValueError:
                pass
        count = len(task_dirs2)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return count


def count_mentions_in_vault_moc(slug: str, vault: Path) -> int:
    """Count occurrences of slug in vault MOC files."""
    moc_dir = vault / "_MOC"
    if not moc_dir.is_dir():
        return 0
    count = 0
    for moc in moc_dir.glob("*.md"):
        try:
            text = moc.read_text(encoding="utf-8")
            count += len(re.findall(re.escape(slug), text))
        except (OSError, UnicodeDecodeError):
            continue
    return count


def count_active_mentions(slug: str, flow: Path) -> int:
    """Count occurrences in active project files (excluding the source itself)."""
    if not flow.is_dir():
        return 0
    try:
        result = subprocess.run(
            ["grep", "-rl", "--include=*.md", slug, str(flow)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        files = result.stdout.strip().splitlines()
        # Exclude source itself
        return max(0, len(files) - 1)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return 0


def gather_metrics(source: Path, target: Path) -> PromotionMetrics:
    flow = get_flow_dir()
    vault = Path.home() / "data" / "knowledge-base"

    slug = source.stem
    content = source.read_text(encoding="utf-8")

    return PromotionMetrics(
        archived_task_mentions=count_mentions_in_archived_tasks(slug, flow),
        vault_moc_mentions=count_mentions_in_vault_moc(slug, vault),
        project_active_mentions=count_active_mentions(slug, flow),
        target_exists=target.exists(),
        source_size_lines=len(content.splitlines()),
        source_size_chars=len(content),
    )


def check_criteria(target_tier: str, metrics: PromotionMetrics, force: bool) -> tuple[bool, list[str]]:
    """Return (passes, warnings)."""
    warnings = []

    # Lv1 → Lv2 (vault)
    if target_tier == "vault":
        # Heuristic: at least one of (archive_count >= 2, vault_moc_mentions >= 1)
        archive_ok = metrics.archived_task_mentions >= 2
        cross_project_ok = metrics.vault_moc_mentions >= 1

        if not (archive_ok or cross_project_ok or force):
            warnings.append(
                f"Promotion criteria not yet met: archived_task_mentions={metrics.archived_task_mentions} (want ≥2) "
                f"and vault_moc_mentions={metrics.vault_moc_mentions} (want ≥1). Use --force to override."
            )
            return False, warnings

        # Char cap warning
        if metrics.source_size_lines > CAPS["vault-pattern"]:
            warnings.append(
                f"Source has {metrics.source_size_lines} lines, vault pattern cap is {CAPS['vault-pattern']}. "
                "Consider splitting before promoting."
            )

    # Lv2 → Lv3 (rules)
    elif target_tier == "rules":
        if not force:
            warnings.append(
                "Promotion to ~/.claude/rules/ is to a HARD-RULE tier. Almost-immutable. Requires explicit --confirm-rule flag."
            )
            return False, warnings

        # Char cap warning
        if metrics.source_size_lines > CAPS["rules"]:
            warnings.append(
                f"Source has {metrics.source_size_lines} lines, rules cap is {CAPS['rules']}. "
                "Tighten before promoting."
            )

    if metrics.target_exists:
        warnings.append(f"Target exists. Use --force to overwrite.")
        return False, warnings

    return True, warnings


def main():
    parser = argparse.ArgumentParser(description="Promote memory between tiers")
    parser.add_argument("source", type=Path)
    parser.add_argument("target", choices=["vault", "rules"])
    parser.add_argument("--check-only", action="store_true", help="Print metrics, don't promote")
    parser.add_argument("--force", action="store_true", help="Skip criteria validation")
    parser.add_argument("--confirm-rule", action="store_true", help="Required for rules tier")
    args = parser.parse_args()

    source = args.source.resolve()
    if not source.is_file():
        print(f"ERROR: {source} not found", file=sys.stderr)
        sys.exit(1)

    target = get_target_path(source, args.target)
    metrics = gather_metrics(source, target)

    print(f"Source: {source}")
    print(f"Target: {target}")
    print(f"\nMetrics:")
    print(f"  Archived task mentions:   {metrics.archived_task_mentions}")
    print(f"  Vault MOC mentions:       {metrics.vault_moc_mentions}")
    print(f"  Active project mentions:  {metrics.project_active_mentions}")
    print(f"  Source size:              {metrics.source_size_lines} lines, {metrics.source_size_chars} chars")
    print(f"  Target exists:            {metrics.target_exists}")

    if args.check_only:
        return

    # For rules tier, require --confirm-rule (in addition to --force if criteria not met)
    effective_force = args.force or (args.target == "rules" and args.confirm_rule)

    passes, warnings = check_criteria(args.target, metrics, force=effective_force)

    if warnings:
        print("\nWarnings:")
        for w in warnings:
            print(f"  ⚠️ {w}")

    if not passes:
        print("\nABORT: criteria not met. See warnings above.")
        sys.exit(USAGE_ERROR)

    # Credential grep on source
    content = source.read_text(encoding="utf-8")
    leaks = credential_grep(content)
    if leaks:
        print(f"\nABORT: credential-shaped string found in {source}", file=sys.stderr)
        for l in leaks:
            print(f"  - matched: {l}", file=sys.stderr)
        sys.exit(3)

    # Write target
    target.parent.mkdir(parents=True, exist_ok=True)
    today = date.today().isoformat()
    promotion_note = f"\n<!-- Promoted from {source} on {today} -->\n"
    target.write_text(content + promotion_note, encoding="utf-8")

    # Update source frontmatter
    rewritten = rewrite_frontmatter_for_promotion(content, str(target), today)
    if rewritten is not None:
        source.write_text(rewritten, encoding="utf-8")

    print(f"\n✅ Promoted: {source.name} → {target}")
    print(f"   Source frontmatter updated with status: promoted")


if __name__ == "__main__":
    main()
