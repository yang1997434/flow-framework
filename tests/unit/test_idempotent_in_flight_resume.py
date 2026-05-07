"""T9 unit tests — find_resume_point + resolve_in_flight_idempotency.

Covers every R8 cell from design line 268-275 + the four ResumePoint
states (no events / completed-chain / dangling started / cross-attempt).

Plan §9.1, 9.3, 9.5. The test file is the FIRST artifact in tests/unit/;
tests/smoke/run.sh discovers this directory in addition to tests/smoke/.

Blindspot notes (per .flow/pitfalls/claude-review-blindspots.md):
- A (.get falsy): assertions read parsed dataclass fields where typed,
  and in_flight_event dicts where ``event`` and ``criterion_idx`` are
  required string/int fields. We treat absent fields as malformed and
  do not silently route them.
- B (cross-ref): the R8 table cells follow design lines 268-275 verbatim;
  the e2e bypass row (line 275, "NO override accepted") is exercised
  explicitly and matches PRD §1.3.
- D5 (catch-all): malformed JSON line in acceptance-progress.jsonl is
  exercised — find_resume_point must skip + log, not crash.
"""
from __future__ import annotations

import json
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_acceptance import (  # noqa: E402
    AcceptanceRunner,
    IdempotencyVerdict,
    ResumePoint,
)
from flow_contract import AcceptanceCriterion, Contract  # noqa: E402
from flow_state_writer import (  # noqa: E402
    AcceptanceProgressEvent,
    append_acceptance_progress,
)


