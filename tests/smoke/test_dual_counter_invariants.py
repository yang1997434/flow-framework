"""T3 — Dual-counter invariants tests (R3.4 + 5 invariants).

Each test maps to one named invariant (PRD §R2 + ADR-1):

1. dispatch_retry_rounds caps implementer-retry loops only.
2. codex_review_rounds is independent of dispatch_retry_rounds.
3. Budget counters cap EVERYTHING (round counters can't outpace them).
4. All terminal paths emit the same HardStopSnapshot shape.
5. No path advances NEITHER counter while looping (= every continue
   advances exactly one).

R3.4: ``rejected_with_rationale`` does NOT consume a retry round.

J-class: invariant 5 is the chained-paper-cut guard. Enumerated.
"""
from __future__ import annotations

import dataclasses
import sys
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


def _make_now_fn(start: datetime, step: float = 1.0):
    state = {"t": start}

    def f() -> str:
        s = _iso(state["t"])
        state["t"] = state["t"] + timedelta(seconds=step)
        return s

    return f


def _make_state(slug: str = "inv-slug") -> RetrySessionState:
    return RetrySessionState(
        task_slug=slug,
        dispatch_retry_rounds=0,
        codex_review_rounds=0,
        progress_path=None,
    )


def _make_afk(start_iso: str) -> AfkMonitor:
    return AfkMonitor(
        start_iso=start_iso, mode="abort",
        idle_seconds_threshold=99_999_999.0,
        hard_cap_seconds=99_999_999.0,
    )


def _scripted(seq):
    rem = list(seq)

    def f(*a, **k):
        if not rem:
            raise AssertionError("scripted callable exhausted")
        return rem.pop(0)

    f.remaining = lambda: len(rem)
    return f


# ----------------------------------------------------------------------
# Invariant 1: dispatch_retry caps implementer loops only
# ----------------------------------------------------------------------

class TestInvariant1(unittest.TestCase):
    def test_invariant_1_dispatch_retry_caps_impl_loops(self):
        # Mix of fail + RWR + pass; only fail should advance retry.
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=5,
                          max_codex_review_rounds=5)
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1, "tokens_out": 1},  # round 1
                {"tokens_in": 1, "tokens_out": 1},  # round 2 (after fail)
                {"tokens_in": 1, "tokens_out": 1},  # round 3 (after pass would not be reached)
            ]),
            run_codex_review=_scripted(["fail", "rejected_with_rationale", "pass"]),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "pass")
        # 1 fail = 1 retry advance; pass adds 0; RWR advances review only.
        self.assertEqual(state.dispatch_retry_rounds, 1)
        self.assertEqual(state.codex_review_rounds, 1)


# ----------------------------------------------------------------------
# Invariant 2: codex_review_rounds is independent
# ----------------------------------------------------------------------

class TestInvariant2(unittest.TestCase):
    def test_invariant_2_codex_review_has_independent_cap(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=10,
                          max_codex_review_rounds=2)
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1, "tokens_out": 1} for _ in range(5)
            ]),
            run_codex_review=_scripted(
                ["rejected_with_rationale", "rejected_with_rationale",
                 "rejected_with_rationale"]
            ),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "review_cap")
        self.assertEqual(state.codex_review_rounds, 2)
        # CRITICAL: dispatch_retry not advanced by RWR
        self.assertEqual(state.dispatch_retry_rounds, 0)


# ----------------------------------------------------------------------
# Invariant 3: budget caps win over round counters
# ----------------------------------------------------------------------

class TestInvariant3(unittest.TestCase):
    def test_invariant_3_budgets_cap_everything_tokens_in(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        budget = bc.make_default_set({**_LIMITS, "tokens_in": 50.0})
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 60, "tokens_out": 1},
            ]),
            run_codex_review=_scripted(["pass"]),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "budget_hit")
        self.assertEqual(snap.counter_name, "tokens_in")

    def test_invariant_3_budgets_cap_everything_cost_usd(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        budget = bc.make_default_set({**_LIMITS, "cost_usd": 1.0})
        # Pre-load cost_usd metadata so first add doesn't blow up.
        budget["cost_usd"].add(
            0.0, model_id="opus-test",
            pricing_version="v1",
        )
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1, "tokens_out": 1, "cost_usd": 5.0,
                 "model_id": "opus-test", "pricing_version": "v1"},
            ]),
            run_codex_review=_scripted(["pass"]),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "budget_hit")
        self.assertEqual(snap.counter_name, "cost_usd")
        # cost_usd snapshots carry pricing metadata in `extra`.
        self.assertEqual(snap.extra.get("model_id"), "opus-test")
        self.assertEqual(snap.extra.get("pricing_version"), "v1")


# ----------------------------------------------------------------------
# Invariant 4: shared snapshot shape across all 4 terminals
# ----------------------------------------------------------------------

