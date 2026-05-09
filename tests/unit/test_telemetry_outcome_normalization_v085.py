"""v0.8.5 — telemetry outcome normalization (frozen schema enforcement).

Codex review I2: PRD R2 freezes ``outcome`` to ``pass | fail | skip |
null``. The orchestrator was previously emitting ``rejected_with_rationale``
and ``blocked`` (GateRunner verdict.status), violating the frozen
schema and shipping events that downstream readers cannot validate.

Fix: ``emit_event`` normalises any non-frozen value to ``fail`` (the
safest collapse — anything that's not pass / skip / unknown is some
form of fail) and preserves the original string in ``fail_reason_raw``
so information is not lost. Tests pin the mapping table.

Mapping table (v1):
    "pass"                       → "pass"
    "fail" | "blocked"
    | "rejected_with_rationale"
    | "inconclusive"             → "fail"
    "skip" | "skipped"           → "skip"
    None                         → None
    <anything else>              → "fail"  (defensive default + warn)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))

from common import telemetry  # noqa: E402  type: ignore


VALID_OUTCOMES = {"pass", "fail", "skip", None}


class FrozenOutcomeEnumeration(unittest.TestCase):
    """PRD R2: outcome ∈ pass | fail | skip | null. Anything else is a
    schema violation and must be normalised before write."""

    def test_pass_passes_through(self) -> None:
        self.assertEqual(telemetry.normalize_outcome("pass"), "pass")

    def test_fail_passes_through(self) -> None:
        self.assertEqual(telemetry.normalize_outcome("fail"), "fail")

    def test_skip_passes_through(self) -> None:
        self.assertEqual(telemetry.normalize_outcome("skip"), "skip")

    def test_none_passes_through(self) -> None:
        self.assertIsNone(telemetry.normalize_outcome(None))

    def test_blocked_collapses_to_fail(self) -> None:
        self.assertEqual(telemetry.normalize_outcome("blocked"), "fail")

    def test_rejected_with_rationale_collapses_to_fail(self) -> None:
        self.assertEqual(
            telemetry.normalize_outcome("rejected_with_rationale"),
            "fail",
        )

    def test_inconclusive_collapses_to_fail(self) -> None:
        self.assertEqual(
            telemetry.normalize_outcome("inconclusive"),
            "fail",
        )

    def test_unknown_collapses_to_fail(self) -> None:
        self.assertEqual(
            telemetry.normalize_outcome("some_future_status"),
            "fail",
        )


class EmitEventNormalizesAndPreservesRaw(unittest.TestCase):
    """When emit_event receives a non-frozen outcome, it MUST:
    1. write the normalised outcome
    2. preserve the raw string in fail_reason_raw (lossless audit)
    """

    def test_emit_event_writes_normalised_outcome(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="t",
                round_num=1,
                phase="reviewer",
                duration_ms=10,
                outcome="rejected_with_rationale",
                fail_reason_raw=None,
                worktree_id=None,
                enabled=True,
            )
            rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            # MUST be normalised.
            self.assertIn(rec["outcome"], VALID_OUTCOMES)
            self.assertEqual(rec["outcome"], "fail")

    def test_raw_outcome_preserved_when_not_already_in_fail_reason(self) -> None:
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="t",
                round_num=1,
                phase="reviewer",
                duration_ms=10,
                outcome="rejected_with_rationale",
                fail_reason_raw=None,  # would otherwise lose info
                worktree_id=None,
                enabled=True,
            )
            rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            # Audit fidelity: original verbatim string in fail_reason_raw.
            self.assertEqual(
                rec["fail_reason_raw"], "rejected_with_rationale",
            )

    def test_existing_fail_reason_not_clobbered(self) -> None:
        """If caller already set fail_reason_raw, normalisation must
        NOT overwrite it. Append the raw outcome instead so neither is
        lost."""
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="t",
                round_num=1,
                phase="gate_run",
                duration_ms=10,
                outcome="blocked",
                fail_reason_raw="halted_at=gate3_manifest",
                worktree_id=None,
                enabled=True,
            )
            rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(rec["outcome"], "fail")
            # The original fail_reason_raw stays intact.
            self.assertIn("halted_at=gate3_manifest", rec["fail_reason_raw"])
            # And the original outcome string is also captured (so
            # downstream readers can recover the exact verdict).
            self.assertIn("blocked", rec["fail_reason_raw"])

    def test_pass_outcome_does_not_pollute_fail_reason(self) -> None:
        """Happy path: outcome=pass MUST NOT inject any string into
        fail_reason_raw."""
        with TemporaryDirectory() as td:
            path = Path(td) / "telemetry.jsonl"
            telemetry.emit_event(
                path=path,
                task_slug="t",
                round_num=1,
                phase="reviewer",
                duration_ms=10,
                outcome="pass",
                fail_reason_raw=None,
                worktree_id=None,
                enabled=True,
            )
            rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(rec["outcome"], "pass")
            self.assertIsNone(rec["fail_reason_raw"])


if __name__ == "__main__":
    unittest.main()
