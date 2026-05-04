#!/usr/bin/env python3
"""flow init — bootstrap .flow/ skeleton in current project.

Usage:
  flow_init.py [--force]
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import date
from pathlib import Path

# Add parent for common.* imports when run as script
sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import REPO_ROOT, get_global_flow_home


SUBDIRS = [
    "tasks",
    "tasks/archive",
    "ADRs",
    "patterns",
    "pitfalls",
    "workspace",
    ".runtime",
]


GITIGNORE_BLOCK = """
# Flow Framework — runtime + machine-local + per-user workspace
.flow/.runtime/
.flow/.current-task
.flow/config.local.yaml
.flow/workspace/*
!.flow/workspace/.gitkeep
.flow/**/*.tmp
.flow/**/.backup-*
"""


def main():
    parser = argparse.ArgumentParser(description="Bootstrap Flow .flow/ skeleton")
    parser.add_argument("--force", action="store_true", help="Overwrite existing files")
    parser.add_argument("--project-name", default=None, help="Project name (default: cwd basename)")
    args = parser.parse_args()

    project_root = Path.cwd()
    flow_dir = project_root / ".flow"
    project_name = args.project_name or project_root.name
    user = os.environ.get("USER", "user")
    today = date.today().isoformat()

    print(f">> flow init for {project_root}")

    # 1. Create skeleton dirs
    for sub in SUBDIRS:
        d = flow_dir / sub
        d.mkdir(parents=True, exist_ok=True)
        print(f"   [mkdir] {d.relative_to(project_root)}")

    # workspace/<user>
    user_ws = flow_dir / "workspace" / user
    user_ws.mkdir(parents=True, exist_ok=True)
    (user_ws / ".gitkeep").touch()
    (flow_dir / "workspace" / ".gitkeep").touch()

    # 2. Write config.yaml from template
    cfg_target = flow_dir / "config.yaml"
    cfg_template = REPO_ROOT / "templates" / "flow.config.yaml.template"
    if cfg_target.exists() and not args.force:
        print(f"   [skip] {cfg_target.relative_to(project_root)} (exists)")
    elif cfg_template.is_file():
        content = cfg_template.read_text(encoding="utf-8")
        content = content.replace("{{PROJECT_NAME}}", project_name)
        content = content.replace("{{DATE}}", today)
        cfg_target.write_text(content, encoding="utf-8")
        print(f"   [write] {cfg_target.relative_to(project_root)}")

    # 3. Append .gitignore block (if .gitignore exists, append; else create)
    gitignore = project_root / ".gitignore"
    existing = gitignore.read_text(encoding="utf-8") if gitignore.exists() else ""
    if "Flow Framework" not in existing:
        with gitignore.open("a", encoding="utf-8") as f:
            f.write(GITIGNORE_BLOCK)
        print(f"   [append] .gitignore")
    else:
        print(f"   [skip] .gitignore (already has Flow block)")

    # 4. Ensure ~/.flow/ exists with credentials.local stub
    global_home = get_global_flow_home()
    global_home.mkdir(mode=0o700, exist_ok=True)
    cred = global_home / "credentials.local"
    if not cred.exists():
        cred_template = REPO_ROOT / "templates" / "flow.config.local.yaml.template"
        if cred_template.is_file():
            cred.write_text(cred_template.read_text(encoding="utf-8"), encoding="utf-8")
            cred.chmod(0o600)
            print(f"   [write] {cred} (chmod 600) — fill in machine_id + remote_targets")

    # 5. Done
    print()
    print(">> flow init complete.")
    print(f"   Next: tell Claude '/flow:start <task>' or read docs/编码框架.md")


if __name__ == "__main__":
    main()
