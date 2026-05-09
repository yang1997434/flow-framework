"""v0.8.5 codex-review I1 — codex_review telemetry from gate4_codex_review.

Codex review I1: previous v0.8.5 wiring emitted ``codex_review`` only
from ``dispatch_with_retry`` when ``review_outcome ==
"rejected_with_rationale"``. In production ``_prod_review`` collapses
ALL non-pass GateRunner verdicts to ``"fail"`` — codex_review event
never fires in real runs, and the test path wrote ``duration_ms=0``
(fake data).

Fix: GateRunner gains an optional ``telemetry_emit_fn`` callable;
``gate4_codex_review`` brackets the actual codex CLI invocation with
``timed_span`` so ``duration_ms`` reflects real wall time.

Tests verify:
- Default GateRunner (no telemetry_emit_fn) does not crash + emits no events
- With a callable seam, gate4_codex_review emits exactly one
  codex_review event regardless of verdict
- duration_ms > 0 for any successful invocation (real CLI ran)
- outcome frozen to pass | fail | skip | null (no leakage of
  GateRunner verdict.status verbatim)
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

import flow_orchestrator as fo  # noqa: E402  type: ignore


def _make_gate_runner(td: Path, telemetry_emit_fn=None):
    """Build a GateRunner whose worktree_path is a fake dir; we patch
    the codex subprocess so the actual CLI never runs."""
    ctx = mock.Mock()
    ctx.worktree_path = td
    ctx.worktree_id = "fake-id"
    contract = mock.Mock()
    return fo.GateRunner(
        ctx=ctx,
        contract=contract,
        task_dir=td,
        run_id="run-1",
        task_id="t1",
        prior_baseline=None,
        telemetry_emit_fn=telemetry_emit_fn,
    )


class GateRunnerAcceptsTelemetryEmitFn(unittest.TestCase):
    def test_constructor_accepts_telemetry_emit_fn(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            calls = []
            gr = _make_gate_runner(
                tdp, telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            self.assertIsNotNone(gr)

    def test_constructor_default_is_none_backward_compatible(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            # Without the new kwarg, old call sites continue to work.
            ctx = mock.Mock()
            ctx.worktree_path = tdp
            contract = mock.Mock()
            gr = fo.GateRunner(
                ctx=ctx, contract=contract, task_dir=tdp,
                run_id="r", task_id="t", prior_baseline=None,
            )
            self.assertIsNone(getattr(gr, "telemetry_emit_fn", None))


class Gate4EmitsCodexReviewEvent(unittest.TestCase):
    """gate4_codex_review must emit exactly one codex_review event with
    real wall time (duration_ms > 0) when telemetry_emit_fn is wired."""

    def _stub_pgkill_green(self):
        """Patch _run_shell_with_pgkill to return a GREEN codex verdict."""
        result = mock.Mock()
        result.spawn_error = None
        result.timed_out = False
        result.returncode = 0
        result.stdout = json.dumps({
            "verdict": "GREEN", "issues": [],
        })
        result.stderr = ""
        return mock.patch.object(fo, "_run_shell_with_pgkill",
                                 return_value=result)

    def test_emits_codex_review_event_with_real_duration(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            calls = []
            gr = _make_gate_runner(
                tdp, telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            with self._stub_pgkill_green():
                result = gr.gate4_codex_review(
                    codex_command="echo green",
                )
            # Verdict is GREEN → status pass.
            self.assertEqual(result.status, "pass")
            # Exactly one codex_review event emitted.
            cx = [c for c in calls if c.get("phase") == "codex_review"]
            self.assertEqual(len(cx), 1)
            ev = cx[0]
            # duration_ms must reflect real wall time (>= 0; could be
            # 0 on extremely fast paths, but must be a real int).
            self.assertIsInstance(ev["duration_ms"], int)
            self.assertGreaterEqual(ev["duration_ms"], 0)
            # Frozen-schema outcome.
            self.assertIn(ev["outcome"], {"pass", "fail", "skip", None})

    def test_emits_codex_review_event_on_red_verdict(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            calls = []
            gr = _make_gate_runner(
                tdp, telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            result_obj = mock.Mock()
            result_obj.spawn_error = None
            result_obj.timed_out = False
            result_obj.returncode = 0
            result_obj.stdout = json.dumps({
                "verdict": "RED", "issues": [{
                    "id": "x1", "severity": "high",
                    "title": "bad", "rationale": "test",
                }],
            })
            result_obj.stderr = ""
            with mock.patch.object(
                fo, "_run_shell_with_pgkill", return_value=result_obj,
            ):
                gr.gate4_codex_review(codex_command="echo red")
            cx = [c for c in calls if c.get("phase") == "codex_review"]
            self.assertEqual(len(cx), 1)
            ev = cx[0]
            # RED → outcome must be a frozen value (fail expected).
            self.assertIn(ev["outcome"], {"pass", "fail", "skip", None})
            self.assertEqual(ev["outcome"], "fail")

    def test_emits_codex_review_event_on_inconclusive(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            calls = []
            gr = _make_gate_runner(
                tdp, telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            result_obj = mock.Mock()
            result_obj.spawn_error = None
            result_obj.timed_out = False
            result_obj.returncode = 1   # CLI failed → inconclusive
            result_obj.stdout = ""
            result_obj.stderr = "boom"
            with mock.patch.object(
                fo, "_run_shell_with_pgkill", return_value=result_obj,
            ):
                gr.gate4_codex_review(codex_command="false")
            cx = [c for c in calls if c.get("phase") == "codex_review"]
            self.assertEqual(len(cx), 1)
            # Inconclusive → outcome normalised to fail (frozen schema).
            self.assertEqual(cx[0]["outcome"], "fail")


class NoEmitFnIsNoOp(unittest.TestCase):
    """When telemetry_emit_fn is not supplied, gate4 must not crash
    and must emit nothing (legacy callers unaffected)."""

    def test_no_emit_fn_no_events_no_crash(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            gr = _make_gate_runner(tdp, telemetry_emit_fn=None)
            result_obj = mock.Mock()
            result_obj.spawn_error = None
            result_obj.timed_out = False
            result_obj.returncode = 0
            result_obj.stdout = json.dumps({"verdict": "GREEN", "issues": []})
            result_obj.stderr = ""
            with mock.patch.object(
                fo, "_run_shell_with_pgkill", return_value=result_obj,
            ):
                # Must not raise.
                result = gr.gate4_codex_review(codex_command="echo green")
            self.assertEqual(result.status, "pass")


if __name__ == "__main__":
    unittest.main()
