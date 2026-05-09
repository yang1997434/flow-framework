"""v0.8.5 codex-review I3 — diff_summary covers uncommitted state.

Codex review I3: ``build_diff_summary`` previously only ran ``git
diff base..HEAD`` (committed only). Failed-round worktrees rarely
commit their changes — Round 2 prompt got an empty diff map.

Fix: extend ``build_diff_summary`` to merge four states:
1. committed   (base..HEAD)
2. staged      (--cached HEAD)
3. unstaged    (working tree vs HEAD, sans --cached)
4. untracked   (git status --porcelain '??' lines)

Tests cover each state in isolation + a 4-way mixed worktree.
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))

from common import diff_summary  # noqa: E402  type: ignore


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True, capture_output=True, text=True,
    )


def _make_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


class StagedChangesShown(unittest.TestCase):
    def test_staged_only_appears_in_summary(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            (repo / "staged.py").write_text(
                "def foo():\n    return 1\n", encoding="utf-8",
            )
            _git(repo, "add", "staged.py")
            # NO commit — staged but uncommitted.
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            self.assertIn("staged.py", text)


class UnstagedChangesShown(unittest.TestCase):
    def test_unstaged_only_appears_in_summary(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            # Make committed file then modify in worktree (NOT staged).
            (repo / "tracked.py").write_text("a = 1\n", encoding="utf-8")
            _git(repo, "add", "tracked.py")
            _git(repo, "commit", "-q", "-m", "add tracked")
            # Now modify without staging.
            (repo / "tracked.py").write_text(
                "a = 2\nb = 3\n", encoding="utf-8",
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            self.assertIn("tracked.py", text)


class UntrackedFilesShown(unittest.TestCase):
    def test_untracked_only_appears_in_summary_with_new_file_marker(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            (repo / "brand_new.py").write_text(
                "x = 1\n", encoding="utf-8",
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            self.assertIn("brand_new.py", text)
            # PRD R4 fix: untracked files marked as new in summary.
            self.assertIn("new file", text.lower())


class FourWayMixedState(unittest.TestCase):
    def test_committed_staged_unstaged_untracked_all_shown(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            # 1. committed: a.py
            (repo / "a.py").write_text("a = 1\n", encoding="utf-8")
            _git(repo, "add", "a.py")
            _git(repo, "commit", "-q", "-m", "add a")
            # 2. staged: b.py
            (repo / "b.py").write_text("b = 1\n", encoding="utf-8")
            _git(repo, "add", "b.py")
            # 3. unstaged (modify a.py without staging)
            (repo / "a.py").write_text("a = 2\n", encoding="utf-8")
            # 4. untracked: c.py
            (repo / "c.py").write_text("c = 1\n", encoding="utf-8")

            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            # Committed file (a.py) appears.
            self.assertIn("a.py", text)
            # Staged file (b.py) appears.
            self.assertIn("b.py", text)
            # Untracked file (c.py) appears.
            self.assertIn("c.py", text)
            # Untracked is marked.
            self.assertIn("new file", text.lower())


class StillNoCodeLinesLeak(unittest.TestCase):
    """The structural-map invariant must hold across all four state
    sources."""

    def test_secret_in_staged_unstaged_untracked_does_not_leak(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            sentinel = "SECRET_PASSWORD_DO_NOT_LEAK_ZZZ"
            # staged
            (repo / "stg.py").write_text(
                f"x = '{sentinel}'\n", encoding="utf-8",
            )
            _git(repo, "add", "stg.py")
            # unstaged
            (repo / "README.md").write_text(
                f"baseline\nfoo = '{sentinel}'\n", encoding="utf-8",
            )
            # untracked
            (repo / "ut.py").write_text(
                f"y = '{sentinel}'\n", encoding="utf-8",
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            self.assertNotIn(sentinel, text)


if __name__ == "__main__":
    unittest.main()
