"""Path resolution helpers for Flow Framework."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parent.parent.parent  # flow-framework/


def get_project_root(start: Path | None = None) -> Path:
    """Find the project root (contains .git or .flow/) from given dir up."""
    cur = (start or Path.cwd()).resolve()
    while cur != cur.parent:
        if (cur / ".flow").is_dir() or (cur / ".git").is_dir():
            return cur
        cur = cur.parent
    return Path.cwd()


def get_flow_dir(project_root: Path | None = None) -> Path:
    """Get the .flow/ directory in the project."""
    return (project_root or get_project_root()) / ".flow"


def get_current_task_path() -> Path | None:
    """Read .flow/.current-task and return resolved task dir, or None."""
    flow = get_flow_dir()
    pointer = flow / ".current-task"
    if not pointer.is_file():
        return None
    raw = pointer.read_text(encoding="utf-8").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.is_absolute():
        p = flow.parent / p
    return p if p.is_dir() else None


def get_user_workspace(project_root: Path | None = None) -> Path:
    """Per-user workspace dir under .flow/workspace/<user>/."""
    user = os.environ.get("USER") or os.environ.get("LOGNAME") or "unknown"
    return get_flow_dir(project_root) / "workspace" / user


def get_global_flow_home() -> Path:
    """~/.flow/ for cross-project credentials + global config."""
    return Path.home() / ".flow"


def get_template(name: str) -> Path:
    """Get path to a template file. e.g. 'prd.md.template'."""
    return REPO_ROOT / "templates" / name


def get_machine_id() -> str:
    """Get machine_id from ~/.flow/credentials.local or fallback to hostname."""
    cred = get_global_flow_home() / "credentials.local"
    if cred.is_file():
        for line in cred.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line.startswith("machine_id:"):
                val = line.split(":", 1)[1].strip().strip('"').strip("'")
                if val and not val.startswith("<"):
                    return val
    try:
        return subprocess.check_output(["hostname", "-s"], text=True).strip()
    except Exception:
        return "unknown"
