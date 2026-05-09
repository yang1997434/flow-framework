"""v0.8.5 — end-to-end telemetry + feedback enrichment smoke tests.

Covers:

- AC1/AC2 — five-phase telemetry events emitted across Round 1 + Round 2:
  worktree_create / implementer / reviewer / gate_run / codex_review.
- AC3 — Round 2 implementer prompt contains the structural diff map
  section AND the framing line.
- AC5 — telemetry/feedback_enrichment switches independent.

Tests use the documented test seam (`deps_factory` on
``_phase2_dispatch``) to inject deterministic fakes — no live git
worktrees, no live subagent dispatch.
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
from common import telemetry  # noqa: E402  type: ignore


def _make_minimal_state(td: Path, telemetry_enabled: bool = True,
                        feedback_enrichment_enabled: bool = True
                        ) -> "fo.RetrySessionState":
    """Build a minimal RetrySessionState pointed at td/telemetry.jsonl."""
    return fo.RetrySessionState(
        task_slug="my-task",
        progress_path=None,
        task_brief="brief text",
        telemetry_path=td / "telemetry.jsonl",
        telemetry_enabled=telemetry_enabled,
        feedback_enrichment_enabled=feedback_enrichment_enabled,
    )


class TelemetryEventsAcrossRounds(unittest.TestCase):
    """AC1 + AC2 — events emitted for each phase across rounds."""

    def test_round_one_emits_implementer_and_reviewer_events(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state = _make_minimal_state(tdp)

            # Fake deps: implementer no-op; reviewer returns "pass" on
            # round 1 (terminating the loop after one full round).
            calls: list[str] = []

            def _impl(*, state, prompt_prefix, **_kw):
                calls.append("impl")
                return {}

            def _rev(*, state, impl_deltas, **_kw):
                calls.append("rev")
                return "pass"

            deps = fo.RetryDeps(
                run_implementer_round=_impl,
                run_codex_review=_rev,
            )
            cfg = fo.RetryConfig(
                max_dispatch_retry_rounds=2,
                max_codex_review_rounds=2,
            )

            from common.afk_monitor import (
                AfkMonitor, now_iso_utc,  # type: ignore
            )
            from common import budget_counter as _bc  # type: ignore
            budget = _bc.make_default_set({})
            afk = AfkMonitor(
                start_iso=now_iso_utc(),
                mode="abort",
            )
            outcome, snap = fo.dispatch_with_retry(
                state=state, config=cfg, budget=budget,
                afk=afk, deps=deps, now_iso_fn=now_iso_utc,
            )
            self.assertEqual(outcome, "pass")
            self.assertEqual(calls, ["impl", "rev"])

            # Read events.
            lines = (tdp / "telemetry.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            events = [json.loads(line) for line in lines]
            phases = [e["phase"] for e in events]
            self.assertIn("implementer", phases)
            self.assertIn("reviewer", phases)
            # Round 1 path: no codex_review (verdict=pass) — that
            # event only fires on rejected_with_rationale.

    def test_fake_deps_path_does_not_emit_codex_review_event(self) -> None:
        """v0.8.5 codex-review I1 invariant: ``codex_review`` events
        are emitted ONLY from inside ``GateRunner.gate4_codex_review``
        (real wall time). When tests inject fake deps that bypass
        GateRunner, no codex_review event is emitted. The previous
        ``dispatch_with_retry``-level emit (``duration_ms=0``
        placeholder) was removed because it shipped fake data. The
        real production-path coverage lives in
        ``test_v085_production_path.py`` (codex review I6)."""
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state = _make_minimal_state(tdp)

            review_iter = iter(["rejected_with_rationale", "pass"])

            def _impl(*, state, prompt_prefix, **_kw):
                return {}

            def _rev(*, state, impl_deltas, **_kw):
                outcome = next(review_iter)
                state.last_reviewer_feedback = "codex says no"
                return outcome

            deps = fo.RetryDeps(
                run_implementer_round=_impl,
                run_codex_review=_rev,
            )
            cfg = fo.RetryConfig(
                max_dispatch_retry_rounds=2,
                max_codex_review_rounds=2,
            )

            from common.afk_monitor import (
                AfkMonitor, now_iso_utc,  # type: ignore
            )
            from common import budget_counter as _bc  # type: ignore
            budget = _bc.make_default_set({})
            afk = AfkMonitor(
                start_iso=now_iso_utc(),
                mode="abort",
            )
            fo.dispatch_with_retry(
                state=state, config=cfg, budget=budget,
                afk=afk, deps=deps, now_iso_fn=now_iso_utc,
            )

            lines = (tdp / "telemetry.jsonl").read_text(
                encoding="utf-8"
            ).splitlines()
            events = [json.loads(line) for line in lines]
            phases = [e["phase"] for e in events]
            # Reviewer event still fires (wraps deps.run_codex_review).
            self.assertIn("reviewer", phases)
            # codex_review event does NOT fire — fake deps bypass
            # GateRunner.gate4_codex_review.
            self.assertNotIn("codex_review", phases)
            # Reviewer outcome normalised to frozen schema.
            rev_events = [e for e in events if e["phase"] == "reviewer"]
            for ev in rev_events:
                self.assertIn(
                    ev["outcome"], {"pass", "fail", "skip", None},
                )


class TelemetryOptOut(unittest.TestCase):
    """AC5 — telemetry switch off → no file."""

    def test_telemetry_off_writes_no_file(self) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state = _make_minimal_state(tdp, telemetry_enabled=False)

            def _impl(**_kw): return {}

            def _rev(*, state, impl_deltas, **_kw): return "pass"

            from common.afk_monitor import (
                AfkMonitor, now_iso_utc,  # type: ignore
            )
            from common import budget_counter as _bc  # type: ignore
            budget = _bc.make_default_set({})
            afk = AfkMonitor(
                start_iso=now_iso_utc(),
                mode="abort",
            )
            fo.dispatch_with_retry(
                state=state,
                config=fo.RetryConfig(),
                budget=budget,
                afk=afk,
                deps=fo.RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_rev,
                ),
                now_iso_fn=now_iso_utc,
            )
            self.assertFalse((tdp / "telemetry.jsonl").exists())


class FeedbackEnrichmentRoundTwo(unittest.TestCase):
    """AC3 — Round 2 prompt contains structural diff map section."""

    def test_round_two_implementer_prompt_includes_diff_map_section(
        self,
    ) -> None:
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state = _make_minimal_state(tdp)
            # Capture the prompts handed to the implementer.
            captured: list[str] = []

            review_iter = iter(["fail", "pass"])

            def _impl(*, state, prompt_prefix, **_kw):
                captured.append(prompt_prefix)
                return {}

            def _rev(*, state, impl_deltas, **_kw):
                state.last_reviewer_feedback = "fix the validation"
                return next(review_iter)

            # Patch _build_prev_round_diff_summary so we don't need a
            # real worktree on disk.
            with mock.patch.object(
                fo, "_build_prev_round_diff_summary",
                side_effect=lambda st: (
                    None if st.dispatch_retry_rounds < 1
                    else (
                        "This is a structural map only; no code content.\n\n"
                        " src/parser.py | 12 +-\n\n"
                        " src/parser.py:\n"
                        "   @@ def normalize_task_name ...\n"
                    )
                ),
            ):
                from common.afk_monitor import (
                    AfkMonitor, now_iso_utc,  # type: ignore
                )
                from common import budget_counter as _bc  # type: ignore
                budget = _bc.make_default_set({})
                afk = AfkMonitor(
                    start_iso=now_iso_utc(),
                    mode="abort",
                )
                fo.dispatch_with_retry(
                    state=state,
                    config=fo.RetryConfig(),
                    budget=budget,
                    afk=afk,
                    deps=fo.RetryDeps(
                        run_implementer_round=_impl,
                        run_codex_review=_rev,
                    ),
                    now_iso_fn=now_iso_utc,
                )

            # Round 1 prompt has no diff map.
            self.assertNotIn(
                "Round N-1 structural diff map", captured[0],
            )
            # Round 2 prompt has the section + framing.
            self.assertIn(
                "Round N-1 structural diff map", captured[1],
            )
            self.assertIn(
                "Use reviewer feedback as the primary signal",
                captured[1],
            )
            self.assertIn("src/parser.py", captured[1])
            self.assertIn(
                "@@ def normalize_task_name", captured[1],
            )

    def test_feedback_enrichment_off_skips_diff_map(self) -> None:
        """AC5 — feedback_enrichment off → no diff map section even on
        Round 2+."""
        with TemporaryDirectory() as td:
            tdp = Path(td)
            state = _make_minimal_state(
                tdp, feedback_enrichment_enabled=False,
            )
            captured: list[str] = []
            review_iter = iter(["fail", "pass"])

            def _impl(*, state, prompt_prefix, **_kw):
                captured.append(prompt_prefix)
                return {}

            def _rev(*, state, impl_deltas, **_kw):
                state.last_reviewer_feedback = "fix it"
                return next(review_iter)

            from common.afk_monitor import (
                AfkMonitor, now_iso_utc,  # type: ignore
            )
            from common import budget_counter as _bc  # type: ignore
            budget = _bc.make_default_set({})
            afk = AfkMonitor(
                start_iso=now_iso_utc(),
                mode="abort",
            )
            fo.dispatch_with_retry(
                state=state,
                config=fo.RetryConfig(),
                budget=budget,
                afk=afk,
                deps=fo.RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_rev,
                ),
                now_iso_fn=now_iso_utc,
            )
            for prompt in captured:
                self.assertNotIn(
                    "Round N-1 structural diff map", prompt,
                )


if __name__ == "__main__":
    unittest.main()
