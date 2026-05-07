import json
import os
import socket
import sys
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))
from flow_state_writer import (
    append_decision, append_review_issue, write_checkpoint, write_blocked,
    DecisionRecord, ReviewIssueRecord,
    AcceptanceProgressEvent, append_acceptance_progress,
    compute_criterion_hash,
    AutoPrepareLock,
    write_auto_prepare_lock, consume_auto_prepare_lock,
    detect_auto_prepare_state,
    _has_auto_engaged_for, JournalCorruptError,
    # T6 — 10 autonomy event types + helper.
    EVENT_AUTO_ENGAGED, EVENT_TASK_READY_TO_MERGE,
    EVENT_MERGE_STARTED, EVENT_MERGE_APPLIED,
    EVENT_POST_MERGE_VERIFICATION_STARTED,
    EVENT_POST_MERGE_VERIFICATION_COMPLETED,
    EVENT_POST_MERGE_VERIFY_FAILED, EVENT_TASK_COMPLETED,
    EVENT_AUTO_PREPARE_CONSUMED, EVENT_AUTO_PREPARE_INTERRUPTED,
    ALL_AUTONOMY_EVENTS, EVENT_REQUIRED_FIELDS,
    append_autonomy_event, _new_event_id,
)
from safe_io import append_jsonl_locked


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

    def test_blocked_md_block_type_rejects_carriage_return(self):
        """Codex round-3 [P2]: ``block_type`` validation must reject ``\\r``,
        not just ``\\n``. With CR allowed, ``block_type="x\\rts: forged"``
        forges a second frontmatter row on every YAML 1.2 parser (and
        Python's own ``str.splitlines``). The shared helper
        ``_reject_frontmatter_line_separators`` covers the full class,
        but pin the CR case explicitly here because it was the original
        regression.
        """
        with self.assertRaises(ValueError) as ctx:
            write_blocked(
                self.task_dir,
                phase=2, task="t1", why_blocked="x",
                required_choice=["a"], safe_resume_command="r",
                block_type="manifest_violation\rts: forged",
            )
        self.assertIn("line-separator", str(ctx.exception).lower())
        # Fail-closed — no partial blocked.md leaked.
        self.assertFalse((self.task_dir / "blocked.md").exists())

    def test_blocked_md_block_type_rejects_unicode_line_separator(self):
        """[P2] — ensure the full helper class also covers U+2028 on
        block_type, not only on frontmatter_extra. Same rationale as
        the CR case: ``str.splitlines`` treats LSEP as a break, so a
        forged-row attack vector exists on any tool that uses the
        stdlib helper."""
        with self.assertRaises(ValueError):
            write_blocked(
                self.task_dir,
                phase=2, task="t1", why_blocked="x",
                required_choice=["a"], safe_resume_command="r",
                block_type="manifest_violation\u2028ts: forged",
            )
        self.assertFalse((self.task_dir / "blocked.md").exists())

    def test_blocked_md_block_type_rejects_vertical_tab(self):
        """Codex round-4 [P2]: ``\\x0b`` (VT) is in Python's
        ``str.splitlines()`` boundary set. Round-3 missed it; an
        attacker passing ``block_type="x\\x0bts: forged"`` would
        get a forged frontmatter row visible to operator scripts
        that splitlines-parse the file. Pin explicitly here.
        """
        with self.assertRaises(ValueError):
            write_blocked(
                self.task_dir,
                phase=2, task="t1", why_blocked="x",
                required_choice=["a"], safe_resume_command="r",
                block_type="manifest_violation\x0bts: forged",
            )
        self.assertFalse((self.task_dir / "blocked.md").exists())


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


# ----------------------------------------------------------------------
# T5 — auto_prepare.lock state machine + 4-state crash recovery.
# ----------------------------------------------------------------------


