"""Git helpers for Flow Framework."""
from __future__ import annotations

import subprocess
from pathlib import Path


def is_git_repo(path: Path | None = None) -> bool:
    try:
        subprocess.run(
            ["git", "rev-parse", "--git-dir"],
            cwd=path or Path.cwd(),
            check=True,
            capture_output=True,
        )
        return True
    except (subprocess.CalledProcessError, FileNotFoundError):
        return False


def get_current_branch(path: Path | None = None) -> str | None:
    try:
        result = subprocess.run(
            ["git", "branch", "--show-current"],
            cwd=path or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout.strip() or None
    except subprocess.CalledProcessError:
        return None


def is_dirty(path: Path | None = None) -> bool:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return bool(result.stdout.strip())
    except subprocess.CalledProcessError:
        return False


def get_recent_commits(n: int = 5, path: Path | None = None) -> list[tuple[str, str]]:
    """Return [(hash, subject), ...]."""
    try:
        result = subprocess.run(
            ["git", "log", f"-{n}", "--format=%h|%s"],
            cwd=path or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return [tuple(line.split("|", 1)) for line in result.stdout.strip().splitlines() if "|" in line]
    except subprocess.CalledProcessError:
        return []


def get_diff_stat(path: Path | None = None) -> str:
    try:
        result = subprocess.run(
            ["git", "diff", "--stat"],
            cwd=path or Path.cwd(),
            check=True,
            capture_output=True,
            text=True,
        )
        return result.stdout
    except subprocess.CalledProcessError:
        return ""
