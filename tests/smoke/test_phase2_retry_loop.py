"""T3 — Phase 2 retry-on-non-pass loop tests.

Covers Acceptance R3.1, R3.2, R3.3 from the v0.8.2 PRD:

- R3.1: integration — first round fail -> second round pass.
- R3.2: budget cap mid-loop — terminates immediately with budget_hit
  snapshot; does NOT start another round.
- R3.3: round caps — max_dispatch_retry_rounds=3 + 4 fail reviews ->
  retry_cap after 3rd round; max_codex_review_rounds=2 + 3 RWR ->
  review_cap after 2nd review.

D-class: refactor preserves fail-fast semantics behind round-cap gate.
B-class: every state-machine transition exercised via injected fakes.
J-class: Invariant 5 enforced (every iteration advances exactly one
counter or terminates) — see test_dual_counter_invariants.py.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common import budget_counter as bc  # noqa: E402  type: ignore
from common.afk_monitor import AfkMonitor  # noqa: E402  type: ignore
from common.snapshot import HardStopSnapshot  # noqa: E402  type: ignore
from flow_orchestrator import (  # noqa: E402  type: ignore
    RetryConfig,
    RetryDeps,
    RetrySessionState,
    dispatch_with_retry,
    redact_blindspot_index,
)


_LIMITS = {
    "tokens_in": 1_000_000.0,
    "tokens_out": 1_000_000.0,
    "cost_usd": 1000.0,
    "active_wallclock_minutes": 600.0,
    "subagent_dispatches": 100.0,
}


def _iso(dt: datetime) -> str:
    raw = dt.astimezone(timezone.utc).isoformat()
    if raw.endswith("+00:00"):
        return raw[:-6] + "Z"
    return raw


def _make_now_fn(start: datetime, step_seconds: float = 1.0):
    """Deterministic now-iso source. Each call advances by step_seconds."""
    state = {"t": start}

    def f() -> str:
        s = _iso(state["t"])
        state["t"] = state["t"] + timedelta(seconds=step_seconds)
        return s

    return f


def _make_state(start_iso: str, task_slug: str = "test-slug") -> RetrySessionState:
    return RetrySessionState(
        task_slug=task_slug,
        dispatch_retry_rounds=0,
        codex_review_rounds=0,
        progress_path=None,
    )


def _make_budget() -> dict:
    return bc.make_default_set(_LIMITS)


def _make_afk(start_iso: str) -> AfkMonitor:
    # Big thresholds so AFK never fires in retry-loop tests.
    return AfkMonitor(
        start_iso=start_iso,
        mode="abort",
        idle_seconds_threshold=99_999_999.0,
        hard_cap_seconds=99_999_999.0,
    )


def _scripted_impl(outcomes: list):
    """Return a callable yielding pre-scripted impl outcomes per call."""
    seq = list(outcomes)

    def f(*args, **kwargs):
        if not seq:
            raise AssertionError("impl called more times than scripted")
        return seq.pop(0)

    f.remaining = lambda: len(seq)
    return f


def _scripted_review(outcomes: list):
    seq = list(outcomes)

    def f(*args, **kwargs):
        if not seq:
            raise AssertionError("review called more times than scripted")
        return seq.pop(0)

    f.remaining = lambda: len(seq)
    return f


class TestR31FailThenPass(unittest.TestCase):
    """R3.1: round 1 fail review -> round 2 pass."""

    def test_first_round_fail_second_round_pass(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state(start_iso)
        with tempfile.TemporaryDirectory() as td:
            progress = Path(td) / "progress.md"
            progress.write_text(
                "# progress\n\n## Execute Log\n\n| round | role | counters |\n|---|---|---|\n",
                encoding="utf-8",
            )
            state.progress_path = progress
            cfg = RetryConfig(
                max_dispatch_retry_rounds=3, max_codex_review_rounds=2,
            )
            deps = RetryDeps(
                run_implementer_round=_scripted_impl([
                    {"tokens_in": 100, "tokens_out": 50},
                    {"tokens_in": 80, "tokens_out": 40},
                ]),
                run_codex_review=_scripted_review(["fail", "pass"]),
            )
            outcome, snap = dispatch_with_retry(
                state=state, config=cfg, budget=_make_budget(),
                afk=_make_afk(start_iso), deps=deps,
                now_iso_fn=_make_now_fn(start),
            )
            self.assertEqual(outcome, "pass")
            self.assertIsNone(snap)
            self.assertEqual(state.dispatch_retry_rounds, 1)
            self.assertEqual(state.codex_review_rounds, 0)
            # progress.md has the original 4 lines + 2 round rows
            content = progress.read_text(encoding="utf-8")
            self.assertIn("| 1 |", content)
            self.assertIn("| 2 |", content)


class TestR32BudgetCapMidLoop(unittest.TestCase):
    """R3.2: budget hit mid-loop -> terminate immediately."""

    def test_tokens_in_exceeded_round_one_terminates(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state(start_iso)
        cfg = RetryConfig(
            max_dispatch_retry_rounds=5, max_codex_review_rounds=5,
        )
        # tokens_in cap = 100; impl burns 200 in round 1.
        budget = bc.make_default_set({**_LIMITS, "tokens_in": 100.0})
        deps = RetryDeps(
            run_implementer_round=_scripted_impl([
                {"tokens_in": 200, "tokens_out": 0},  # burns over cap
                {"tokens_in": 0, "tokens_out": 0},  # NEVER called
            ]),
            run_codex_review=_scripted_review(["fail", "pass"]),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "budget_hit")
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.reason, "budget_hit")
        self.assertEqual(snap.counter_name, "tokens_in")
        self.assertEqual(snap.limit, 100.0)
        # Should NOT have run round 2: impl seq still has 1 left.
        self.assertEqual(deps.run_implementer_round.remaining(), 1)


class TestR33RoundCaps(unittest.TestCase):
    """R3.3: each round cap terminates after Nth round, not (N+1)."""

    def test_max_retry_three_with_four_fails_terminates_at_third(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state(start_iso)
        cfg = RetryConfig(
            max_dispatch_retry_rounds=3, max_codex_review_rounds=10,
        )
        deps = RetryDeps(
            run_implementer_round=_scripted_impl([
                {"tokens_in": 1, "tokens_out": 1} for _ in range(5)
            ]),
            run_codex_review=_scripted_review(["fail"] * 5),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=_make_budget(),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "retry_cap")
        self.assertEqual(state.dispatch_retry_rounds, 3)
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.reason, "retry_cap")
        self.assertEqual(snap.extra.get("max"), 3)

    def test_max_review_two_with_three_rwr_terminates_at_second(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state(start_iso)
        cfg = RetryConfig(
            max_dispatch_retry_rounds=10, max_codex_review_rounds=2,
        )
        deps = RetryDeps(
            run_implementer_round=_scripted_impl([
                {"tokens_in": 1, "tokens_out": 1} for _ in range(5)
            ]),
            run_codex_review=_scripted_review(
                ["rejected_with_rationale"] * 5
            ),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=_make_budget(),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "review_cap")
        self.assertEqual(state.codex_review_rounds, 2)
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.reason, "codex_review_cap")
        self.assertEqual(snap.extra.get("max"), 2)


class TestRetryConfigValidation(unittest.TestCase):
    def test_zero_retry_cap_rejected(self):
        with self.assertRaises(ValueError):
            RetryConfig(max_dispatch_retry_rounds=0,
                        max_codex_review_rounds=2)

    def test_negative_review_cap_rejected(self):
        with self.assertRaises(ValueError):
            RetryConfig(max_dispatch_retry_rounds=2,
                        max_codex_review_rounds=-1)


class TestReviewerTransparencyRedaction(unittest.TestCase):
    """Reviewer feedback transparency rule (R3 PRD): strip 18-class
    blindspot trigger lines but preserve specific findings."""

    def test_strips_letter_dot_class_headers(self):
        feedback = (
            "A. State machine missing pause/resume\n"
            "Specific finding: line 42 forgets to release lock\n"
            "Class B: control flow drift\n"
            "Specific finding: subprocess output not validated\n"
            "[J] chained paper-cut\n"
        )
        out = redact_blindspot_index(feedback)
        self.assertNotIn("A. State machine", out)
        self.assertNotIn("Class B: control", out)
        self.assertNotIn("[J] chained", out)
        self.assertIn("line 42 forgets to release lock", out)
        self.assertIn("subprocess output not validated", out)

    def test_preserves_text_without_class_headers(self):
        feedback = "All good. Specific finding: typo on line 7.\n"
        self.assertEqual(redact_blindspot_index(feedback), feedback)


if __name__ == "__main__":
    unittest.main()