class TestAutoPrepareLock(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def _make_lock(self, **overrides) -> AutoPrepareLock:
        defaults = dict(
            lock_version=1, slug="demo", run_id="run-1", task_id="T1",
            contract_path="/tmp/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            # Default to current host so existing assertions about pid /
            # contract / corrupt states continue to exercise THOSE states
            # rather than getting short-circuited by host_mismatch (codex
            # T5 R1 [P2] added the host check). Cross-host scenarios are
            # covered explicitly in TestHostMismatchFailClosed.
            pid=os.getpid(), host=socket.gethostname(), cwd="/tmp",
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        defaults.update(overrides)
        return AutoPrepareLock(**defaults)

    # ----- write -----

    def test_write_lock_creates_file(self):
        path = write_auto_prepare_lock(self.task_dir, self._make_lock())
        self.assertTrue(path.exists())
        self.assertEqual(path.name, "auto_prepare.lock")
        rec = json.loads(path.read_text())
        self.assertEqual(rec["slug"], "demo")
        self.assertEqual(rec["lock_version"], 1)
        self.assertEqual(rec["run_id"], "run-1")
        self.assertEqual(rec["task_id"], "T1")

    def test_write_lock_rejects_when_lock_already_present(self):
        """§8.1: NEVER live alongside another unconsumed lock."""
        write_auto_prepare_lock(self.task_dir, self._make_lock())
        with self.assertRaises(FileExistsError):
            write_auto_prepare_lock(self.task_dir, self._make_lock())

    def test_write_lock_creates_missing_task_dir(self):
        nested = self.tmp / ".flow" / "tasks" / "fresh"
        path = write_auto_prepare_lock(nested, self._make_lock(slug="fresh"))
        self.assertTrue(path.exists())

    # ----- consume -----

    def test_consume_renames_lock_to_consumed(self):
        write_auto_prepare_lock(self.task_dir, self._make_lock())
        consumed = consume_auto_prepare_lock(
            self.task_dir, slug="demo", run_id="run-1", task_id="T1")
        self.assertFalse((self.task_dir / "auto_prepare.lock").exists())
        self.assertTrue((self.task_dir / "auto_prepare.consumed").exists())
        self.assertEqual(consumed.name, "auto_prepare.consumed")

    def test_consume_emits_auto_prepare_consumed_event(self):
        """Y8: explicit consumption proof in decisions.jsonl."""
        write_auto_prepare_lock(self.task_dir, self._make_lock())
        consume_auto_prepare_lock(
            self.task_dir, slug="demo", run_id="run-1", task_id="T1")
        dec_path = self.task_dir / "decisions.jsonl"
        self.assertTrue(dec_path.exists())
        last = json.loads(dec_path.read_text().splitlines()[-1])
        self.assertEqual(last["event"], "auto_prepare_consumed")
        self.assertEqual(last["task_id"], "T1")
        self.assertEqual(last["run_id"], "run-1")
        self.assertEqual(last["slug"], "demo")
        self.assertIn("consumed_at", last)
        self.assertIn("lock_path", last)
        self.assertIn("event_id", last)

    def test_consume_when_no_lock_raises(self):
        with self.assertRaises(FileNotFoundError):
            consume_auto_prepare_lock(
                self.task_dir, slug="demo", run_id="r", task_id="T1")

    # ----- detect: 6 states -----

    def test_state_no_run(self):
        r = detect_auto_prepare_state(
            self.task_dir, run_id="r", task_id="T1",
            current_contract_hash="abc")
        self.assertEqual(r["state"], "no_run")

    def test_state_clean_post_engagement(self):
        """No lock, but auto_engaged exists for this run → normal post-engagement."""
        append_jsonl_locked(self.task_dir / "decisions.jsonl", {
            "event": "auto_engaged", "run_id": "run-1", "task_id": "T1",
        })
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "clean_post_engagement")

    def test_state_active_run_when_pid_alive(self):
        write_auto_prepare_lock(
            self.task_dir, self._make_lock(pid=os.getpid()))
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "active_run")
        self.assertEqual(r["lock"]["pid"], os.getpid())

    def test_state_interrupted_dead_pid(self):
        # pid 2**31 - 1 is well outside the OS pid range — guaranteed dead.
        write_auto_prepare_lock(
            self.task_dir, self._make_lock(pid=2**31 - 1))
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_dead_pid")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")

    def test_state_interrupted_contract_changed(self):
        write_auto_prepare_lock(self.task_dir, self._make_lock(
            pid=os.getpid(), contract_hash="a" * 64))
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="b" * 64)  # mismatch
        self.assertEqual(r["state"], "interrupted_contract_changed")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")

    def test_state_orphan_lock_post_engaged(self):
        """Lock present AND auto_engaged event present → orphan; consume + warn."""
        write_auto_prepare_lock(
            self.task_dir, self._make_lock(pid=os.getpid()))
        append_jsonl_locked(self.task_dir / "decisions.jsonl", {
            "event": "auto_engaged", "run_id": "run-1", "task_id": "T1",
        })
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "orphan_lock_post_engaged")
        self.assertEqual(r["action"], "consume_with_warning")

    # ----- detect: edge cases (D1 / blindspot-A) -----

    def test_state_engaged_only_matches_same_run_and_task(self):
        """`auto_engaged` for a DIFFERENT run_id+task_id MUST NOT count."""
        append_jsonl_locked(self.task_dir / "decisions.jsonl", {
            "event": "auto_engaged", "run_id": "OTHER", "task_id": "T1",
        })
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "no_run")

    def test_state_corrupt_lock_classified_as_interrupted(self):
        """Corrupt JSON in lock → block (state=interrupted_lock_corrupt),
        NOT silent no_run. Distinct state-name from dead_pid avoids D1
        conflation; same block_type so T19 routes identically.
        """
        (self.task_dir / "auto_prepare.lock").write_text(
            "{not valid json", encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_lock_corrupt")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")
        self.assertTrue(r.get("lock_corrupt"))

    def test_state_non_dict_lock_classified_as_interrupted(self):
        """Lock JSON that parses but isn't an object → corrupt branch."""
        (self.task_dir / "auto_prepare.lock").write_text(
            "[1, 2, 3]", encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_lock_corrupt")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")
        self.assertTrue(r.get("lock_corrupt"))

    def test_state_lock_with_null_hash_not_silently_matched(self):
        """`contract_hash: null` MUST mismatch a real current_contract_hash."""
        # Hand-craft a lock with explicit null hash (bypass dataclass).
        (self.task_dir / "auto_prepare.lock").write_text(
            json.dumps({
                "lock_version": 1, "slug": "demo", "run_id": "run-1",
                "task_id": "T1", "contract_path": "/c.json",
                "contract_hash": None,  # explicit null
                "contract_schema_version": 1,
                "created_at": "2026-05-06T00:00:00Z",
                "pid": os.getpid(), "host": "x", "cwd": "/",
                "target_branch": "master",
                "intended_first_task_dispatch_at": "2026-05-06T00:00:01Z",
            }), encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_contract_changed")

    def test_state_lock_with_zero_pid_treated_as_dead(self):
        """pid=0 / pid=-1 are kill(2)-special — treat as dead, not alive."""
        write_auto_prepare_lock(
            self.task_dir, self._make_lock(pid=0))
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_dead_pid")


# ----------------------------------------------------------------------
# Codex T5 R1 [P2] — F1 + F2 fix-pass tests.
# ----------------------------------------------------------------------


class TestJournalCorruptFailClosed(unittest.TestCase):
    """F1: malformed `decisions.jsonl` lines must NOT silent-skip.

    A truncated/corrupt mid-flush could BE the only `auto_engaged` event
    for this task. Silent-skip → caller sees False → recovery
    classifies as no_run → fresh dispatch on top of an interrupted run.
    This is a D2 fallback bypass per
    `.flow/pitfalls/claude-review-blindspots.md`.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def _make_lock(self, **overrides) -> AutoPrepareLock:
        defaults = dict(
            lock_version=1, slug="demo", run_id="run-1", task_id="T1",
            contract_path="/tmp/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=os.getpid(), host=socket.gethostname(), cwd="/tmp",
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        defaults.update(overrides)
        return AutoPrepareLock(**defaults)

    def test_has_auto_engaged_raises_on_malformed_json_line(self):
        """`_has_auto_engaged_for` MUST raise JournalCorruptError, not
        return False, when a line fails JSON parsing."""
        path = self.task_dir / "decisions.jsonl"
        # First line valid, second truncated mid-flush — common crash mode.
        path.write_text(
            json.dumps({"event": "noise", "run_id": "x", "task_id": "y"}) + "\n"
            + '{"event": "auto_engaged", "run_id": "run-1"',  # truncated
            encoding="utf-8",
        )
        with self.assertRaises(JournalCorruptError) as ctx:
            _has_auto_engaged_for(self.task_dir, "run-1", "T1")
        self.assertIn("line 2", str(ctx.exception))

    def test_has_auto_engaged_raises_on_non_dict_line(self):
        """Non-dict (e.g. a JSON list) is also corrupt — must raise."""
        path = self.task_dir / "decisions.jsonl"
        path.write_text("[1, 2, 3]\n", encoding="utf-8")
        with self.assertRaises(JournalCorruptError):
            _has_auto_engaged_for(self.task_dir, "run-1", "T1")

    def test_detect_state_returns_interrupted_journal_corrupt(self):
        """Top-level: detect_auto_prepare_state must catch the
        JournalCorruptError and route to `interrupted_journal_corrupt`.
        Same block_type as the other interrupted states (T19 routes
        identically); distinct state-name preserves cause/effect honesty.
        """
        # Write a valid lock so we exercise the engaged-scan path.
        write_auto_prepare_lock(self.task_dir, self._make_lock())
        path = self.task_dir / "decisions.jsonl"
        path.write_text("{not valid json", encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_journal_corrupt")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")
        self.assertTrue(r.get("journal_corrupt"))
        self.assertIn("parse_error", r)

    def test_corruption_after_match_still_raises(self):
        """Codex T5 R2 [P2]: a valid match followed by a corrupt line
        used to short-circuit (return True before reaching the corruption).
        That bypassed `interrupted_journal_corrupt` and let
        `clean_post_engagement` win even though the journal has an
        integrity problem the operator must see. Now scans the whole
        file and raises after seeing the corruption, regardless of
        match position."""
        path = self.task_dir / "decisions.jsonl"
        path.write_text(
            json.dumps({"event": "auto_engaged", "run_id": "run-1",
                        "task_id": "T1"}) + "\n"
            + '{"event": "trailing_corrupt"',  # truncated
            encoding="utf-8",
        )
        with self.assertRaises(JournalCorruptError):
            _has_auto_engaged_for(self.task_dir, "run-1", "T1")

    def test_control_clean_journal_no_auto_engaged_returns_false(self):
        """Control: a clean journal containing other events but no
        `auto_engaged` returns False normally (no spurious raise)."""
        path = self.task_dir / "decisions.jsonl"
        path.write_text(
            json.dumps({"event": "noise", "run_id": "run-1",
                        "task_id": "T1"}) + "\n"
            + json.dumps({"event": "other", "run_id": "run-1",
                          "task_id": "T1"}) + "\n",
            encoding="utf-8",
        )
        # No raise — clean parse, no match.
        self.assertFalse(_has_auto_engaged_for(self.task_dir, "run-1", "T1"))

    def test_control_clean_journal_state_is_no_run(self):
        """Control through detect_auto_prepare_state: clean journal +
        no lock + no match = the appropriate non-corrupt state (no_run)."""
        path = self.task_dir / "decisions.jsonl"
        path.write_text(
            json.dumps({"event": "noise", "run_id": "x",
                        "task_id": "y"}) + "\n",
            encoding="utf-8",
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "no_run")


class TestHostMismatchFailClosed(unittest.TestCase):
    """F2: cross-host PID-collision guard.

    Lock written on machine A (host="hostA"); task_dir copied to B.
    On B, `_is_pid_alive(lock_pid)` would treat any locally-live PID as
    "the original orchestrator" — a coincidence. Without this check,
    recovery classifies as `active_run` forever and never proceeds.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def _make_lock(self, **overrides) -> AutoPrepareLock:
        defaults = dict(
            lock_version=1, slug="demo", run_id="run-1", task_id="T1",
            contract_path="/tmp/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=os.getpid(), host=socket.gethostname(), cwd="/tmp",
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        defaults.update(overrides)
        return AutoPrepareLock(**defaults)

    def test_mismatched_host_routes_to_host_mismatch_even_when_pid_alive(self):
        """The whole point: with a foreign host, even a locally-LIVE pid
        (our own pid here) MUST NOT classify as active_run."""
        write_auto_prepare_lock(
            self.task_dir,
            self._make_lock(host="some-other-machine", pid=os.getpid()),
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_host_mismatch")
        self.assertEqual(r["block_type"], "auto_prepare_interrupted")
        self.assertEqual(r["lock_host"], "some-other-machine")
        self.assertEqual(r["current_host"], socket.gethostname())

    def test_mismatched_host_routes_to_host_mismatch_when_pid_dead(self):
        """Even with a definitively dead pid, a foreign host should still
        surface as host_mismatch (more specific cause)."""
        write_auto_prepare_lock(
            self.task_dir,
            self._make_lock(host="some-other-machine", pid=2**31 - 1),
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_host_mismatch")

    def test_matching_host_alive_pid_still_active_run(self):
        """Control: matching host + alive pid → active_run (preserved
        pre-existing behavior). This guards against over-tightening."""
        write_auto_prepare_lock(
            self.task_dir,
            self._make_lock(host=socket.gethostname(), pid=os.getpid()),
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "active_run")

    def test_matching_host_dead_pid_still_dead_pid(self):
        """Control: matching host + dead pid → interrupted_dead_pid
        (preserved pre-existing behavior)."""
        write_auto_prepare_lock(
            self.task_dir,
            self._make_lock(host=socket.gethostname(), pid=2**31 - 1),
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_dead_pid")

    def test_missing_host_field_fails_closed(self):
        """A lock without `host` (older v0.8.0-shaped lock) MUST
        fail-closed to host_mismatch — v0.8.1 schema requires `host: str`,
        and silently trusting PID-only on a missing field would be the
        same fallback bypass F2 is fixing.
        """
        # Hand-craft a lock with `host` key absent — bypass dataclass.
        (self.task_dir / "auto_prepare.lock").write_text(
            json.dumps({
                "lock_version": 1, "slug": "demo", "run_id": "run-1",
                "task_id": "T1", "contract_path": "/c.json",
                "contract_hash": "a" * 64, "contract_schema_version": 1,
                "created_at": "2026-05-06T00:00:00Z",
                "pid": os.getpid(),
                # no `host` key
                "cwd": "/", "target_branch": "master",
                "intended_first_task_dispatch_at": "2026-05-06T00:00:01Z",
            }), encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_host_mismatch")
        self.assertIsNone(r["lock_host"])

    def test_explicit_null_host_fails_closed(self):
        """Schema-parsing rule: explicit `host: null` is malformed and
        MUST NOT silently match anything. Mirror the contract_hash
        explicit-null test pattern.
        """
        (self.task_dir / "auto_prepare.lock").write_text(
            json.dumps({
                "lock_version": 1, "slug": "demo", "run_id": "run-1",
                "task_id": "T1", "contract_path": "/c.json",
                "contract_hash": "a" * 64, "contract_schema_version": 1,
                "created_at": "2026-05-06T00:00:00Z",
                "pid": os.getpid(),
                "host": None,  # explicit null
                "cwd": "/", "target_branch": "master",
                "intended_first_task_dispatch_at": "2026-05-06T00:00:01Z",
            }), encoding="utf-8")
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="a" * 64)
        self.assertEqual(r["state"], "interrupted_host_mismatch")
        self.assertIsNone(r["lock_host"])

    def test_contract_changed_takes_precedence_over_host(self):
        """Detection order: contract-mismatch → host → pid. A foreign
        host with a stale contract should surface as
        `interrupted_contract_changed` (more decisive signal)."""
        write_auto_prepare_lock(
            self.task_dir,
            self._make_lock(host="other-host", contract_hash="a" * 64),
        )
        r = detect_auto_prepare_state(
            self.task_dir, run_id="run-1", task_id="T1",
            current_contract_hash="b" * 64)  # mismatch
        self.assertEqual(r["state"], "interrupted_contract_changed")


# ----------------------------------------------------------------------
# T6 — 10 autonomy event types in decisions.jsonl.
#
# Per design §8.4 + §6 R3/R4/R6/Y3/Y8/R10. Tests cover:
#   - Step 6.1: 10 EVENT_* constants exposed + ALL_AUTONOMY_EVENTS tuple.
#   - Step 6.3: append_autonomy_event validates event name + required-field
#     coverage; writes to decisions.jsonl on success.
#   - Step 6.5: DecisionRecord.supersedes list[str] + forward-compat
#     normalization (None → []; str → [str]; default factory []).
#   - Step 6.7: v0.8.0 decision and v0.8.1 event coexist by `event` key.
# ----------------------------------------------------------------------


def _auto_engaged_fields() -> dict:
    """Valid required-field set for `auto_engaged` event (per §8.4)."""
    return {
        "event_id": _new_event_id(),
        "ts": "2026-05-06T00:00:00Z",
        "slug": "demo",
        "run_id": "run-1",
        "task_id": "T1",
        "worktree_id": "demo+t1+abc1234",
        "worktree_path": "/tmp/wt/demo+t1+abc1234",
        "original_base_commit": "abc" * 7 + "1",
        "current_base_commit": "abc" * 7 + "1",
        "lifecycle_state": "active",
        "checkpoint_id": None,
        "contract_path": "/tmp/contract.json",
        "contract_hash": "a" * 64,
        "contract_schema_version": 1,
    }


class TestAutonomyEventConstants(unittest.TestCase):
    """Step 6.1: 10 EVENT_* constants exposed + ALL_AUTONOMY_EVENTS tuple."""

    def test_ten_event_constants_exposed(self):
        names = {
            EVENT_AUTO_ENGAGED, EVENT_TASK_READY_TO_MERGE,
            EVENT_MERGE_STARTED, EVENT_MERGE_APPLIED,
            EVENT_POST_MERGE_VERIFICATION_STARTED,
            EVENT_POST_MERGE_VERIFICATION_COMPLETED,
            EVENT_POST_MERGE_VERIFY_FAILED, EVENT_TASK_COMPLETED,
            EVENT_AUTO_PREPARE_CONSUMED, EVENT_AUTO_PREPARE_INTERRUPTED,
        }
        # Cardinality check — exactly 10, all distinct.
        self.assertEqual(len(names), 10)
        # Tuple `ALL_AUTONOMY_EVENTS` matches the constant set (no extras /
        # no missing).
        self.assertEqual(set(ALL_AUTONOMY_EVENTS), names)
        self.assertEqual(len(ALL_AUTONOMY_EVENTS), 10)

    def test_event_required_fields_covers_every_event(self):
        """Every event in ALL_AUTONOMY_EVENTS must have a required-field
        entry — silent missing-key would be an A-class fallback bypass."""
        for ev in ALL_AUTONOMY_EVENTS:
            self.assertIn(ev, EVENT_REQUIRED_FIELDS,
                          f"event {ev!r} missing required-field entry")
            self.assertGreater(len(EVENT_REQUIRED_FIELDS[ev]), 0)

    def test_required_fields_match_design_84_table(self):
        """Per design §8.4 cardinality (Y4) — verify each event's required
        field count + identity. Catches a refactor that drops a column."""
        # (event, expected required field count, sample required field)
        spec = [
            (EVENT_AUTO_ENGAGED, 14, "contract_schema_version"),
            (EVENT_TASK_READY_TO_MERGE, 12, "diff_hash"),
            (EVENT_MERGE_STARTED, 9, "integration_target"),
            (EVENT_MERGE_APPLIED, 8, "merge_strategy"),
            (EVENT_POST_MERGE_VERIFICATION_STARTED, 8,
             "verification_worktree_path"),
            (EVENT_POST_MERGE_VERIFICATION_COMPLETED, 8, "criteria_results"),
            (EVENT_POST_MERGE_VERIFY_FAILED, 8, "user_choices"),
            (EVENT_TASK_COMPLETED, 8, "final_diff_hash"),
            (EVENT_AUTO_PREPARE_CONSUMED, 7, "consumed_at"),
            (EVENT_AUTO_PREPARE_INTERRUPTED, 7, "blocked_md_path"),
        ]
        for ev, expected_count, sample_field in spec:
            with self.subTest(event=ev):
                req = EVENT_REQUIRED_FIELDS[ev]
                self.assertEqual(len(req), expected_count,
                                 f"{ev}: expected {expected_count} required "
                                 f"fields, got {len(req)}: {sorted(req)}")
                self.assertIn(sample_field, req,
                              f"{ev}: missing sentinel field {sample_field}")

    def test_every_event_includes_run_id(self):
        """Per §6 S2 — `run_id` carried by all autonomy events."""
        for ev in ALL_AUTONOMY_EVENTS:
            self.assertIn("run_id", EVENT_REQUIRED_FIELDS[ev],
                          f"{ev}: missing run_id (S2 invariant)")


class TestAppendAutonomyEvent(unittest.TestCase):
    """Step 6.3: append_autonomy_event validates + writes."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"

    def test_rejects_unknown_event(self):
        with self.assertRaises(ValueError) as cm:
            append_autonomy_event(self.task_dir, "totally_made_up", {})
        self.assertIn("totally_made_up", str(cm.exception))
        # File must NOT be created — fail-closed before disk.
        self.assertFalse((self.task_dir / "decisions.jsonl").exists())

    def test_rejects_missing_required_field(self):
        fields = _auto_engaged_fields()
        fields.pop("worktree_path")
        with self.assertRaises(ValueError) as cm:
            append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, fields)
        self.assertIn("worktree_path", str(cm.exception))
        # Fail-closed: no partial write.
        self.assertFalse((self.task_dir / "decisions.jsonl").exists())

    def test_rejects_event_key_in_fields(self):
        """`fields` MUST NOT contain its own `event` key — that would
        let a buggy caller silently override the validated event-name.
        Fail-closed; do not let the **fields expansion overwrite our
        positional `event` arg."""
        fields = _auto_engaged_fields()
        fields["event"] = "different_event"
        with self.assertRaises(ValueError) as cm:
            append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, fields)
        self.assertIn("'event'", str(cm.exception))

    def test_writes_to_decisions_jsonl(self):
        fields = _auto_engaged_fields()
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, fields)
        path = self.task_dir / "decisions.jsonl"
        self.assertTrue(path.is_file())
        rec = json.loads(path.read_text().splitlines()[-1])
        self.assertEqual(rec["event"], "auto_engaged")
        self.assertEqual(rec["worktree_path"], fields["worktree_path"])
        self.assertEqual(rec["contract_schema_version"], 1)
        # `event` key is the FIRST in the record (visual-grep convention).
        loaded_keys = list(rec.keys())
        self.assertEqual(loaded_keys[0], "event")

    def test_explicit_null_value_satisfies_required(self):
        """Schema-parsing rule: required = "key present", not "value
        truthy". `checkpoint_id: None` is valid (design §8.4 allows it
        for fresh runs).
        """
        fields = _auto_engaged_fields()
        # `checkpoint_id` is already None in the helper; verify it writes.
        self.assertIsNone(fields["checkpoint_id"])
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, fields)
        rec = json.loads(
            (self.task_dir / "decisions.jsonl").read_text().splitlines()[-1])
        self.assertIsNone(rec["checkpoint_id"])

    def test_extra_fields_pass_through(self):
        """Caller may include MORE fields than required (forward-compat
        for additional design columns). Validator only checks REQUIRED
        coverage; extras are persisted as-is."""
        fields = _auto_engaged_fields()
        fields["extra_diagnostic"] = "hello"
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, fields)
        rec = json.loads(
            (self.task_dir / "decisions.jsonl").read_text().splitlines()[-1])
        self.assertEqual(rec["extra_diagnostic"], "hello")

    def test_writes_each_of_ten_events(self):
        """Smoke-coverage: every one of the 10 events accepts a
        minimal-required-fields payload and writes a line. Catches a
        regression where a single event's required-set was mistyped."""
        # Provide a minimal valid fields-dict for each event (exactly the
        # required keys, with synthesized values).
        per_event_fields = {
            EVENT_AUTO_ENGAGED: _auto_engaged_fields(),
            EVENT_TASK_READY_TO_MERGE: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "worktree_id": "wt", "worktree_path": "/p",
                "original_base_commit": "a", "current_base_commit": "a",
                "lifecycle_state": "active", "diff_hash": "h",
                "target_commit_pre_merge": "c",
            },
            EVENT_MERGE_STARTED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "worktree_id": "wt", "worktree_path": "/p",
                "integration_target": "master",
                "target_commit_pre_merge": "c",
            },
            EVENT_MERGE_APPLIED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "worktree_id": "wt",
                "target_commit_post_merge": "d", "merge_strategy": "ff-only",
            },
            EVENT_POST_MERGE_VERIFICATION_STARTED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "verification_worktree_id": "vwt",
                "verification_worktree_path": "/vp",
                "target_commit_post_merge": "d",
            },
            EVENT_POST_MERGE_VERIFICATION_COMPLETED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "verification_worktree_id": "vwt",
                "status": "pass", "criteria_results": [],
            },
            EVENT_POST_MERGE_VERIFY_FAILED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "verification_worktree_id": "vwt",
                "blocked_md_path": "/p/blocked.md",
                "user_choices": ["retry", "abort"],
            },
            EVENT_TASK_COMPLETED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "worktree_id": "wt",
                "final_diff_hash": "h", "target_commit_post_merge": "d",
            },
            EVENT_AUTO_PREPARE_CONSUMED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1",
                "lock_path": "/p/auto_prepare.consumed",
                "consumed_at": "t",
            },
            EVENT_AUTO_PREPARE_INTERRUPTED: {
                "event_id": "e", "ts": "t", "slug": "s", "run_id": "r",
                "task_id": "T1", "lock_path": "/p/auto_prepare.lock",
                "blocked_md_path": "/p/blocked.md",
            },
        }
        for ev in ALL_AUTONOMY_EVENTS:
            with self.subTest(event=ev):
                # Fresh task dir per event so each writes line 1.
                td = self.tmp / f"per-event-{ev}"
                append_autonomy_event(td, ev, per_event_fields[ev])
                lines = (td / "decisions.jsonl").read_text().splitlines()
                self.assertEqual(len(lines), 1)
                self.assertEqual(json.loads(lines[0])["event"], ev)


