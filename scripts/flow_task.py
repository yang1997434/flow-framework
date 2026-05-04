#!/usr/bin/env python3
"""flow task — task lifecycle management.

Usage:
  flow_task.py create <title> [--slug NAME] [--type TYPE] [--complexity LEVEL]
  flow_task.py start <slug>
  flow_task.py current
  flow_task.py finish
  flow_task.py archive <slug>
  flow_task.py list [--archive]
  flow_task.py status
  flow_task.py switch <slug>
"""
from __future__ import annotations

import argparse
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from common.paths import REPO_ROOT, get_flow_dir, get_current_task_path, get_project_root
from common.config import load_config


def slugify(text: str) -> str:
    """Convert text to kebab-case slug, ASCII only."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    text = text.strip("-")
    return text[:50] if text else "untitled"


# ---------- worktree helpers (v0.4) ----------------------------------------

def _is_git_repo(path: Path) -> bool:
    """Check whether `path` is inside a git repo (not just has a .git dir)."""
    try:
        r = subprocess.run(
            ["git", "-C", str(path), "rev-parse", "--git-dir"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        return r.returncode == 0
    except FileNotFoundError:
        return False


def _git_toplevel(path: Path) -> Path | None:
    """Return git toplevel (the *main* worktree, not a linked one)."""
    try:
        out = subprocess.check_output(
            ["git", "-C", str(path), "rev-parse", "--show-toplevel"],
            stderr=subprocess.DEVNULL, text=True,
        ).strip()
        return Path(out) if out else None
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def _resolve_isolation_mode(project_root: Path) -> str:
    """Read `task_isolation` from .flow/config.yaml. Default: shared."""
    cfg = load_config(project_root)
    mode = cfg.get("task_isolation", "shared")
    if mode not in ("worktree", "branch", "shared"):
        return "shared"
    return mode


def _worktree_path_for(project_root: Path, slug: str) -> Path:
    """Compute the sibling worktree path for a given task slug."""
    repo_name = project_root.name
    return project_root.parent / f"{repo_name}-flow-{slug}"


def _create_worktree(project_root: Path, slug: str) -> tuple[Path | None, str | None]:
    """Try to create a git worktree for the task.

    Returns (worktree_path, error_message). If error_message is set, caller
    must fall back to shared mode and warn.
    """
    if not _is_git_repo(project_root):
        return None, "not a git repo"

    wt_path = _worktree_path_for(project_root, slug)
    branch = f"flow/{slug}"

    if wt_path.exists():
        return None, f"worktree path already exists: {wt_path}"

    try:
        # -b creates and checks out a new branch in the new worktree.
        subprocess.run(
            ["git", "-C", str(project_root), "worktree", "add", str(wt_path), "-b", branch],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return wt_path, None
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace").strip() or str(e)
        return None, f"git worktree add failed: {err}"
    except FileNotFoundError:
        return None, "git executable not found"


def _worktree_is_dirty(wt_path: Path) -> bool | None:
    """Return True if worktree has uncommitted changes or untracked files,
    False if clean, None if status couldn't be determined."""
    try:
        r = subprocess.run(
            ["git", "-C", str(wt_path), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            return None
        return bool(r.stdout.strip())
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None


def _remove_worktree(
    project_root: Path, wt_path: Path, force: bool = False
) -> tuple[bool, str | None]:
    """Try to remove a git worktree. Returns (ok, err).

    `force=False` (default) uses `git worktree remove` without --force, which
    fails if the worktree is dirty — preserving uncommitted work. Callers
    should detect dirtiness via `_worktree_is_dirty` and decide policy.
    """
    if not _is_git_repo(project_root):
        return False, "not a git repo"
    cmd = ["git", "-C", str(project_root), "worktree", "remove", str(wt_path)]
    if force:
        cmd.insert(-1, "--force")
    try:
        subprocess.run(
            cmd,
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        return True, None
    except subprocess.CalledProcessError as e:
        err = (e.stderr or b"").decode("utf-8", "replace").strip() or str(e)
        return False, err
    except FileNotFoundError:
        return False, "git executable not found"


def _read_location(task_dir: Path) -> Path | None:
    loc = task_dir / ".location"
    if not loc.is_file():
        return None
    raw = loc.read_text(encoding="utf-8").strip()
    return Path(raw) if raw else None


def _write_location(task_dir: Path, path: Path) -> None:
    (task_dir / ".location").write_text(str(path) + "\n", encoding="utf-8")


# ---------- progress.md frontmatter parsing --------------------------------

_FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n", re.DOTALL)


def _parse_progress_frontmatter(task_dir: Path) -> dict:
    """Return frontmatter dict for a task's progress.md. Tolerant on missing fields."""
    pmd = task_dir / "progress.md"
    if not pmd.is_file():
        return {}
    text = pmd.read_text(encoding="utf-8")
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return {}
    fm: dict = {}
    body = m.group(1)
    cur_list_key: str | None = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line or line.lstrip().startswith("#"):
            continue
        # list continuation
        stripped = line.lstrip()
        if stripped.startswith("- ") and cur_list_key:
            fm[cur_list_key].append(stripped[2:].strip().strip('"').strip("'"))
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if not val:
                fm[key] = []
                cur_list_key = key
                continue
            cur_list_key = None
            if val.startswith("[") and val.endswith("]"):
                inner = val[1:-1].strip()
                if not inner:
                    fm[key] = []
                else:
                    fm[key] = [
                        x.strip().strip('"').strip("'")
                        for x in inner.split(",") if x.strip()
                    ]
            else:
                fm[key] = val.strip('"').strip("'")
    return fm


# ---------- commands -------------------------------------------------------

def cmd_create(args):
    flow = get_flow_dir()
    project_root = flow.parent
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

    # ----- Task isolation (v0.4) -----
    mode = _resolve_isolation_mode(project_root)
    if mode == "worktree":
        wt_path, err = _create_worktree(project_root, slug)
        if wt_path:
            _write_location(task_dir, wt_path)
            print(f"Worktree: {wt_path} (branch flow/{slug})")
        else:
            # Fallback: shared mode, write current project_root as location, warn.
            _write_location(task_dir, project_root)
            print(
                f"WARN: task_isolation=worktree but worktree creation failed "
                f"({err}); falling back to shared mode.",
                file=sys.stderr,
            )
    elif mode == "branch":
        # Branch mode: just create the branch in-place if git repo. No worktree.
        _write_location(task_dir, project_root)
        if _is_git_repo(project_root):
            try:
                subprocess.run(
                    ["git", "-C", str(project_root), "branch", f"flow/{slug}"],
                    check=False,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except FileNotFoundError:
                pass
    else:
        # shared
        _write_location(task_dir, project_root)


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
    project_root = flow.parent
    candidates = list((flow / "tasks").glob(f"*-{args.slug}"))
    candidates = [c for c in candidates if c.is_dir() and "archive" not in c.parts]
    if not candidates:
        print(f"ERROR: no active task matching '{args.slug}'", file=sys.stderr)
        sys.exit(1)
    if len(candidates) > 1:
        print(f"ERROR: multiple matches: {candidates}", file=sys.stderr)
        sys.exit(1)

    task_dir = candidates[0]

    # Decide whether this archive will clear .current-task BEFORE moving:
    # after shutil.move, task_dir no longer exists, and get_current_task_path()
    # would falsely report any current pointer as stale.
    cur = get_current_task_path()
    was_current = cur is not None and cur.resolve() == task_dir.resolve()

    # ----- Worktree cleanup (v0.4) -----
    # If the task has a .location pointing somewhere other than project_root,
    # treat it as a managed worktree and remove it before the task dir is moved.
    location = _read_location(task_dir)
    worktree_to_remove: Path | None = None
    if location is not None:
        try:
            same = location.resolve() == project_root.resolve()
        except OSError:
            same = False
        if not same and location.exists():
            worktree_to_remove = location

    if worktree_to_remove is not None:
        # Only gate on dirtiness if the worktree IS a git worktree. Otherwise
        # (user removed .git, manual cleanup, etc.) fall through to remove.
        if _is_git_repo(worktree_to_remove):
            dirty = _worktree_is_dirty(worktree_to_remove)
            force = getattr(args, "force", False)
            if dirty is True and not force:
                print(
                    f"ERROR: worktree {worktree_to_remove} has uncommitted "
                    f"changes or untracked files. Commit/stash them, or pass "
                    f"--force to discard and archive anyway.",
                    file=sys.stderr,
                )
                sys.exit(1)
            if dirty is None and not force:
                print(
                    f"WARN: could not determine cleanliness of worktree "
                    f"{worktree_to_remove}; aborting to be safe. Pass --force "
                    f"to override.",
                    file=sys.stderr,
                )
                sys.exit(1)
        ok, err = _remove_worktree(
            project_root, worktree_to_remove, force=getattr(args, "force", False)
        )
        if not ok:
            print(
                f"WARN: failed to remove worktree {worktree_to_remove}: {err}; "
                f"continuing with archive.",
                file=sys.stderr,
            )

    year_month = datetime.now().strftime("%Y-%m")
    archive_dir = flow / "tasks" / "archive" / year_month
    archive_dir.mkdir(parents=True, exist_ok=True)

    target = archive_dir / task_dir.name
    shutil.move(str(task_dir), str(target))

    if was_current:
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


def _slug_of(task_dir: Path) -> str:
    """Strip the MM-DD- date prefix, returning the slug portion."""
    name = task_dir.name
    m = re.match(r"^\d{2}-\d{2}-(.+)$", name)
    return m.group(1) if m else name


def cmd_status(args):
    """Tree view of all active tasks with their status + dependencies.

    Reads progress.md frontmatter for `status` + `phase` + `blocked_by`.
    Prints a simple tree: blockers above blocked tasks (one level of indent).
    """
    flow = get_flow_dir()
    tasks_dir = flow / "tasks"
    if not tasks_dir.is_dir():
        print("(no .flow/tasks directory)")
        return

    cur = get_current_task_path()
    cur_resolved = cur.resolve() if cur else None

    tasks: list[Path] = []
    for entry in sorted(tasks_dir.iterdir()):
        if not entry.is_dir() or entry.name == "archive":
            continue
        tasks.append(entry)

    if not tasks:
        print("(no active tasks)")
        return

    # Build slug -> task_dir map (slug is suffix after MM-DD-)
    by_slug: dict[str, Path] = {}
    fm_cache: dict[Path, dict] = {}
    for t in tasks:
        by_slug[_slug_of(t)] = t
        fm_cache[t] = _parse_progress_frontmatter(t)

    # Compute dependents map: blocker_slug -> [blocked task dirs]
    dependents: dict[str, list[Path]] = {}
    for t in tasks:
        fm = fm_cache[t]
        blockers = fm.get("blocked_by") or []
        if isinstance(blockers, str):
            blockers = [blockers] if blockers else []
        for b in blockers:
            dependents.setdefault(b, []).append(t)

    # Render: top-level = tasks with no blockers (or whose blockers are not in this list)
    blocked_slugs = {_slug_of(t) for t in tasks for b in (fm_cache[t].get("blocked_by") or []) if b in by_slug}
    # actually: a task is "top-level" if no blocker is among current tasks
    def is_top(t: Path) -> bool:
        for b in fm_cache[t].get("blocked_by") or []:
            if b in by_slug:
                return False
        return True

    print("Active tasks:")

    def render(t: Path, depth: int) -> None:
        slug = _slug_of(t)
        fm = fm_cache[t]
        status = fm.get("status") or "active"
        phase = fm.get("phase") or "-"
        marker = " *" if cur_resolved and t.resolve() == cur_resolved else ""
        prefix = "  " * depth + ("- " if depth > 0 else "  ")
        loc = _read_location(t)
        loc_part = ""
        if loc is not None:
            try:
                project_root = flow.parent
                if loc.resolve() != project_root.resolve():
                    loc_part = f"  [worktree: {loc.name}]"
            except OSError:
                pass
        print(f"{prefix}{t.name}  [{status}/{phase}]{marker}{loc_part}")
        # Render dependents (children) under this slug
        for child in dependents.get(slug, []):
            render(child, depth + 1)

    seen: set[Path] = set()
    for t in tasks:
        if not is_top(t):
            continue
        render(t, 0)
        seen.add(t)

    # Any tasks with cycles or external blockers not yet rendered: render flat.
    leftovers = [t for t in tasks if t not in seen]
    if leftovers:
        # Render them (they reference blockers not in the active set)
        for t in leftovers:
            # Skip if we already rendered as a child of another top
            # (children rendering above includes them via `dependents` walk).
            # But the `seen` set only tracks tops; check by traversing dependents.
            pass
        # Simple guarantee: print remaining as top-levels (avoid losing them).
        rendered_via_children: set[Path] = set()
        for top in tasks:
            if not is_top(top):
                continue
            stack = [top]
            while stack:
                cur_t = stack.pop()
                rendered_via_children.add(cur_t)
                for ch in dependents.get(_slug_of(cur_t), []):
                    if ch not in rendered_via_children:
                        stack.append(ch)
        truly_left = [t for t in leftovers if t not in rendered_via_children]
        for t in truly_left:
            render(t, 0)


def cmd_switch(args):
    """Print shell `cd` command(s) for the requested task to stdout.

    Designed for `eval $(flow task switch <slug>)`. If the task has a
    .location pointing at a worktree, cd to that; otherwise cd to project root.
    Also flips .current-task to the requested task.
    """
    flow = get_flow_dir()
    project_root = flow.parent
    candidates = list((flow / "tasks").glob(f"*-{args.slug}"))
    candidates = [c for c in candidates if c.is_dir() and "archive" not in c.parts]
    if not candidates:
        print(f"echo 'flow: no task matching {args.slug}' >&2; false", file=sys.stdout)
        sys.exit(1)
    if len(candidates) > 1:
        names = ", ".join(c.name for c in candidates)
        print(f"echo 'flow: multiple matches: {names}' >&2; false", file=sys.stdout)
        sys.exit(1)

    task_dir = candidates[0]
    # Flip current pointer (side effect — switching IS activating)
    rel = str(task_dir.relative_to(flow.parent))
    (flow / ".current-task").write_text(rel, encoding="utf-8")

    target = _read_location(task_dir) or project_root
    # Sanity: only cd if the directory actually exists; else fall back to project_root.
    if not target.is_dir():
        target = project_root

    print(f"cd {shlex.quote(str(target))}")


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
    p_archive.add_argument(
        "--force", action="store_true",
        help="Discard uncommitted changes in the task's worktree (if any) before archiving",
    )
    p_archive.set_defaults(func=cmd_archive)

    p_list = sub.add_parser("list")
    p_list.add_argument("--archive", action="store_true")
    p_list.set_defaults(func=cmd_list)

    sub.add_parser("status").set_defaults(func=cmd_status)

    p_switch = sub.add_parser("switch")
    p_switch.add_argument("slug")
    p_switch.set_defaults(func=cmd_switch)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
