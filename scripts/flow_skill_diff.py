#!/usr/bin/env python3
"""flow skill-diff — analyze newly-installed plugins for capability overlap.

Triggered by SessionStart hook (or manually). Compares ~/.claude/plugins/
state against the last snapshot, then runs cheap keyword overlap analysis
against the flow capability registry. Per-(spec, version) results cached
on disk so re-running is near-instant.

Files:
  ~/.flow/.runtime/skill-snapshot.json         # last-seen plugin set
  ~/.flow/.runtime/skill-diff-cache/<spec>.json # per-skill analysis cache
  ~/.flow/.runtime/skill-diff-pending.md       # pending suggestion (read by hook)

CLI:
  flow_skill_diff.py snapshot          # capture current installed plugins as new snapshot
  flow_skill_diff.py diff              # diff vs snapshot, analyze new plugins, write pending.md
  flow_skill_diff.py show              # print pending.md to stdout
  flow_skill_diff.py clear             # delete pending.md (dismiss)
  flow_skill_diff.py reset-cache       # clear analysis cache (force re-analyze)
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
INSTALLED_PLUGINS = Path.home() / ".claude" / "plugins" / "installed_plugins.json"
RUNTIME = Path.home() / ".flow" / ".runtime"
SNAPSHOT = RUNTIME / "skill-snapshot.json"
CACHE_DIR = RUNTIME / "skill-diff-cache"
PENDING = RUNTIME / "skill-diff-pending.md"

# Tokens that aren't useful semantic signal — strip from keyword sets
STOPWORDS = frozenset({
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "to", "of", "and", "or", "but", "in", "on", "at", "by", "for", "with",
    "from", "into", "onto", "use", "uses", "used", "using", "this", "that",
    "these", "those", "it", "its", "as", "if", "when", "while", "via",
    "user", "users", "you", "your", "i", "we", "they", "them", "their",
    "out", "up", "down", "all", "any", "some", "more", "most", "less",
    "claude", "skill", "skills", "plugin", "plugins", "default", "true",
    "false", "null", "none", "yes", "no", "do", "does", "did", "not",
    "should", "must", "may", "can", "will", "would", "could", "have",
    "has", "had", "make", "makes", "made", "name", "description",
    "type", "command", "tool", "tools", "file", "files", "code", "text",
    "task", "tasks", "phase", "phases", "step", "steps", "run", "runs",
})


def tokenize(text: str) -> set[str]:
    """Lowercase, strip punctuation, drop stopwords; keep tokens len ≥ 4."""
    words = re.findall(r"[a-z][a-z_-]{3,}", text.lower())
    return {w for w in words if w not in STOPWORDS}


def jaccard(a: set, b: set) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def overlap_coef(a: set, b: set) -> float:
    """Szymkiewicz–Simpson coefficient: |A ∩ B| / min(|A|, |B|).

    Better than Jaccard for asymmetric set sizes (capability descriptions are
    short, ~5 tokens; SKILL.md keyword sets are long, ~50). With Jaccard,
    even a perfect match of all capability tokens scores low (~0.1) because
    the union is dominated by the larger set.
    """
    if not a or not b:
        return 0.0
    return len(a & b) / min(len(a), len(b))


def load_installed() -> dict:
    if not INSTALLED_PLUGINS.is_file():
        return {}
    return json.loads(INSTALLED_PLUGINS.read_text(encoding="utf-8")).get("plugins", {})


def find_skill_md_files(install_path: Path, cap: int = 5) -> list[Path]:
    """Find SKILL.md files inside a plugin's install dir, capped to avoid pathological cases."""
    found = []
    if not install_path.is_dir():
        return []
    for f in install_path.rglob("SKILL.md"):
        found.append(f)
        if len(found) >= cap:
            break
    return found


def extract_skill_keywords(install_path: Path, max_lines: int = 100) -> set[str]:
    """Read first N lines from each SKILL.md inside this plugin → unioned keyword set."""
    keywords: set[str] = set()
    for skill_md in find_skill_md_files(install_path):
        try:
            text = "\n".join(skill_md.read_text(encoding="utf-8").splitlines()[:max_lines])
            keywords |= tokenize(text)
        except OSError:
            continue
    return keywords


def extract_capability_keywords(cap: dict) -> set[str]:
    """Mine keywords from a capability registry entry."""
    parts = []
    if isinstance(cap, dict):
        parts.append(str(cap.get("description", "")))
        parts.append(str(cap.get("default", "")))
        for v in cap.get("args", {}).values() if isinstance(cap.get("args"), dict) else []:
            parts.append(str(v))
    return tokenize(" ".join(parts))


def cache_path(spec: str, version: str) -> Path:
    safe = spec.replace("@", "__").replace("/", "_")
    return CACHE_DIR / f"{safe}_{version}.json"