class TestDecisionRecordSupersedes(unittest.TestCase):
    """Step 6.5/6.6: supersedes is list[str] with forward-compat
    normalization for v0.8.0 single-string + None."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def test_decision_record_supersedes_list(self):
        """Q6.2: list semantics — one retry can resolve multiple priors."""
        rec = DecisionRecord(
            id="d-3", ts="2026-05-06T00:00:00Z", phase=2, task="T1",
            decision="land fix",
            reason="resolved RED issues from rounds 1 and 2",
            supersedes=["d-1", "d-2"],
        )
        append_decision(self.task_dir, rec)
        line = (self.task_dir / "decisions.jsonl").read_text().splitlines()[-1]
        self.assertEqual(json.loads(line)["supersedes"], ["d-1", "d-2"])

    def test_decision_record_supersedes_default_empty_list(self):
        rec = DecisionRecord(
            id="d-1", ts="t", phase=1, task="T1",
            decision="x", reason="y",
        )
        self.assertEqual(rec.supersedes, [])

    def test_decision_record_v080_string_supersedes_normalized(self):
        """Forward-compat: v0.8.0-shape single string normalizes to [str]."""
        rec = DecisionRecord(
            id="d-2", ts="t", phase=1, task="T1",
            decision="x", reason="y",
            supersedes="d-1",  # v0.8.0 shape
        )
        self.assertEqual(rec.supersedes, ["d-1"])

    def test_decision_record_supersedes_none_normalized_to_empty(self):
        rec = DecisionRecord(
            id="d-2", ts="t", phase=1, task="T1",
            decision="x", reason="y", supersedes=None,
        )
        self.assertEqual(rec.supersedes, [])

    def test_decision_record_supersedes_rejects_bad_type(self):
        """Fail-closed: an int / dict / etc. is not a valid supersedes
        shape. Silent acceptance would let bogus JSON hit decisions.jsonl
        and confuse downstream readers (A-class falsy bypass)."""
        with self.assertRaises(ValueError):
            DecisionRecord(
                id="d-x", ts="t", phase=1, task="T1",
                decision="x", reason="y",
                supersedes=123,  # bogus
            )

    def test_decision_record_supersedes_rejects_list_with_non_str(self):
        """Fail-closed on list[non-str] — element type matters for
        on-disk JSON correctness."""
        with self.assertRaises(ValueError):
            DecisionRecord(
                id="d-x", ts="t", phase=1, task="T1",
                decision="x", reason="y",
                supersedes=["d-1", 7],  # mixed
            )


class TestV080V081Coexistence(unittest.TestCase):
    """Step 6.7: v0.8.0 DecisionRecord + v0.8.1 autonomy event in the
    same decisions.jsonl. Reader must disambiguate by `event` presence."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def test_v080_decision_and_v081_event_coexist_in_jsonl(self):
        # v0.8.0-shape decision (no `event` field).
        append_decision(self.task_dir, DecisionRecord(
            id="d-1", ts="2026-05-06T00:00:00Z", phase=1, task="T1",
            decision="x", reason="y",
        ))
        # v0.8.1-shape autonomy event.
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED,
                              _auto_engaged_fields())
        lines = (self.task_dir / "decisions.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 2)
        rec0, rec1 = (json.loads(l) for l in lines)
        # v0.8.0 decision: `event` key is ABSENT (not None).
        self.assertNotIn("event", rec0)
        # v0.8.1 event: `event` key present + names the kind.
        self.assertEqual(rec1["event"], "auto_engaged")
        # v0.8.0 decision must still carry its identity field.
        self.assertEqual(rec0["id"], "d-1")


if __name__ == "__main__":
    unittest.main()
