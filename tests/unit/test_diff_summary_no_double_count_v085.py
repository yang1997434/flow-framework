"""v0.8.5 codex-review R2-I3A — staged not double-counted as unstaged.

Codex review R2 I3-A: ``_collect_unstaged()`` ran ``git diff --stat
HEAD`` / ``git diff HEAD``, which is "working-tree-vs-HEAD" — that
INCLUDES staged changes. Result: staged lines were double-counted
(once via ``_collect_staged`` and once via ``_collect_unstaged``).

Fix: ``_collect_unstaged()`` uses bare ``git diff --stat`` /
``git diff -U0`` (no ref). Bare ``git diff`` = "working tree vs
index" = unstaged-only.

Reference: ``git help diff`` —
    git diff [<options>] [--] [<path>…​]
        This form is to view the changes you made relative to the
        index (staging area for the next commit). In other words,
        the differences are what you could tell Git to further add
        to the index but you still haven't.
    git diff [<options>] <commit> [--] [<path>…​]
        This form is to view the changes you have in your working
        tree relative to the named <commit>. ...

The two forms differ when there are staged changes: bare returns
working-vs-index; with-commit returns working-vs-commit (= staged
+ unstaged combined).
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


class StagedNotDoubleCountedAsUnstaged(unittest.TestCase):
    """The same path with both staged AND unstaged changes must
    appear in EACH collector's stat list ONCE — not double-counted.
    """

    def test_staged_only_does_not_appear_in_unstaged(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            # Make a tracked file with content + commit, then stage a
            # change WITHOUT modifying working tree further.
            (repo / "f.py").write_text("x = 1\n", encoding="utf-8")
            _git(repo, "add", "f.py")
            _git(repo, "commit", "-q", "-m", "add f")
            # Stage a 5-line addition.
            (repo / "f.py").write_text(
                "x = 1\na = 1\nb = 2\nc = 3\nd = 4\ne = 5\n",
                encoding="utf-8",
            )
            _git(repo, "add", "f.py")
            # Working tree now == index (staged but no further wt edits).

            # _collect_staged sees f.py.
            st_stats, _ = diff_summary._collect_staged(repo)
            self.assertTrue(
                any(p == "f.py" for p, _r in st_stats),
                f"staged collector should see f.py; got {st_stats}",
            )
            # _collect_unstaged MUST NOT see f.py — there are no
            # working-tree-only changes.
            us_stats, _ = diff_summary._collect_unstaged(repo)
            self.assertFalse(
                any(p == "f.py" for p, _r in us_stats),
                f"unstaged collector must NOT see f.py (it would "
                f"double-count staged content); got {us_stats}",
            )

    def test_staged_plus_unstaged_each_seen_only_in_their_source(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            (repo / "g.py").write_text("a\n", encoding="utf-8")
            _git(repo, "add", "g.py")
            _git(repo, "commit", "-q", "-m", "add g")
            # Stage +5 -1 to g.py: write 5 lines + remove the 'a'.
            (repo / "g.py").write_text(
                "1\n2\n3\n4\n5\n", encoding="utf-8",
            )
            _git(repo, "add", "g.py")
            # Now further modify the working tree on top of the
            # index: +3 -2 (delete 2 of the staged lines, add 3
            # new lines).
            (repo / "g.py").write_text(
                "1\n2\n3\nX\nY\nZ\n", encoding="utf-8",
            )
            # Index = "1\n2\n3\n4\n5\n", working = "1\n2\n3\nX\nY\nZ\n".

            st_stats, _ = diff_summary._collect_staged(repo)
            us_stats, _ = diff_summary._collect_unstaged(repo)
            # Both see g.py — but with DIFFERENT counts.
            st_g = [r for p, r in st_stats if p == "g.py"]
            us_g = [r for p, r in us_stats if p == "g.py"]
            self.assertEqual(len(st_g), 1, f"staged: {st_stats}")
            self.assertEqual(len(us_g), 1, f"unstaged: {us_stats}")
            # Sanity: stats strings differ — if they were identical
            # we'd be double-counting (the with-HEAD form yields
            # the SUM of staged + unstaged).
            self.assertNotEqual(
                st_g[0], us_g[0],
                f"staged ({st_g[0]!r}) and unstaged ({us_g[0]!r}) "
                f"counts must differ — identical means unstaged is "
                f"using `git diff HEAD` (the bug)",
            )


class FullSummaryNoDoubleCount(unittest.TestCase):
    """End-to-end: build_diff_summary across a 3-state worktree
    must NOT show the staged path twice in the stat block when
    there are no further unstaged edits."""

    def test_staged_only_shown_once_in_full_summary(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo(Path(td))
            (repo / "single_state.py").write_text(
                "x = 1\n", encoding="utf-8",
            )
            _git(repo, "add", "single_state.py")
            # NO commit, NO further wt edits — pure staged.
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            # Count occurrences in stat-style lines (those starting
            # with " <path> | ").
            stat_lines = [
                ln for ln in text.splitlines()
                if "single_state.py" in ln and "|" in ln
            ]
            self.assertEqual(
                len(stat_lines), 1,
                f"staged-only file should appear exactly ONCE in stat "
                f"block; saw {len(stat_lines)}: {stat_lines}",
            )


if __name__ == "__main__":
    unittest.main()
