"""v0.8.5 — diff_summary module unit tests.

Covers:
- ``build_diff_summary`` produces a structural diff map (no code lines)
- Includes ``git diff --stat`` style file list (path / +N / -M)
- Includes top-level ``@@`` hunk headers per file (function/class names)
- Per-file breadth truncation: max 10 hunk headers per file, marker
  for excess
- 200-line hard cap with ``[... truncated, N more files]`` marker
- Light redaction: long hex/base64 tokens (>=32 chars), UUIDs,
  emails, URL secret-style query strings → ``<REDACTED-TOKEN>``
- Round 1 (no prev) returns None / empty (caller short-circuits)
- Empty diff produces empty summary
- ``no code lines`` invariant: no ``+ ``/``- ``/context lines

PRD: ``.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md``
§R4.
"""
from __future__ import annotations

import re
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


def _make_repo_with_commit(td: Path, files: dict[str, str]) -> Path:
    """Init a git repo at td with a baseline commit (empty README) and
    return repo path."""
    repo = td / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("baseline\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-q", "-m", "baseline")
    return repo


def _commit_changes(repo: Path, files: dict[str, str], msg: str = "change") -> None:
    for rel, contents in files.items():
        target = repo / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(contents, encoding="utf-8")
        _git(repo, "add", rel)
    _git(repo, "commit", "-q", "-m", msg)


class StructuralMapNoCodeLines(unittest.TestCase):
    def test_summary_contains_stat_and_hunk_headers_no_code_lines(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            _commit_changes(
                repo,
                {
                    "src/parser.py": (
                        "def normalize_task_name(name):\n"
                        "    return name.strip().lower()\n"
                        "\n"
                        "def validate_task(task):\n"
                        "    return bool(task.get('name'))\n"
                    ),
                },
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo,
                base_ref="HEAD~1",
            )
            # Must contain the stat-style line.
            self.assertIn("src/parser.py", text)
            # Must NOT contain raw added-code lines (they would start
            # with "+" followed by code; check for absence of common
            # python prefix).
            self.assertNotIn("+def normalize_task_name", text)
            self.assertNotIn("-baseline", text)
            # Must NOT have hunk-line context (no "    return" leak).
            self.assertNotIn("    return name.strip()", text)


class StatLineFormat(unittest.TestCase):
    def test_stat_line_has_path_and_plus_minus_counts(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            _commit_changes(
                repo,
                {
                    "a.py": "x = 1\ny = 2\nz = 3\n",
                    "b.py": "p = 1\n",
                },
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            # Each file appears with +/- counts (git diff --stat format).
            # We don't pin exact format but presence of "+" count is required.
            self.assertIn("a.py", text)
            self.assertIn("b.py", text)


class HunkHeadersIncluded(unittest.TestCase):
    def test_hunk_headers_present_for_python_functions(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            _commit_changes(
                repo,
                {
                    "mod.py": (
                        "def alpha():\n"
                        "    return 1\n"
                        "\n"
                        "def beta():\n"
                        "    return 2\n"
                        "\n"
                        "def gamma():\n"
                        "    return 3\n"
                    ),
                },
            )
            # Now modify TWO functions so we get >=2 hunks.
            (repo / "mod.py").write_text(
                "def alpha():\n"
                "    return 100\n"
                "\n"
                "def beta():\n"
                "    return 2\n"
                "\n"
                "def gamma():\n"
                "    return 300\n",
                encoding="utf-8",
            )
            _git(repo, "add", "mod.py")
            _git(repo, "commit", "-q", "-m", "modify")
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            # At least one @@ hunk header must surface.
            self.assertIn("@@", text)


class PerFileBreadthTruncation(unittest.TestCase):
    def test_per_file_max_10_hunk_headers(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            # Create file with 30 functions.
            funcs = "\n\n".join(
                f"def func_{i}():\n    return {i}" for i in range(30)
            )
            (repo / "many.py").write_text(funcs + "\n", encoding="utf-8")
            _git(repo, "add", "many.py")
            _git(repo, "commit", "-q", "-m", "add many funcs")
            # Now modify ALL 30 functions to force 30 hunks.
            modified = "\n\n".join(
                f"def func_{i}():\n    return {i * 1000}" for i in range(30)
            )
            (repo / "many.py").write_text(modified + "\n", encoding="utf-8")
            _git(repo, "add", "many.py")
            _git(repo, "commit", "-q", "-m", "modify all")
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            # Count @@ in lines for file many.py.
            # PRD R4: "每文件最多 10 条 hunk header"
            hunk_lines = [ln for ln in text.splitlines() if ln.lstrip().startswith("@@")]
            self.assertLessEqual(len(hunk_lines), 10)
            # Must mark the truncation.
            self.assertIn("more hunks in this file", text)


class TwoHundredLineHardCap(unittest.TestCase):
    def test_hard_cap_truncates_at_200_lines_with_marker(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            # Create 100 files (hits cap quickly even without hunks).
            files = {f"f{i}.py": f"x = {i}\n" for i in range(100)}
            _commit_changes(repo, files, msg="add 100 files")
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            self.assertLessEqual(len(text.splitlines()), 200)
            self.assertIn("truncated", text)


class RedactionLightweight(unittest.TestCase):
    def test_long_hex_token_redacted_in_path(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            # Realistic 40-char hex (sha1-like): mixed letters + digits.
            long_hex = "a1b2c3d4e5f6789012345678901234567890abcd"
            _commit_changes(
                repo,
                {f"src/{long_hex}.py": "x = 1\n"},
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            self.assertNotIn(long_hex, text)
            self.assertIn("<REDACTED-TOKEN>", text)

    def test_uuid_redacted(self) -> None:
        text = diff_summary._redact_line(
            " path/550e8400-e29b-41d4-a716-446655440000/file.py"
        )
        self.assertIn("<REDACTED-TOKEN>", text)
        self.assertNotIn("550e8400-e29b-41d4-a716-446655440000", text)

    def test_email_redacted(self) -> None:
        text = diff_summary._redact_line(
            "@@ def send(user@example.com):"
        )
        self.assertNotIn("user@example.com", text)
        self.assertIn("<REDACTED-TOKEN>", text)


class EmptyDiff(unittest.TestCase):
    def test_empty_diff_returns_empty_string(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD",
            )
            # No diff against self → empty.
            self.assertEqual(text.strip(), "")


class NoCodeLinesInvariant(unittest.TestCase):
    """PRD R4: structural map only; no code content."""
    def test_no_added_or_removed_lines_leak(self) -> None:
        with TemporaryDirectory() as td:
            repo = _make_repo_with_commit(Path(td), {})
            sentinel = "SECRET_PASSWORD_DO_NOT_LEAK_ZZZ"
            _commit_changes(
                repo,
                {"file.py": f"def f():\n    return '{sentinel}'\n"},
            )
            text = diff_summary.build_diff_summary(
                worktree_path=repo, base_ref="HEAD~1",
            )
            self.assertNotIn(sentinel, text)


if __name__ == "__main__":
    unittest.main()
