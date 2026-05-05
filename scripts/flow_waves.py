"""flow waves — wave decomposition preview / inspection.

Usage:
  flow waves --preview <task-slug>      Show planned wave decomposition
  flow waves --show <task-slug>         Show cached wave decomposition (if any)
  flow waves --invalidate <task-slug>   Delete the cache file
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_wave_planner import (  # noqa: E402
    parse_plan_tasks,
    pack_into_waves,
    PLANNER_VERSION,
    _cache_path_for_slug,
    _progress_md_for_slug,
    read_cache,
)


def _render_waves(waves, rationale=None):
    print(f"  PlannerVersion: {PLANNER_VERSION}")
    print(f"  Waves: {len(waves)}")
    for i, wave in enumerate(waves):
        ids = [t.id if hasattr(t, "id") else t for t in wave]
        print(f"    Wave[{i}] (size={len(wave)}): {ids}")
    if rationale:
        print("\n  Rationale:")
        for r in rationale:
            print(f"    - {json.dumps(r)}")


def cli_preview(slug: str) -> int:
    progress = _progress_md_for_slug(slug)
    if not progress.is_file():
        print(f"ERROR: progress.md not found for {slug}", file=sys.stderr)
        return 1
    text = progress.read_text(encoding="utf-8")
    tasks = parse_plan_tasks(text)
    if not tasks:
        print(f"  Task '{slug}': no `### Tasks` block — single-task default")
        return 0
    waves = pack_into_waves(tasks, cap=3)
    print(f"  Task '{slug}': preview")
    _render_waves(waves)
    return 0


def cli_show(slug: str) -> int:
    cache = read_cache(_cache_path_for_slug(slug))
    if cache is None:
        print(f"  Task '{slug}': no cache (run preview or execute Phase 2)")
        return 0
    print(f"  Task '{slug}': cached decomposition")
    print(f"    plan_hash: {cache['plan_hash']}")
    print(f"    base_commit: {cache['base_commit']}")
    print(f"    controller_model: {cache['controller_model']}")
    print(f"    planner_version: {cache['planner_version']}")
    print(f"    cap_used: {cache['cap_used']}")
    print(f"    Waves: {len(cache['waves'])}")
    for wave in cache["waves"]:
        print(f"      Wave[{wave['index']}]: {wave['tasks']}")
    return 0


def cli_invalidate(slug: str) -> int:
    cache_path = _cache_path_for_slug(slug)
    if cache_path.is_file():
        cache_path.unlink()
        print(f"  Task '{slug}': cache deleted")
    else:
        print(f"  Task '{slug}': no cache to delete")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description="flow waves — preview/inspect decomposition")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--preview", metavar="SLUG")
    grp.add_argument("--show", metavar="SLUG")
    grp.add_argument("--invalidate", metavar="SLUG")
    args = ap.parse_args(argv)
    if args.preview:
        sys.exit(cli_preview(args.preview))
    if args.show:
        sys.exit(cli_show(args.show))
    if args.invalidate:
        sys.exit(cli_invalidate(args.invalidate))


if __name__ == "__main__":
    main()
