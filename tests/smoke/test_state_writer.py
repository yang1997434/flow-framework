import json
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_state_writer import (
    append_decision, append_review_issue, write_checkpoint, write_blocked,
    DecisionRecord, ReviewIssueRecord,
    AcceptanceProgressEvent, append_acceptance_progress,
    compute_criterion_hash,
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


def _make_started_event(**overrides) -> AcceptanceProgressEvent:
    """Valid 24-field `started` event with sensible defaults; override per test."""
    base = dict(
        event_id="evt-1",
        ts="2026-05-06T00:00:00Z",
        slug="demo",
        task_id="T1",
        run_id="run-1",
        worktree_id="demo+t1+abc1234",
        attempt_id="att-1",
        retry_idx=0,
        criterion_id="c1",
        criterion_idx=0,
        criterion_hash="a" * 64,
        type="unit",
        method="cmd",
        idempotent="true",
        event="started",
        started_at="2026-05-06T00:00:00Z",
        completed_at=None,
        timeout_sec=600,
        status=None,
        exit_code=None,
        duration_ms=None,
        stdout_log_path=None,
        stderr_log_path=None,
        command_hash=None,
    )
    base.update(overrides)
    return AcceptanceProgressEvent(**base)


def _make_completed_event(**overrides) -> AcceptanceProgressEvent:
    """Valid 24-field `completed` event with sensible defaults; override per test."""
    base = dict(
        event_id="evt-2",
        ts="2026-05-06T00:00:01Z",
        slug="demo",
        task_id="T1",
        run_id="run-1",
        worktree_id="demo+t1+abc1234",
        attempt_id="att-1",
        retry_idx=0,
        criterion_id="c1",
        criterion_idx=0,
        criterion_hash="a" * 64,
        type="unit",
        method="cmd",
        idempotent="true",
        event="completed",
        started_at="2026-05-06T00:00:00Z",
        completed_at="2026-05-06T00:00:01Z",
        timeout_sec=600,
        status="pass",
        exit_code=0,
        duration_ms=42,
        stdout_log_path="/tmp/out.log",
        stderr_log_path="/tmp/err.log",
        command_hash="b" * 64,
    )
    base.update(overrides)
    return AcceptanceProgressEvent(**base)


class TestAcceptanceProgressEvent(unittest.TestCase):
    def test_event_dataclass_round_trip(self):
        ev = _make_started_event()
        d = asdict(ev)
        self.assertEqual(d["event"], "started")
        self.assertIsNone(d["completed_at"])
        # End-to-end JSON roundtrip — every field must be JSON-serializable.
        roundtripped = json.loads(json.dumps(d))
        self.assertEqual(roundtripped["criterion_hash"], "a" * 64)
        # Confirm the 24-field schema is fully populated (no surprise fields).
        self.assertEqual(len(d), 24)


class TestAppendAcceptanceProgress(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        # Note: append_acceptance_progress should mkdir parents itself.

    def test_append_creates_file_and_writes_line(self):
        append_acceptance_progress(self.task_dir, _make_started_event())
        path = self.task_dir / "acceptance-progress.jsonl"
        self.assertTrue(path.is_file())
        rec = json.loads(path.read_text().strip())
        self.assertEqual(rec["event"], "started")
        self.assertEqual(rec["criterion_id"], "c1")

    def test_append_started_then_completed_pair(self):
        append_acceptance_progress(self.task_dir, _make_started_event())
        append_acceptance_progress(
            self.task_dir,
            _make_completed_event(status="pass", exit_code=0, duration_ms=42),
        )
        lines = (self.task_dir / "acceptance-progress.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        self.assertEqual(json.loads(lines[0])["event"], "started")
        self.assertEqual(json.loads(lines[1])["event"], "completed")
        self.assertEqual(json.loads(lines[1])["status"], "pass")

    def test_rejects_unknown_event(self):
        # Bypass dataclass validation by mutating after construction; the
        # writer must still fail-closed on the bad enum.
        ev = _make_started_event()
        ev = replace(ev, event="unknown")
        with self.assertRaises(ValueError):
            append_acceptance_progress(self.task_dir, ev)

    def test_rejects_unknown_status(self):
        ev = _make_completed_event(status="ok")
        with self.assertRaises(ValueError):
            append_acceptance_progress(self.task_dir, ev)

    def test_rejects_started_with_completed_at(self):
        # Lifecycle invariant: `started` must have null completed_at.
        ev = _make_started_event(completed_at="2026-05-06T00:00:00Z")
        with self.assertRaises(ValueError):
            append_acceptance_progress(self.task_dir, ev)

    def test_rejects_started_with_any_outcome_field(self):
        """Codex T4 R1 [P2]: `started` must reject ALL outcome fields,
        not just completed_at/status/duration_ms. A caller using
        `dataclasses.replace(completed_ev, event="started")` could
        leak exit_code / log paths / command_hash into a "started"
        line, confusing T9's tail reader."""
        leak_cases = [
            ("exit_code", 0),
            ("stdout_log_path", "/tmp/log.out"),
            ("stderr_log_path", "/tmp/log.err"),
            ("command_hash", "abc123"),
        ]
        for field_name, value in leak_cases:
            with self.subTest(field=field_name):
                kwargs = {field_name: value}
                ev = replace(_make_started_event(), **kwargs)
                with self.assertRaises(ValueError) as ctx:
                    append_acceptance_progress(self.task_dir, ev)
                msg = str(ctx.exception)
                self.assertIn(field_name, msg)
                # File must not be created
                self.assertFalse(
                    (self.task_dir / "acceptance-progress.jsonl").exists()
                )

    def test_rejects_completed_without_status(self):
        ev = _make_completed_event(status=None)
        with self.assertRaises(ValueError):
            append_acceptance_progress(self.task_dir, ev)

    def test_invalid_event_does_not_create_file(self):
        # Validation runs BEFORE any disk write — confirm a rejected event
        # leaves no partial file. Important for fail-closed posture.
        ev = replace(_make_started_event(), event="unknown")
        with self.assertRaises(ValueError):
            append_acceptance_progress(self.task_dir, ev)
        self.assertFalse(
            (self.task_dir / "acceptance-progress.jsonl").exists()
        )


class TestCriterionHash(unittest.TestCase):
    def test_criterion_hash_stable_for_same_criterion(self):
        crit = {
            "description": "smoke", "type": "smoke", "method": "cmd",
            "command": "true", "timeout_sec": 30,
        }
        h1 = compute_criterion_hash(crit)
        h2 = compute_criterion_hash({**crit})
        self.assertEqual(h1, h2)
        self.assertEqual(len(h1), 64)  # sha256 hex

    def test_criterion_hash_changes_when_command_changes(self):
        h1 = compute_criterion_hash(
            {"description": "x", "type": "unit", "method": "cmd",
             "command": "true"})
        h2 = compute_criterion_hash(
            {"description": "x", "type": "unit", "method": "cmd",
             "command": "false"})
        self.assertNotEqual(h1, h2)

    def test_criterion_hash_ignores_key_order(self):
        h1 = compute_criterion_hash(
            {"description": "x", "type": "unit", "method": "cmd",
             "command": "true"})
        h2 = compute_criterion_hash(
            {"command": "true", "method": "cmd", "type": "unit",
             "description": "x"})
        self.assertEqual(h1, h2)


if __name__ == "__main__":
    unittest.main()