class TestInvariant4(unittest.TestCase):
    REQUIRED_FIELDS = {
        "reason", "counter_name", "value", "limit", "hit_at_iso",
        "estimated", "extra", "task_slug", "schema_version",
    }

    def _assert_snapshot_shape(self, snap: HardStopSnapshot):
        d = dataclasses.asdict(snap)
        self.assertEqual(set(d.keys()), self.REQUIRED_FIELDS)
        self.assertEqual(d["schema_version"], "v1")
        self.assertIsInstance(d["hit_at_iso"], str)
        self.assertTrue(d["hit_at_iso"])
        self.assertIsInstance(d["extra"], dict)

    def test_invariant_4_budget_hit_shape(self):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=5,
                          max_codex_review_rounds=5)
        budget = bc.make_default_set({**_LIMITS, "tokens_in": 1.0})
        deps = RetryDeps(
            run_implementer_round=_scripted([{"tokens_in": 99}]),
            run_codex_review=_scripted(["pass"]),
        )
        _, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self._assert_snapshot_shape(snap)
        self.assertEqual(snap.reason, "budget_hit")

    def test_invariant_4_retry_cap_shape(self):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=1,
                          max_codex_review_rounds=99)
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1}, {"tokens_in": 1},
            ]),
            run_codex_review=_scripted(["fail", "fail"]),
        )
        _, snap = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self._assert_snapshot_shape(snap)
        self.assertEqual(snap.reason, "retry_cap")

    def test_invariant_4_codex_review_cap_shape(self):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=1)
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1}, {"tokens_in": 1},
            ]),
            run_codex_review=_scripted([
                "rejected_with_rationale", "rejected_with_rationale",
            ]),
        )
        _, snap = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self._assert_snapshot_shape(snap)
        self.assertEqual(snap.reason, "codex_review_cap")

    def test_invariant_4_afk_hard_cap_shape(self):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        # AFK starts an hour ago with 1s hard cap; first pre-tick at
        # ``start`` is already past the hard cap -> trips immediately.
        prior_iso = _iso(start - timedelta(hours=1))
        afk = AfkMonitor(
            start_iso=prior_iso, mode="abort",
            idle_seconds_threshold=99_999.0,
            hard_cap_seconds=1.0,
        )
        deps = RetryDeps(
            # Should not be called: hard_cap pre-tick terminates first.
            run_implementer_round=_scripted([{"tokens_in": 1}]),
            run_codex_review=_scripted(["pass"]),
        )
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=afk, deps=deps,
            now_iso_fn=_make_now_fn(start, step=1.0),
        )
        self.assertEqual(outcome, "afk_hard_cap")
        self._assert_snapshot_shape(snap)
        # AfkMonitor.to_snapshot uses snapshot.reason="afk_timeout"
        # (the snapshot module's enum); the loop's outcome is
        # "afk_hard_cap" but the SNAPSHOT reason is "afk_timeout".
        self.assertEqual(snap.reason, "afk_timeout")


# ----------------------------------------------------------------------
# Invariant 5: no path leaves both counters static
# ----------------------------------------------------------------------

class TestInvariant5(unittest.TestCase):
    """For each (impl_outcome, review_outcome) pair the loop encounters,
    EXACTLY one counter advances OR the loop terminates. No path
    silently re-enters the loop with both counters static (J-class)."""

    def _run(self, review_seq, expected_outcome,
             expected_retry, expected_review):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=5,
                          max_codex_review_rounds=5)
        deps = RetryDeps(
            run_implementer_round=_scripted(
                [{"tokens_in": 1, "tokens_out": 1}
                 for _ in range(len(review_seq) + 1)]
            ),
            run_codex_review=_scripted(review_seq),
        )
        outcome, _ = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, expected_outcome)
        self.assertEqual(state.dispatch_retry_rounds, expected_retry)
        self.assertEqual(state.codex_review_rounds, expected_review)

    def test_invariant_5_pass_immediate(self):
        # pass on round 1: neither counter advanced (terminated).
        self._run(["pass"], "pass", 0, 0)

    def test_invariant_5_fail_then_pass(self):
        # fail (retry+1) -> pass (terminate).
        self._run(["fail", "pass"], "pass", 1, 0)

    def test_invariant_5_rwr_then_pass(self):
        # RWR (review+1) -> pass (terminate).
        self._run(["rejected_with_rationale", "pass"], "pass", 0, 1)

    def test_invariant_5_alternating_fail_rwr_pass(self):
        # fail (retry+1) -> RWR (review+1) -> pass (terminate).
        # Each non-pass advances exactly one counter; pass terminates.
        self._run(
            ["fail", "rejected_with_rationale", "pass"],
            "pass", 1, 1,
        )

    def test_invariant_5_unknown_review_raises(self):
        # Unknown review outcome must raise — not silently loop with
        # both counters static.
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=3,
                          max_codex_review_rounds=3)
        deps = RetryDeps(
            run_implementer_round=_scripted([{"tokens_in": 1}]),
            run_codex_review=_scripted(["weird_unknown_value"]),
        )
        with self.assertRaises(ValueError):
            dispatch_with_retry(
                state=state, config=cfg,
                budget=bc.make_default_set(_LIMITS),
                afk=_make_afk(start_iso), deps=deps,
                now_iso_fn=_make_now_fn(start),
            )


# ----------------------------------------------------------------------
# R3.4: RWR must not consume retry round
# ----------------------------------------------------------------------

class TestR34RwrDoesNotConsumeRetry(unittest.TestCase):
    def test_R34_rwr_does_not_consume_retry(self):
        start = datetime(2026, 5, 8, tzinfo=timezone.utc)
        start_iso = _iso(start)
        state = _make_state()
        cfg = RetryConfig(max_dispatch_retry_rounds=3,
                          max_codex_review_rounds=3)
        deps = RetryDeps(
            run_implementer_round=_scripted([
                {"tokens_in": 1, "tokens_out": 1},
                {"tokens_in": 1, "tokens_out": 1},
            ]),
            run_codex_review=_scripted(
                ["rejected_with_rationale", "pass"]
            ),
        )
        outcome, _ = dispatch_with_retry(
            state=state, config=cfg, budget=bc.make_default_set(_LIMITS),
            afk=_make_afk(start_iso), deps=deps,
            now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "pass")
        self.assertEqual(state.dispatch_retry_rounds, 0)
        self.assertEqual(state.codex_review_rounds, 1)


if __name__ == "__main__":
    unittest.main()
