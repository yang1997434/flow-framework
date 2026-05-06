import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_state_writer import (
    append_decision, append_review_issue, write_checkpoint, write_blocked,
    DecisionRecord, ReviewIssueRecord,
)


class TestStateWriter(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def test_append_decision_writes_one_line(self):
        rec = DecisionRecord(
            id="d-001", ts="2026-05-05T00:00:00Z",
            phase=2, task="t1", decision="use lib X",
            reason="simpler", alternatives=["Y", "Z"],
            files_affected=["src/a.py"], review_status="pending",
            supersedes=None,
        )
        append_decision(self.task_dir, rec)
        path = self.task_dir / "decisions.jsonl"
        self.assertTrue(path.is_file())
        lines = path.read_text().splitlines()
        self.assertEqual(len(lines), 1)
        loaded = json.loads(lines[0])
        self.assertEqual(loaded["id"], "d-001")

    def test_append_decision_is_appendonly(self):
        for i in range(3):
            append_decision(self.task_dir, DecisionRecord(
                id=f"d-{i:03d}", ts="2026-05-05T00:00:00Z", phase=2,
                task="t1", decision="x", reason="y", alternatives=[],
                files_affected=[], review_status="pending", supersedes=None,
            ))
        path = self.task_dir / "decisions.jsonl"
        self.assertEqual(len(path.read_text().splitlines()), 3)

    def test_review_issue_with_disposition(self):
        rec = ReviewIssueRecord(
            id="r-001", ts="2026-05-05T00:00:00Z", task="t1",
            severity="high", reviewer="codex",
            description="missing null check",
            disposition="fixed",
        )
        append_review_issue(self.task_dir, rec)
        path = self.task_dir / "review-issues.jsonl"
        first_line = path.read_text().splitlines()[0]
        self.assertEqual(json.loads(first_line)["disposition"], "fixed")

    def test_checkpoint_atomic(self):
        ts = "2026-05-05T12-00-00Z"
        write_checkpoint(self.task_dir, ts, body="step: foo\nfiles_changed: []\n",
                         git_hash="abcd123")
        path = self.task_dir / "checkpoints" / f"{ts}.md"
        self.assertTrue(path.is_file())
        content = path.read_text()
        self.assertIn("git_hash: abcd123", content)
        self.assertIn("step: foo", content)

    def test_blocked_md_writes_required_fields(self):
        write_blocked(
            self.task_dir,
            phase=2, task="t1", why_blocked="codex flagged P0",
            required_choice=["fix", "abort", "interactive"],
            safe_resume_command="/flow:resume demo",
        )
        path = self.task_dir / "blocked.md"
        body = path.read_text()
        for f in ("phase: 2", "task: t1", "why_blocked", "required_choice",
                  "safe_resume_command"):
            self.assertIn(f, body)


if __name__ == "__main__":
    unittest.main()