class _RunnerFixtureBase(unittest.TestCase):
    """Shared setUp + helpers for the three T9 unit suites.

    Helpers write paired ``started`` + ``completed`` events (or just a
    dangling ``started``) into ``acceptance-progress.jsonl`` so each
    test can construct the exact tail-state it wants to exercise.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        log_dir = self.tmp / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        self.runner = AcceptanceRunner(
            worktree_root=self.tmp,
            log_dir=log_dir,
            slug="d",
            task_id="T",
            run_id="r",
            worktree_id="w",
        )

    # ------------------------------------------------------------------
    # Event-fixture writers — go through append_acceptance_progress so
    # the schema validator (Q6.1 invariants) gates everything.
    # ------------------------------------------------------------------

    def _make_event(
        self,
        criterion_idx: int,
        *,
        attempt_id: str,
        event: str,
        type_: str = "unit",
        method: str = "cmd",
        status: str | None = None,
        completed_at: str | None = None,
        duration_ms: int | None = None,
        criterion_hash: str | None = None,
    ) -> AcceptanceProgressEvent:
        return AcceptanceProgressEvent(
            event_id=f"e{criterion_idx}_{event}",
            ts="2026-05-06T00:00:00Z",
            slug="d",
            task_id="T",
            run_id="r",
            worktree_id="w",
            attempt_id=attempt_id,
            retry_idx=0,
            criterion_id=f"c{criterion_idx}",
            criterion_idx=criterion_idx,
            criterion_hash=("0" * 64) if criterion_hash is None else criterion_hash,
            type=type_,
            method=method,
            idempotent="false" if method == "cmd" else "true",
            event=event,
            started_at="2026-05-06T00:00:00Z",
            completed_at=completed_at,
            timeout_sec=30,
            status=status,
            exit_code=0 if status == "pass" else None,
            duration_ms=duration_ms,
            stdout_log_path=None,
            stderr_log_path=None,
            command_hash=None,
        )

    def _write_completed_pair(
        self,
        criterion_idx: int,
        *,
        attempt_id: str,
        type_: str = "unit",
        method: str = "cmd",
    ) -> None:
        append_acceptance_progress(
            self.tmp,
            self._make_event(
                criterion_idx,
                attempt_id=attempt_id,
                event="started",
                type_=type_,
                method=method,
            ),
        )
        append_acceptance_progress(
            self.tmp,
            self._make_event(
                criterion_idx,
                attempt_id=attempt_id,
                event="completed",
                type_=type_,
                method=method,
                status="pass",
                completed_at="2026-05-06T00:00:01Z",
                duration_ms=1000,
            ),
        )

    def _write_started_only(
        self,
        criterion_idx: int,
        *,
        attempt_id: str,
        type_: str = "unit",
        method: str = "cmd",
        criterion_hash: str | None = None,
    ) -> None:
        append_acceptance_progress(
            self.tmp,
            self._make_event(
                criterion_idx,
                attempt_id=attempt_id,
                event="started",
                type_=type_,
                method=method,
                criterion_hash=criterion_hash,
            ),
        )

    # ------------------------------------------------------------------
    # Builders for criterion + contract — minimal, only fields the
    # resolver reads.
    # ------------------------------------------------------------------

    def _crit(self, **kw) -> AcceptanceCriterion:
        return AcceptanceCriterion(
            description=kw.get("description", "x"),
            type=kw.get("type", "unit"),
            method=kw.get("method", "cmd"),
            command=kw.get("command", "true"),
            path=kw.get("path"),
            url=kw.get("url"),
            json_query=kw.get("json_query"),
            timeout_sec=kw.get("timeout_sec", 30),
            idempotent=kw.get("idempotent"),
        )

    def _contract(self, **overrides) -> Contract:
        defaults = dict(
            contract_schema_version=1,
            autonomy_mode="auto",
            created_at="2026-05-06T00:00:00Z",
        )
        # Allow tests to pass `idempotent_cmd_allowlist=[]` to disable the
        # default allowlist; the dataclass default would otherwise pre-seed
        # pytest / mypy / etc. and bleed into the cmd-block-by-default test.
        defaults.update(overrides)
        return Contract(**defaults)


class TestFindResumePoint(_RunnerFixtureBase):
    """Plan §9.1 — tail-scan correctness."""

    def test_no_events_resume_at_zero(self) -> None:
        rp = self.runner.find_resume_point(self.tmp, attempt_id="a1")
        self.assertEqual(rp.next_idx, 0)
        self.assertIsNone(rp.in_flight_criterion_idx)
        self.assertIsNone(rp.in_flight_event)

    def test_no_jsonl_file_resume_at_zero(self) -> None:
        # Bare directory with no progress file at all → idx=0, no in-flight.
        empty = self.tmp / "empty"
        empty.mkdir()
        rp = self.runner.find_resume_point(empty, attempt_id="a1")
        self.assertEqual(rp.next_idx, 0)
        self.assertIsNone(rp.in_flight_criterion_idx)

    def test_completed_chain_resumes_at_next(self) -> None:
        for idx in (0, 1, 2):
            self._write_completed_pair(idx, attempt_id="a1")
        rp = self.runner.find_resume_point(self.tmp, attempt_id="a1")
        self.assertEqual(rp.next_idx, 3)
        self.assertIsNone(rp.in_flight_criterion_idx)

    def test_started_without_completed_marks_in_flight(self) -> None:
        self._write_completed_pair(0, attempt_id="a1")
        self._write_started_only(1, attempt_id="a1")
        rp = self.runner.find_resume_point(self.tmp, attempt_id="a1")
        self.assertEqual(rp.in_flight_criterion_idx, 1)
        self.assertIsNotNone(rp.in_flight_event)
        self.assertEqual(rp.in_flight_event["criterion_idx"], 1)
        self.assertEqual(rp.in_flight_event["event"], "started")

    def test_other_attempts_ignored(self) -> None:
        """Different attempt_id (e.g., prior retry) does NOT count as
        in-flight for the current attempt."""
        self._write_completed_pair(0, attempt_id="prior")
        self._write_started_only(1, attempt_id="prior")
        rp = self.runner.find_resume_point(self.tmp, attempt_id="current")
        self.assertEqual(rp.next_idx, 0)
        self.assertIsNone(rp.in_flight_criterion_idx)

    def test_malformed_json_line_is_skipped(self) -> None:
        """D5 catch-all: invalid JSON line must not crash the tail scan."""
        # Write one good completed-pair, then an unparseable line, then
        # another good pair. The scan should yield next_idx=2 (both pairs
        # honored, garbage skipped).
        self._write_completed_pair(0, attempt_id="a1")
        progress_path = self.tmp / "acceptance-progress.jsonl"
        with progress_path.open("a", encoding="utf-8") as fh:
            fh.write("this is not json {[\n")
        self._write_completed_pair(1, attempt_id="a1")
        rp = self.runner.find_resume_point(self.tmp, attempt_id="a1")
        self.assertEqual(rp.next_idx, 2)
        self.assertIsNone(rp.in_flight_criterion_idx)

    def test_stale_started_below_completed_discarded(self) -> None:
        """If a started event sits at an idx <= last completed, it's a
        retry-iteration leftover, NOT a fresh in-flight entry."""
        # Manually craft an out-of-order tail: started at idx=0 last but
        # idx=1 already completed → idx=0 is stale.
        self._write_completed_pair(0, attempt_id="a1")
        self._write_completed_pair(1, attempt_id="a1")
        # An older retry's started event for idx=0 (e.g., re-run on retry).
        self._write_started_only(0, attempt_id="a1")
        rp = self.runner.find_resume_point(self.tmp, attempt_id="a1")
        # next_idx is still 2 (max completed + 1); stale started ignored.
        self.assertEqual(rp.next_idx, 2)
        self.assertIsNone(rp.in_flight_criterion_idx)


class TestResolveInFlightIdempotency(_RunnerFixtureBase):
    """Plan §9.3 — every R8 cell from design line 268-275."""

    # --- read-only methods: always auto-rerun ---

    def test_file_exists_in_flight_auto_reruns(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(method="file_exists", path="VERSION", command=None),
            contract=self._contract(),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")
        self.assertIn("read-only", v.reason.lower())

    def test_json_query_in_flight_auto_reruns(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                method="json_query",
                path="c.json",
                json_query="x",
                command=None,
            ),
            contract=self._contract(),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")
        self.assertIn("read-only", v.reason.lower())

    # --- cmd: default block; allowlist OR rationale unblocks ---

    def test_cmd_in_flight_default_blocks(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(method="cmd", command="rm -rf build"),
            contract=self._contract(idempotent_cmd_allowlist=[]),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "block_in_flight")
        self.assertIn("non-idempotent", v.reason.lower())

    def test_cmd_in_flight_allowlist_unblocks(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(method="cmd", command="pytest tests/smoke"),
            contract=self._contract(
                idempotent_cmd_allowlist=["pytest", "mypy"],
            ),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")
        self.assertIn("allowlist", v.reason.lower())

    def test_cmd_in_flight_multiword_allowlist_unblocks(self) -> None:
        """`flow doctor` is a two-word allowlist entry — must match the
        whole prefix, not just the binary."""
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(method="cmd", command="flow doctor --json"),
            contract=self._contract(
                idempotent_cmd_allowlist=["flow doctor"],
            ),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")

    def test_cmd_in_flight_rationale_unblocks(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                method="cmd",
                command="curl -s http://x/health",
                idempotent={
                    "value": True,
                    "rationale": "GET against stub",
                    "timeout_sec": 30,
                    "side_effect_class": "read_only",
                },
            ),
            contract=self._contract(idempotent_cmd_allowlist=[]),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")
        self.assertIn("override", v.reason.lower())

    def test_cmd_rationale_value_false_does_not_unblock(self) -> None:
        """An override with value=false is honest declaration of
        non-idempotence; must keep the block."""
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                method="cmd",
                command="x",
                idempotent={
                    "value": False,
                    "rationale": "side effects",
                    "timeout_sec": 30,
                    "side_effect_class": "reversible",
                },
            ),
            contract=self._contract(idempotent_cmd_allowlist=[]),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "block_in_flight")

    def test_cmd_rationale_missing_does_not_unblock(self) -> None:
        """value=true without rationale is incomplete; must keep block."""
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                method="cmd",
                command="x",
                idempotent={
                    "value": True,
                    "rationale": "",
                    "timeout_sec": 30,
                    "side_effect_class": "read_only",
                },
            ),
            contract=self._contract(idempotent_cmd_allowlist=[]),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "block_in_flight")

    # --- http: GET-only in T7 → safe by default ---

    def test_http_get_in_flight_auto_reruns(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                method="http",
                url="http://localhost/health",
                command=None,
            ),
            contract=self._contract(),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "auto_rerun")
        self.assertIn("get", v.reason.lower())

    # --- e2e: ALWAYS block (design line 275; PRD §1.3) ---

    def test_e2e_type_always_blocks_no_override(self) -> None:
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                type="e2e",
                method="cmd",
                command="playwright test",
                idempotent={
                    "value": True,
                    "rationale": "stub-only",
                    "timeout_sec": 30,
                    "side_effect_class": "read_only",
                },
            ),
            contract=self._contract(
                idempotent_cmd_allowlist=["playwright"],
            ),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "block_in_flight")
        self.assertIn("e2e", v.reason.lower())

    def test_e2e_type_with_file_exists_method_still_blocks(self) -> None:
        """Type wins over method per design line 275 — even a read-only
        method under type=e2e blocks on in-flight."""
        v = self.runner.resolve_in_flight_idempotency(
            self._crit(
                type="e2e",
                method="file_exists",
                path="VERSION",
                command=None,
            ),
            contract=self._contract(),
            in_flight_event={"event": "started"},
        )
        self.assertEqual(v.decision, "block_in_flight")


class TestResumeAttempt(_RunnerFixtureBase):
    """Plan §9.5 — orchestration helper that combines find_resume_point
    + resolve_in_flight_idempotency for the orchestrator."""

    def test_no_in_flight_returns_auto_rerun_no_op(self) -> None:
        # Empty tail → no in-flight; auto_rerun pass-through.
        criteria = [self._crit()]
        verdict, rp = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=criteria,
            contract=self._contract(),
        )
        self.assertEqual(verdict.decision, "auto_rerun")
        self.assertIn("no in-flight", verdict.reason.lower())
        self.assertEqual(rp.next_idx, 0)

    def test_resume_with_in_flight_safe_method_continues(self) -> None:
        """file_exists in-flight → resume continues."""
        (self.tmp / "VERSION").write_text("0.8.1\n")
        in_flight_crit = self._crit(
            method="file_exists",
            path="VERSION",
            type="smoke",
            command=None,
        )
        self._write_completed_pair(0, attempt_id="a1")
        self._write_started_only(
            1,
            attempt_id="a1",
            type_="smoke",
            method="file_exists",
            criterion_hash=self.runner._criterion_hash(in_flight_crit),
        )
        criteria = [self._crit(), in_flight_crit, self._crit()]
        verdict, rp = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=criteria,
            contract=self._contract(),
        )
        self.assertEqual(verdict.decision, "auto_rerun")
        self.assertEqual(rp.in_flight_criterion_idx, 1)

    def test_resume_with_in_flight_unsafe_cmd_blocks(self) -> None:
        in_flight_crit = self._crit(command="rm -rf build")
        self._write_started_only(
            0,
            attempt_id="a1",
            type_="unit",
            method="cmd",
            criterion_hash=self.runner._criterion_hash(in_flight_crit),
        )
        criteria = [in_flight_crit]
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=criteria,
            # Empty allowlist — the criterion's `rm` is non-idempotent
            # by default and has no per-criterion rationale.
            contract=self._contract(idempotent_cmd_allowlist=[]),
        )
        self.assertEqual(verdict.decision, "block_in_flight")
        # Reason must point at R8 default block, NOT identity mismatch
        # (otherwise this test wouldn't actually exercise R8).
        self.assertIn("non-idempotent by default", verdict.reason.lower())

    def test_resume_blocks_when_criterion_identity_changed(self) -> None:
        """Codex round-1 [P1]: contract edited mid-attempt — current
        criterion at recorded idx no longer matches the in-flight event's
        recorded ``criterion_hash``. Must block, not auto_rerun, even if
        the new criterion's method would otherwise route to auto_rerun.
        """
        # Started event recorded against an OLD criterion (a cmd).
        old_crit = self._crit(command="rm -rf build", method="cmd")
        self._write_started_only(
            0,
            attempt_id="a1",
            type_="unit",
            method="cmd",
            criterion_hash=self.runner._criterion_hash(old_crit),
        )
        # Contract was edited — at idx 0 there is now a DIFFERENT criterion
        # (file_exists, which would normally auto_rerun cleanly).
        (self.tmp / "VERSION").write_text("0.8.1\n")
        new_crit = self._crit(
            method="file_exists",
            path="VERSION",
            type="smoke",
            command=None,
        )
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=[new_crit],
            contract=self._contract(),
        )
        self.assertEqual(verdict.decision, "block_in_flight")
        self.assertIn("identity changed", verdict.reason.lower())

    def test_cmd_compound_command_blocks_despite_allowlist_match(
        self,
    ) -> None:
        """Codex round-1 [P1]: ``pytest tests; ./deploy.sh`` matches the
        ``pytest`` allowlist prefix but contains ``;``. _run_cmd uses
        shell=True, so re-running would re-fire ``./deploy.sh``. Must
        block; operator opts in via per-criterion override only.
        """
        in_flight_crit = self._crit(command="pytest tests; ./deploy.sh")
        self._write_started_only(
            0,
            attempt_id="a1",
            method="cmd",
            criterion_hash=self.runner._criterion_hash(in_flight_crit),
        )
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=[in_flight_crit],
            contract=self._contract(idempotent_cmd_allowlist=["pytest"]),
        )
        self.assertEqual(verdict.decision, "block_in_flight")
        self.assertIn("shell control characters", verdict.reason.lower())

    def test_cmd_pipe_command_blocks_despite_allowlist_match(self) -> None:
        """Same family as compound — pipe is also a shell control char."""
        in_flight_crit = self._crit(command="pytest tests | tee log.txt")
        self._write_started_only(
            0,
            attempt_id="a1",
            method="cmd",
            criterion_hash=self.runner._criterion_hash(in_flight_crit),
        )
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=[in_flight_crit],
            contract=self._contract(idempotent_cmd_allowlist=["pytest"]),
        )
        self.assertEqual(verdict.decision, "block_in_flight")
        self.assertIn("shell control characters", verdict.reason.lower())

    def test_resume_blocks_when_recorded_hash_missing(self) -> None:
        """Codex round-2 [P2]: fail-closed when the in-flight event has no
        usable ``criterion_hash``. Falsy / non-string hash → block, NOT
        fall-through to resolver (would re-introduce round-1 vulnerability
        if a malformed JSONL line slipped past the schema check).
        """
        in_flight_crit = self._crit(
            method="file_exists", path="VERSION", type="smoke", command=None,
        )
        (self.tmp / "VERSION").write_text("0.8.1\n")
        # Empty hash simulates missing field / older schema.
        self._write_started_only(
            0,
            attempt_id="a1",
            type_="smoke",
            method="file_exists",
            criterion_hash="",
        )
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=[in_flight_crit],
            contract=self._contract(),
        )
        self.assertEqual(verdict.decision, "block_in_flight")
        self.assertIn("lacks a usable criterion_hash", verdict.reason)

    def test_cmd_compound_with_explicit_override_unblocks(self) -> None:
        """Operator opts in: per-criterion override with rationale wins
        over the shell-metachar guard. The override path (4a) bypasses
        the allowlist path (4b) entirely — operator takes responsibility.
        """
        in_flight_crit = self._crit(
            command="pytest tests && echo done",
            idempotent={
                "value": True,
                "rationale": "compound is read-only — verified by author",
                "side_effect_class": "read_only",
            },
        )
        self._write_started_only(
            0,
            attempt_id="a1",
            method="cmd",
            criterion_hash=self.runner._criterion_hash(in_flight_crit),
        )
        verdict, _ = self.runner.resume_attempt(
            self.tmp,
            attempt_id="a1",
            criteria=[in_flight_crit],
            contract=self._contract(idempotent_cmd_allowlist=["pytest"]),
        )
        self.assertEqual(verdict.decision, "auto_rerun")
        self.assertIn("per-criterion override", verdict.reason.lower())


if __name__ == "__main__":
    unittest.main()
