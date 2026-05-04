#!/usr/bin/env python3
"""flow task — task lifecycle management.

Usage:
  flow_task.py create <title> [--slug NAME] [--type TYPE] [--complexity LEVEL]
  flow_task.py start <slug>
  flow_task.py current
  flow_task.py finish
  flow_task.py archive <slug>
  flow_task.py list [--archive]
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import REPO_ROOT, get_flow_dir, get_current_task_path


def slugify(text: str) -> str:
    """Convert text to kebab-case slug, ASCII only."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:50] if text else "untitled"


def cmd_create(args):
    flow = get_flow_dir()
    if not flow.is_dir():
        print(f"ERROR: {flow} not found. Run flow_init.py first.", file=sys.stderr)
        sys.exit(1)

    slug = args.slug or slugify(args.title)
    date_prefix = date.today().strftime("%m-%d")
    task_dir = flow / "tasks" / f"{date_prefix}-{slug}"

    if task_dir.exists():
        print(f"ERROR: {task_dir} already exists", file=sys.stderr)
        sys.exit(1)

    task_dir.mkdir(parents=True)
    (task_dir / "research").mkdir()

    # Render templates
    today = date.today().isoformat()
    substitutions = {
        "{{TASK_TITLE}}": args.title,
        "{{DATE}}": today,
        "{{SLUG}}": slug,
        "{{TASK_TYPE}}": args.type or "backend",
        "{{COMPLEXITY}}": args.complexity or "moderate",
    }

    for template_name, output_name in [("prd.md.template", "prd.md"), ("progress.md.template", "progress.md")]:
        tpl = REPO_ROOT / "templates" / template_name
        if not tpl.is_file():
            continue
        content = tpl.read_text(encoding="utf-8")
        for k, v in substitutions.items():
            content = content.replace(k, v)
        (task_dir / output_name).write_text(content, encoding="utf-8")

    # Set as current
    (flow / ".current-task").write_text(str(task_dir.relative_to(flow.parent)), encoding="utf-8")

    print(f"Created task: {task_dir}")
    print(f"Active: {task_dir.relative_to(flow.parent)}")


def cmd_start(args):
    flow = get_flow_dir()
    candidates = list((flow / "tasks").glob(f"*-{args.slug}"))
    if not candidates:
        print(f"ERROR: no task matching slug '{args.slug}'", file=sys.stderr)
        sys.exit(1)
    if len(candidates) > 1:
        print(f"ERROR: multiple matches: {candidates}", file=sys.stderr)
        sys.exit(1)

    task_dir = candidates[0]
    (flow / ".current-task").write_text(str(task_dir.relative_to(flow.parent)), encoding="utf-8")
    print(f"Active: {task_dir.relative_to(flow.parent)}")


def cmd_current(args):
    cur = get_current_task_path()
    if cur:
        print(cur)
    else:
        print("(no active task)")
        sys.exit(1)


def cmd_finish(args):
    flow = get_flow_dir()
    pointer = flow / ".current-task"
    if not pointer.is_file():
        print("(no active task)", file=sys.stderr)
        sys.exit(1)
    pointer.unlink()
    print("Cleared current-task pointer.")


def cmd_archive(args):
    flow = get_flow_dir()
    candidates = list((flow / "tasks").glob(f"*-{args.slug}"))
    candidates = [c for c in candidates if c.is_dir() and "archive" not in c.parts]
    if not candidates:
        print(f"ERROR: no active task matching '{args.slug}'", file=sys.stderr)
        sys.exit(1)
    if len(candidates) > 1:
        print(f"ERROR: multiple matches: {candidates}", file=sys.stderr)
        sys.exit(1)

    task_dir = candidates[0]
    year_month = datetime.now().strftime("%Y-%m")
    archive_dir = flow / "tasks" / "archive" / year_month
    archive_dir.mkdir(parents=True, exist_ok=True)

    target = archive_dir / task_dir.name
    shutil.move(str(task_dir), str(target))

    # If was current, clear pointer
    cur = get_current_task_path()
    if cur is None or not cur.is_dir():
        ptr = flow / ".current-task"
        if ptr.is_file():
            ptr.unlink()

    print(f"Archived: {target}")


def cmd_list(args):
    flow = get_flow_dir()
    if args.archive:
        archive = flow / "tasks" / "archive"
        if archive.is_dir():
            for ym_dir in sorted(archive.iterdir()):
                if not ym_dir.is_dir():
                    continue
                for task in sorted(ym_dir.iterdir()):
                    if task.is_dir():
                        print(f"{ym_dir.name}/{task.name}")
    else:
        tasks_dir = flow / "tasks"
        cur = get_current_task_path()
        for task in sorted(tasks_dir.iterdir()):
            if not task.is_dir() or task.name == "archive":
                continue
            marker = " (active)" if cur and cur.resolve() == task.resolve() else ""
            print(f"{task.name}{marker}")


def main():
    parser = argparse.ArgumentParser(description="Flow task lifecycle")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_create = sub.add_parser("create")
    p_create.add_argument("title")
    p_create.add_argument("--slug")
    p_create.add_argument("--type", choices=["backend", "frontend", "data", "doc", "deploy", "research"])
    p_create.add_argument("--complexity", choices=["trivial", "simple", "moderate", "complex"])
    p_create.set_defaults(func=cmd_create)

    p_start = sub.add_parser("start")
    p_start.add_argument("slug")
    p_start.set_defaults(func=cmd_start)

    sub.add_parser("current").set_defaults(func=cmd_current)
    sub.add_parser("finish").set_defaults(func=cmd_finish)

    p_archive = sub.add_parser("archive")
    p_archive.add_argument("slug")
    p_archive.set_defaults(func=cmd_archive)

    p_list = sub.add_parser("list")
    p_list.add_argument("--archive", action="store_true")
    p_list.set_defaults(func=cmd_list)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