def analyze_plugin(spec: str, version: str, install_path: Path, registry, *, threshold: float = 0.3, top_n: int = 3) -> dict:
    """Return analysis dict; cached per (spec, version)."""
    cache_file = cache_path(spec, version)
    if cache_file.is_file():
        try:
            return json.loads(cache_file.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass  # corrupt cache → re-analyze

    skill_kw = extract_skill_keywords(install_path)
    candidates: list[tuple[str, float, str]] = []
    for cap_name, cap in registry.capabilities.items():
        cap_kw = extract_capability_keywords(cap)
        score = overlap_coef(skill_kw, cap_kw)
        if score >= threshold:
            candidates.append((cap_name, round(score, 4), cap.get("default", "(none)")))
    candidates.sort(key=lambda x: -x[1])
    candidates = candidates[:top_n]

    result = {
        "spec": spec,
        "version": version,
        "analyzed_at": datetime.now().isoformat(timespec="seconds"),
        "skill_keywords_count": len(skill_kw),
        "candidates": candidates,
    }

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8")
    return result


def current_plugin_versions() -> dict[str, str]:
    """Map of plugin spec → installed version, from Claude Code's registry."""
    out = {}
    for spec, entries in load_installed().items():
        if entries:
            ver = entries[0].get("version")
            if ver:
                out[spec] = ver
    return out


def previous_snapshot() -> dict[str, str]:
    if not SNAPSHOT.is_file():
        return {}
    try:
        return json.loads(SNAPSHOT.read_text(encoding="utf-8")).get("plugins", {})
    except json.JSONDecodeError:
        return {}


def detect_new_or_upgraded(current: dict[str, str], previous: dict[str, str]) -> list[tuple[str, str, str]]:
    """Return list of (spec, version, reason) for plugins that are new or upgraded."""
    out = []
    for spec, ver in current.items():
        if spec not in previous:
            out.append((spec, ver, "new"))
        elif previous[spec] != ver:
            out.append((spec, ver, f"upgrade from {previous[spec]}"))
    return out


def write_snapshot(current: dict[str, str]) -> None:
    RUNTIME.mkdir(parents=True, exist_ok=True)
    SNAPSHOT.write_text(
        json.dumps(
            {"captured_at": datetime.now().isoformat(timespec="seconds"), "plugins": current},
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def render_pending_md(analyses: list[dict]) -> str:
    lines = [
        f"# Skill Compatibility Diff — {len(analyses)} new/changed plugin(s)",
        "",
        "_Auto-generated suggestion. Review and decide whether to update flow.config.yaml capability mapping._",
        "",
    ]
    for a in analyses:
        lines.append(f"## `{a['spec']}` v{a['version']} — {a.get('reason', 'new')}")
        lines.append("")
        if not a["candidates"]:
            lines.append("- No capability overlap detected (likely unrelated to flow)")
        else:
            lines.append("Capability overlap candidates (Szymkiewicz–Simpson coefficient, top 3):")
            for cap_name, score, current_default in a["candidates"]:
                lines.append(f"- **{cap_name}** (score {score}) — currently maps to `{current_default}`")
        lines.append("")

    lines.extend([
        "---",
        "",
        "**To replace a default mapping**, edit `<project>/.flow/config.local.yaml`:",
        "",
        "```yaml",
        "capabilities:",
        "  <cap_name>:",
        "    default: <new-skill-spec>",
        "```",
        "",
        "Then run `flow install render-prompts` to re-render. Run `flow skill-diff clear` to dismiss.",
    ])
    return "\n".join(lines) + "\n"


# --- CLI -----------------------------------------------------------------------

def cmd_snapshot(args) -> int:
    current = current_plugin_versions()
    write_snapshot(current)
    print(f"Snapshot captured: {len(current)} plugin(s) → {SNAPSHOT}")
    return 0


def cmd_diff(args) -> int:
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    from flow_capability import load_registry

    current = current_plugin_versions()
    previous = previous_snapshot()
    new_or_upgraded = detect_new_or_upgraded(current, previous)

    if not new_or_upgraded:
        # Do NOT delete an existing pending — user may not have dismissed it yet.
        # Explicit `flow skill-diff clear` is the only way to remove it.
        if not args.quiet:
            print(f"No new/changed plugins (snapshot has {len(previous)}, current {len(current)})")
        return 0

    registry = load_registry()
    installed = load_installed()
    analyses = []
    for spec, version, reason in new_or_upgraded:
        entries = installed.get(spec, [])
        if not entries:
            continue
        install_path = Path(entries[0].get("installPath", ""))
        result = analyze_plugin(spec, version, install_path, registry)
        result["reason"] = reason
        analyses.append(result)

    PENDING.parent.mkdir(parents=True, exist_ok=True)
    PENDING.write_text(render_pending_md(analyses), encoding="utf-8")

    write_snapshot(current)

    if not args.quiet:
        print(f"Wrote {PENDING} ({len(analyses)} plugin(s) analyzed)")
    return 0


def cmd_show(args) -> int:
    if PENDING.is_file():
        sys.stdout.write(PENDING.read_text(encoding="utf-8"))
        return 0
    print("No pending skill-diff suggestion")
    return 0


def cmd_clear(args) -> int:
    if PENDING.is_file():
        PENDING.unlink()
        print(f"Cleared {PENDING}")
        return 0
    print("Nothing to clear")
    return 0


def cmd_reset_cache(args) -> int:
    if CACHE_DIR.is_dir():
        shutil.rmtree(CACHE_DIR)
        print(f"Reset cache at {CACHE_DIR}")
    else:
        print("Cache empty")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Flow skill compatibility diff")
    sub = parser.add_subparsers(dest="cmd", required=True)

    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--quiet", "-q", action="store_true")

    sub.add_parser("snapshot", parents=[common]).set_defaults(func=cmd_snapshot)
    sub.add_parser("diff", parents=[common]).set_defaults(func=cmd_diff)
    sub.add_parser("show").set_defaults(func=cmd_show)
    sub.add_parser("clear").set_defaults(func=cmd_clear)
    sub.add_parser("reset-cache").set_defaults(func=cmd_reset_cache)

    args = parser.parse_args()
    sys.exit(args.func(args))


if __name__ == "__main__":
    main()
