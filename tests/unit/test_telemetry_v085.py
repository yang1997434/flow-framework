"""v0.8.5 — telemetry module unit tests.

Covers:
- ``schema_version`` constant frozen at 1
- ``PHASES`` constant matches PRD R3 (worktree_create / implementer /
  reviewer / gate_run / codex_review)
- ``emit_event`` writes a single JSONL line with the v1 schema fields
- ``emit_event`` opt-out (telemetry_enabled=False) skips file creation
- ``emit_event`` swallows write failures (parent-dir-missing-and-uncreatable
  case), increments a process counter, and never propagates the exception
- Multiple events appended to the same file produce N lines

PRD: ``.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md``
§R1, §R2, §R5.
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))

from common import telemetry  # noqa: E402  type: ignore


class SchemaConstantsFrozen(unittest.TestCase):
    def test_schema_version_is_1(self) -> None:
        self.assertEqual(telemetry.SCHEMA_VERSION, 1)

    def test_phases_match_prd_r3(self) -> None:
        # Order is documented but not load-bearing; the SET is what
        # dispatch must cover.
        self.assertEqual(
            set(telemetry.PHASES),
            {
                "worktree_create",
                "implementer",
                "reviewer",
                "gate_run",
                "codex_review",
            },
        )

    def test_event_field_set_frozen(self) -> None:
        # Field set is the v1 contract; tests must trip if anyone adds /
        # removes a field silently.
        self.assertEqual(
            set(telemetry.EVENT_FIELDS),
            {
                "ts",
                "schema_version",
                "task_slug",
                "round_num",
                "phase",
                "duration_ms",
                "outcome",
                "fail_reason_raw",
                "fail_category",
                "worktree_id",
            },
        )


class EmitEventWritesJsonlLine(unittest.TestCase):
    def test_emit_event_appends_one_line_with_v1_schema(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="my-task",
                round_num=1,
                phase="implementer",
                duration_ms=1234,
                outcome="pass",
                fail_reason_raw=None,
                worktree_id="my-task+t0+abc1234",
                enabled=True,
            )
            self.assertTrue(path.is_file())
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            # Field set must be exact.
            self.assertEqual(set(rec.keys()), set(telemetry.EVENT_FIELDS))
            self.assertEqual(rec["schema_version"], 1)
            self.assertEqual(rec["task_slug"], "my-task")
            self.assertEqual(rec["round_num"], 1)
            self.assertEqual(rec["phase"], "implementer")
            self.assertEqual(rec["duration_ms"], 1234)
            self.assertEqual(rec["outcome"], "pass")
            self.assertIsNone(rec["fail_reason_raw"])
            # PRD R2: fail_category reserved; v0.8.5 always null.
            self.assertIsNone(rec["fail_category"])
            self.assertEqual(rec["worktree_id"], "my-task+t0+abc1234")
            # ts is ISO 8601 UTC (Z suffix).
            self.assertIsInstance(rec["ts"], str)
            self.assertTrue(rec["ts"].endswith("Z") or "+" in rec["ts"])

    def test_emit_event_appends_n_records(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            for i in range(3):
                telemetry.emit_event(
                    path=path,
                    task_slug="t",
                    round_num=i + 1,
                    phase="reviewer",
                    duration_ms=10 * (i + 1),
                    outcome="fail",
                    fail_reason_raw=f"reason {i}",
                    worktree_id=None,
                    enabled=True,
                )
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)


class EmitEventOptOut(unittest.TestCase):
    def test_disabled_skips_file_creation(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="t",
                round_num=1,
                phase="reviewer",
                duration_ms=1,
                outcome="pass",
                fail_reason_raw=None,
                worktree_id=None,
                enabled=False,
            )
            # PRD R5: off → file not created.
            self.assertFalse(path.exists())


class EmitEventSwallowsFailure(unittest.TestCase):
    def test_write_failure_does_not_raise_and_increments_counter(self) -> None:
        # Force append_jsonl_locked to return False → swallow path.
        before = telemetry.swallow_count()
        with mock.patch.object(
            telemetry,
            "_append_jsonl_locked",
            return_value=False,
        ):
            with TemporaryDirectory() as td:
                path = Path(td) / "telemetry.jsonl"
                # MUST NOT raise.
                telemetry.emit_event(
                    path=path,
                    task_slug="t",
                    round_num=1,
                    phase="reviewer",
                    duration_ms=1,
                    outcome="pass",
                    fail_reason_raw=None,
                    worktree_id=None,
                    enabled=True,
                )
        after = telemetry.swallow_count()
        # Counter incremented monotonically by exactly one.
        self.assertEqual(after - before, 1)

    def test_write_failure_via_oserror_swallowed(self) -> None:
        before = telemetry.swallow_count()
        with mock.patch.object(
            telemetry,
            "_append_jsonl_locked",
            side_effect=OSError("disk full"),
        ):
            with TemporaryDirectory() as td:
                path = Path(td) / "telemetry.jsonl"
                # MUST NOT raise.
                telemetry.emit_event(
                    path=path,
                    task_slug="t",
                    round_num=1,
                    phase="reviewer",
                    duration_ms=1,
                    outcome="pass",
                    fail_reason_raw=None,
                    worktree_id=None,
                    enabled=True,
                )
        after = telemetry.swallow_count()
        self.assertEqual(after - before, 1)


class TimedSpanContextManager(unittest.TestCase):
    def test_timed_span_emits_event_on_exit(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            with telemetry.timed_span(
                path=path,
                task_slug="t",
                round_num=1,
                phase="implementer",
                worktree_id="w-id",
                enabled=True,
            ) as span:
                span.set_outcome("pass")
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["phase"], "implementer")
            self.assertEqual(rec["outcome"], "pass")
            self.assertGreaterEqual(rec["duration_ms"], 0)

    def test_timed_span_emits_event_even_on_exception(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            with self.assertRaises(RuntimeError):
                with telemetry.timed_span(
                    path=path,
                    task_slug="t",
                    round_num=1,
                    phase="implementer",
                    worktree_id=None,
                    enabled=True,
                ) as span:
                    span.set_outcome("fail")
                    span.set_fail_reason("boom")
                    raise RuntimeError("boom")
            # Telemetry must still land even though exception propagated.
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            rec = json.loads(lines[0])
            self.assertEqual(rec["outcome"], "fail")
            self.assertEqual(rec["fail_reason_raw"], "boom")

    def test_timed_span_disabled_writes_nothing(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            with telemetry.timed_span(
                path=path,
                task_slug="t",
                round_num=1,
                phase="implementer",
                worktree_id=None,
                enabled=False,
            ) as span:
                span.set_outcome("pass")
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
