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
from common.exit_codes import PARKED_RECOVERABLE  # noqa: E402  type: ignore
from common.snapshot import HardStopSnapshot  # noqa: E402  type: ignore
from flow_orchestrator import (  # noqa: E402  type: ignore
    Contract,
    RetryConfig,
    RetryDeps,
    RetrySessionState,
    _phase2_dispatch,
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

    # -- T6.1 P2.1: extended variants (em-dash / colon / paren / Class-em) --
    # The dispatch_template reviewer prompt uses the em-dash format
    # (e.g. "A — Python falsy/truthy traps"); the redactor MUST cover
    # that case or the trigger checklist leaks back into implementer
    # prompts on every retry.

    def test_redact_em_dash_class_letter(self):
        feedback = "A — schema parsing\nReal finding: foo\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("schema parsing", out)
        self.assertIn("Real finding: foo", out)

    def test_redact_colon_class_letter(self):
        feedback = "A: state machine\nReal finding: bar\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("state machine", out)
        self.assertIn("Real finding: bar", out)

    def test_redact_paren_class_letter(self):
        feedback = "A) trust boundary\nReal finding: baz\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("trust boundary", out)
        self.assertIn("Real finding: baz", out)

    def test_redact_class_word_em_dash(self):
        feedback = "Class A — schema bug\nReal finding: qux\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("schema bug", out)
        self.assertIn("Real finding: qux", out)

    def test_redact_class_word_hyphen(self):
        feedback = "Class B - control flow drift\nReal finding: x\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("control flow drift", out)
        self.assertIn("Real finding: x", out)

    def test_redact_class_word_paren(self):
        feedback = "Class C) ordering\nReal finding: y\n"
        out = redact_blindspot_index(feedback)
        self.assertNotIn("ordering", out)
        self.assertIn("Real finding: y", out)

    def test_redact_keeps_unrelated_lowercase(self):
        # "a function named foo" is not an anchored class header — must
        # NOT be stripped. (Lowercase 'a' is a regular English word.)
        feedback = "a function named foo\nb section header\n"
        out = redact_blindspot_index(feedback)
        self.assertIn("a function named foo", out)
        self.assertIn("b section header", out)


class TestCmdAutoExecuteUsesRetryLoop(unittest.TestCase):
    """T3.1 wire-up: production `_cmd_auto_execute` (via the extracted
    `_phase2_dispatch` helper) flows through `dispatch_with_retry`.

    D-class: legacy fail-fast `GateRunner.run_phase2` is no longer
    reachable from the production entrypoint when `_phase2_dispatch`
    is in use — the prod adapter calls `gate_runner.run_phase2` only
    inside the retry-loop's review callback, NOT directly.

    Strategy: drive `_phase2_dispatch` with a fake `deps_factory` that
    spies impl/review calls. Asserts the retry loop iterated (impl
    called twice across a fail→pass scripted review).
    """

    def _make_contract(self) -> Contract:
        # Minimal contract: huge budgets so the loop never trips, no
        # AFK pressure (default afk_timeout_min=None -> default
        # 1800s threshold; we won't accumulate that in the test).
        return Contract(
            contract_schema_version=1,
            autonomy_mode="full",
            created_at="2026-05-08T00:00:00Z",
            budget={
                "tokens_in": 1_000_000.0,
                "tokens_out": 1_000_000.0,
                "cost_usd": 1000.0,
                "active_wallclock_minutes": 600.0,
                "subagent_dispatches": 100.0,
            },
        )

    def test_phase2_dispatch_routes_through_retry_loop(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n",
                encoding="utf-8",
            )

            # Spy: track every call to impl / review and assert no
            # direct GateRunner.run_phase2 invocation occurred.
            impl_calls: list = []
            review_outcomes = ["fail", "pass"]
            review_calls: list = []
            run_phase2_invocations: list = []

            def _fake_deps_factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    impl_calls.append({
                        "round": state.dispatch_retry_rounds,
                        "prefix": prompt_prefix,
                    })
                    return {}

                def _review(*, state, impl_deltas, **__):
                    review_calls.append(state.dispatch_retry_rounds)
                    return review_outcomes.pop(0)

                return RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                )

            # Sentinel notifier — never asked to fire on a "pass" path.
            class _SpyNotifier:
                def __init__(self):
                    self.fired: list = []

                def fire_block(self, **kw):
                    self.fired.append(kw)

            notifier = _SpyNotifier()
            contract = self._make_contract()
            # Patch GateRunner.run_phase2 globally so any accidental
            # direct call (D-class regression) is detected loudly.
            import flow_orchestrator as fo
            orig_run_phase2 = fo.GateRunner.run_phase2

            def _trapped_run_phase2(self, *a, **kw):
                run_phase2_invocations.append((a, kw))
                raise AssertionError(
                    "legacy GateRunner.run_phase2 reached from "
                    "_phase2_dispatch — retry-loop wire-up regression"
                )

            fo.GateRunner.run_phase2 = _trapped_run_phase2  # type: ignore
            try:
                rc = _phase2_dispatch(
                    slug="t3-1-wireup",
                    task_dir=task_dir,
                    contract=contract,
                    manifest=object(),
                    facts=object(),
                    ctx=object(),
                    criteria=[],
                    gate_cmds={
                        "baseline": "true",
                        "codex": "true",
                        "smoke": "true",
                    },
                    run_id="run-1",
                    task_id="task-1",
                    notifier=notifier,
                    deps_factory=_fake_deps_factory,
                )
            finally:
                fo.GateRunner.run_phase2 = orig_run_phase2  # type: ignore

            self.assertEqual(rc, 0, "fail-then-pass should land at rc=0")
            # Retry loop ran impl twice (round 1 fail -> round 2 pass).
            self.assertEqual(
                len(impl_calls), 2,
                f"expected 2 impl rounds, got {len(impl_calls)}",
            )
            self.assertEqual(impl_calls[0]["round"], 0)
            self.assertEqual(impl_calls[1]["round"], 1)
            # Review called twice mirroring impl rounds.
            self.assertEqual(len(review_calls), 2)
            # No legacy fail-fast direct invocation.
            self.assertEqual(run_phase2_invocations, [])
            # No block fired on the pass terminal.
            self.assertEqual(notifier.fired, [])

    def test_phase2_dispatch_terminal_writes_snapshot_and_blocks(self):
        """Non-pass terminal (retry_cap) -> snapshot file written +
        notifier.fire_block called -> rc=3."""
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n",
                encoding="utf-8",
            )

            # Always-fail review forces retry-cap exhaustion.
            def _fake_deps_factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    del state, prompt_prefix
                    return {}

                def _review(*, state, impl_deltas, **__):
                    del state, impl_deltas
                    return "fail"

                return RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                )

            class _SpyNotifier:
                def __init__(self):
                    self.fired: list = []

                def fire_block(self, **kw):
                    self.fired.append(kw)

            notifier = _SpyNotifier()
            contract = self._make_contract()

            rc = _phase2_dispatch(
                slug="t3-1-terminal",
                task_dir=task_dir,
                contract=contract,
                manifest=object(),
                facts=object(),
                ctx=object(),
                criteria=[],
                gate_cmds={
                    "baseline": "true",
                    "codex": "true",
                    "smoke": "true",
                },
                run_id="run-2",
                task_id="task-2",
                notifier=notifier,
                deps_factory=_fake_deps_factory,
            )

            self.assertEqual(rc, 3)
            self.assertEqual(len(notifier.fired), 1)
            self.assertEqual(
                notifier.fired[0]["block_type"], "phase2_retry_cap"
            )
            # Snapshot persisted to stable path.
            snap_path = task_dir / "hard-stop.json"
            self.assertTrue(
                snap_path.exists(),
                f"expected hard-stop.json at {snap_path}",
            )
            # Round-trip via the snapshot reader (verifies G-class
            # atomic write produced a valid v1-schema payload).
            from common.snapshot import read as _read_snap
            snap = _read_snap(snap_path)
            self.assertEqual(snap.reason, "retry_cap")
            self.assertIsInstance(snap, HardStopSnapshot)


# ----------------------------------------------------------------------
# T6.1 P1.1 — wait-mode AFK timeout returns afk_idle_park (recoverable),
# does NOT keep dispatching as a fall-through.
# ----------------------------------------------------------------------

class TestT61WaitModeIdlePark(unittest.TestCase):
    """In `wait` mode, an idle timeout (without 24h hard cap) should
    park: outcome="afk_idle_park", snap=None, NO further impl/review
    rounds dispatched."""

    def test_wait_mode_timeout_returns_park_no_snapshot(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        # AfkMonitor: wait mode, idle threshold 60s, hard cap 99999s.
        afk = AfkMonitor(
            start_iso=start_iso,
            mode="wait",
            idle_seconds_threshold=60.0,
            hard_cap_seconds=99_999.0,
        )
        # now_iso advances by 120 s on each call → first pre-tick is
        # already 120 s past last_activity_iso (start_iso) → timeout.
        # But wait mode → to_snapshot None → must park, NOT loop.
        now_fn = _make_now_fn(start + timedelta(seconds=120), step_seconds=120.0)
        impl_calls = []
        review_calls = []

        def _impl(*, state, prompt_prefix, **__):
            impl_calls.append(state.dispatch_retry_rounds)
            return {}

        def _review(*, state, impl_deltas, **__):
            review_calls.append(state.dispatch_retry_rounds)
            return "fail"

        deps = RetryDeps(run_implementer_round=_impl, run_codex_review=_review)
        state = _make_state(start_iso)
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=_make_budget(),
            afk=afk, deps=deps, now_iso_fn=now_fn,
        )
        self.assertEqual(outcome, "afk_idle_park")
        self.assertIsNone(snap)
        # Critical: no impl / review rounds were started.
        self.assertEqual(impl_calls, [])
        self.assertEqual(review_calls, [])

    def test_abort_mode_timeout_returns_afk_aborted_with_snapshot(self):
        """In `abort` mode, idle timeout produces a HardStopSnapshot
        and outcome="afk_aborted" (terminal, distinct from hard_cap)."""
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        afk = AfkMonitor(
            start_iso=start_iso,
            mode="abort",
            idle_seconds_threshold=60.0,
            hard_cap_seconds=99_999.0,
        )
        now_fn = _make_now_fn(start + timedelta(seconds=120), step_seconds=120.0)

        def _impl(*, state, prompt_prefix, **__):
            raise AssertionError("impl should not run after abort timeout")

        def _review(*, state, impl_deltas, **__):
            raise AssertionError("review should not run after abort timeout")

        deps = RetryDeps(run_implementer_round=_impl, run_codex_review=_review)
        state = _make_state(start_iso)
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=_make_budget(),
            afk=afk, deps=deps, now_iso_fn=now_fn,
        )
        self.assertEqual(outcome, "afk_aborted")
        self.assertIsInstance(snap, HardStopSnapshot)
        # Snapshot reason is the schema-frozen "afk_timeout".
        self.assertEqual(snap.reason, "afk_timeout")


# ----------------------------------------------------------------------
# T6.1 P1.2 — active_wallclock_minutes ticks from PausedClock at the top
# of each loop iteration so the budget actually trips on long runs.
# ----------------------------------------------------------------------

class TestT61WallclockBudgetTicks(unittest.TestCase):
    def test_active_wallclock_budget_ticks_and_trips(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        # AFK: wide thresholds — never the cause of termination here.
        afk = AfkMonitor(
            start_iso=start_iso,
            mode="abort",
            idle_seconds_threshold=99_999_999.0,
            hard_cap_seconds=99_999_999.0,
        )
        # 30 minutes per iteration; limit = 20 minutes → trip on round 1.
        now_fn = _make_now_fn(start + timedelta(minutes=30), step_seconds=1800.0)
        budget = bc.make_default_set({**_LIMITS, "active_wallclock_minutes": 20.0})
        # Always-fail review with infinite caps so only budget can trip.
        deps = RetryDeps(
            run_implementer_round=_scripted_impl([
                {"tokens_in": 1} for _ in range(20)
            ]),
            run_codex_review=_scripted_review(["fail"] * 20),
        )
        state = _make_state(start_iso)
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=afk, deps=deps, now_iso_fn=now_fn,
        )
        self.assertEqual(outcome, "budget_hit")
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.counter_name, "active_wallclock_minutes")
        self.assertEqual(snap.limit, 20.0)


# ----------------------------------------------------------------------
# T6.1 P1.3 — retry_cap snapshot preserves last halted gate + details so
# operator can triage WHICH gate failed (smoke / codex / baseline) and
# WHY (failed test name / reviewer rationale).
# ----------------------------------------------------------------------

class TestT61LastHaltedGateInSnapshot(unittest.TestCase):
    def test_retry_cap_snapshot_preserves_last_halted_gate(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        afk = _make_afk(start_iso)
        cfg = RetryConfig(max_dispatch_retry_rounds=2,
                          max_codex_review_rounds=99)

        def _impl(*, state, prompt_prefix, **__):
            return {}

        def _review(*, state, impl_deltas, **__):
            # Simulate the prod adapter setting gate context after a fail.
            state.last_halted_at_gate = "smoke"
            state.last_gate_details = {"failed_test": "test_foo"}
            state.last_reviewer_feedback = "phase 2 halted at smoke"
            return "fail"

        deps = RetryDeps(run_implementer_round=_impl, run_codex_review=_review)
        state = _make_state(start_iso)
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=_make_budget(),
            afk=afk, deps=deps, now_iso_fn=_make_now_fn(start),
        )
        self.assertEqual(outcome, "retry_cap")
        self.assertIsNotNone(snap)
        self.assertEqual(snap.extra.get("last_halted_at_gate"), "smoke")
        self.assertEqual(
            snap.extra.get("last_gate_details"),
            {"failed_test": "test_foo"},
        )

    def test_retry_cap_notifier_message_includes_gate(self):
        """The fire_block message MUST mention the last halted gate so
        operator triage doesn't lose the context (D-class regression)."""
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n", encoding="utf-8",
            )

            def _fake_deps_factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    return {}

                def _review(*, state, impl_deltas, **__):
                    state.last_halted_at_gate = "codex"
                    state.last_gate_details = {"reviewer": "rejected"}
                    state.last_reviewer_feedback = "phase 2 halted at codex"
                    return "fail"

                return RetryDeps(run_implementer_round=_impl,
                                 run_codex_review=_review)

            class _SpyNotifier:
                def __init__(self):
                    self.fired = []

                def fire_block(self, **kw):
                    self.fired.append(kw)

            notifier = _SpyNotifier()
            from flow_orchestrator import _phase2_dispatch as _disp
            contract = TestCmdAutoExecuteUsesRetryLoop()._make_contract()
            rc = _disp(
                slug="t61-gate-context",
                task_dir=task_dir,
                contract=contract,
                manifest=object(),
                facts=object(),
                ctx=object(),
                criteria=[],
                gate_cmds={"baseline": "true", "codex": "true", "smoke": "true"},
                run_id="run-x",
                task_id="task-x",
                notifier=notifier,
                deps_factory=_fake_deps_factory,
            )
            self.assertEqual(rc, 3)
            self.assertEqual(len(notifier.fired), 1)
            why = notifier.fired[0].get("why_blocked", "")
            self.assertIn("codex", why,
                          f"expected gate name in why_blocked, got {why!r}")


# ----------------------------------------------------------------------
# T8.2.1 P1.1 (was T6.2 in v0.8.2 with rc=2; v0.8.2.1 corrected to
# rc=5 = PARKED_RECOVERABLE) — afk_idle_park returns rc=5 (distinct
# from rc=0 pass and rc=3 terminal-with-snapshot). _cmd_auto_execute
# must NOT proceed to gate-7 merge when Phase 2 parked. Park-becomes-
# merge would silently merge partial work — semantic regression of T6.1.
# ----------------------------------------------------------------------

class TestT821Phase2DispatchParkReturnsRc5(unittest.TestCase):
    """T8.2.1 P1.1 (legacy v0.8.2 T6.2; corrected to rc=5 in v0.8.2.1):
    rc mapping in `_phase2_dispatch`:
        pass            -> 0
        afk_idle_park   -> 5  (RECOVERABLE; no snapshot, no notifier)
        terminal-w-snap -> 3  (snapshot persisted + notifier fired)
    """

    def _make_contract(self) -> Contract:
        return Contract(
            contract_schema_version=1,
            autonomy_mode="full",
            created_at="2026-05-08T00:00:00Z",
            budget={
                "tokens_in": 1_000_000.0,
                "tokens_out": 1_000_000.0,
                "cost_usd": 1000.0,
                "active_wallclock_minutes": 600.0,
                "subagent_dispatches": 100.0,
            },
        )

    def test_phase2_dispatch_park_returns_rc5_no_merge(self):
        """wait-mode AFK timeout in `_phase2_dispatch` -> rc=5
        (PARKED_RECOVERABLE). No hard-stop.json on disk. Notifier MUST
        NOT have been fired."""
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n", encoding="utf-8",
            )

            class _SpyNotifier:
                def __init__(self):
                    self.fired: list = []

                def fire_block(self, **kw):
                    self.fired.append(kw)

            notifier = _SpyNotifier()

            # Inject a deps_factory whose impl never gets called because
            # the wait-mode AFK timeout fires on the very first pre-tick.
            def _deps_factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    raise AssertionError("impl must not run on park")

                def _review(*, state, impl_deltas, **__):
                    raise AssertionError("review must not run on park")

                return RetryDeps(run_implementer_round=_impl,
                                 run_codex_review=_review)

            # Patch _resolve_afk_monitor to return a wait-mode AFK that
            # times out on the very first evaluate() call.
            import flow_orchestrator as fo
            from common.afk_monitor import AfkMonitor

            start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
            start_iso = _iso(start)
            wait_afk = AfkMonitor(
                start_iso=start_iso,
                mode="wait",
                idle_seconds_threshold=1.0,
                hard_cap_seconds=99_999.0,
            )
            # Force the next evaluate to time out: roll last_activity
            # back by 999s so the next now() (still close to start) is
            # already > idle_seconds_threshold.
            wait_afk.last_activity_iso = _iso(
                start - timedelta(seconds=999),
            )

            orig_resolve_afk = fo._resolve_afk_monitor

            def _patched_resolve_afk(contract, *, start_iso):
                del contract, start_iso
                return wait_afk

            fo._resolve_afk_monitor = _patched_resolve_afk  # type: ignore
            try:
                rc = _phase2_dispatch(
                    slug="t62-park",
                    task_dir=task_dir,
                    contract=self._make_contract(),
                    manifest=object(),
                    facts=object(),
                    ctx=object(),
                    criteria=[],
                    gate_cmds={
                        "baseline": "true", "codex": "true",
                        "smoke": "true",
                    },
                    run_id="run-park",
                    task_id="task-park",
                    notifier=notifier,
                    deps_factory=_deps_factory,
                )
            finally:
                fo._resolve_afk_monitor = orig_resolve_afk  # type: ignore

            # rc=5 = parked (distinct from 0/pass and 3/terminal).
            self.assertEqual(
                rc, PARKED_RECOVERABLE,
                f"wait-mode park must return rc=5 (PARKED_RECOVERABLE), got {rc}",
            )
            # No hard-stop.json on disk (park is recoverable).
            self.assertFalse(
                (task_dir / "hard-stop.json").exists(),
                "park must NOT persist HardStopSnapshot",
            )
            # No notifier.fire_block on park.
            self.assertEqual(notifier.fired, [],
                             "park must NOT fire block notifier")


class TestT821CmdAutoExecuteHonorsParkRc5(unittest.TestCase):
    """T8.2.1 P1.1 (legacy v0.8.2 T6.2 with rc=2; corrected to rc=5
    in v0.8.2.1): `_cmd_auto_execute` MUST treat rc=5
    (PARKED_RECOVERABLE) from `_phase2_dispatch` as 'parked, do NOT
    proceed to merge'. Merge gate must NOT be invoked."""

    def test_cmd_auto_execute_does_not_merge_on_park_rc5(self):
        """Drive _cmd_auto_execute with monkeypatched _phase2_dispatch
        returning rc=5 (PARKED_RECOVERABLE); assert MergeRunner.merge_task
        is NEVER called (gate 7 short-circuited on park)."""
        import flow_orchestrator as fo

        # Spies for the key call sites past the rc check.
        merge_calls: list = []
        gate8_calls: list = []
        return_codes: list = []

        # Monkeypatch _phase2_dispatch to return rc=5 (parked).
        orig_phase2 = fo._phase2_dispatch

        def _fake_phase2(**_kw):
            return PARKED_RECOVERABLE

        # Monkeypatch MergeRunner so any accidental merge attempt is
        # captured as a test failure (D-class regression detector).
        class _SpyMerger:
            def __init__(self, **kw):
                pass

            def merge_task(self, *a, **kw):
                merge_calls.append((a, kw))
                raise AssertionError(
                    "MergeRunner.merge_task called despite Phase 2 "
                    "park (rc=5 PARKED_RECOVERABLE; legacy v0.8.2 rc=2)"
                )

        class _SpyGate8:
            def __init__(self, **kw):
                pass

            def verify(self, *a, **kw):
                gate8_calls.append((a, kw))
                raise AssertionError(
                    "Gate8VerificationRunner.verify called despite "
                    "Phase 2 park (rc=5 PARKED_RECOVERABLE; legacy v0.8.2 rc=2)"
                )

        # Stub out the front-of-loop machinery so we can reach the
        # _phase2_dispatch call site quickly.
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / ".flow" / "tasks" / "t62-park-merge"
            task_dir.mkdir(parents=True)
            # Minimal contract.json so _cmd_auto_execute can hash it.
            contract_json = task_dir / "contract.json"
            contract_json.write_text(
                "{\"contract_schema_version\":1,"
                "\"autonomy_mode\":\"full\","
                "\"created_at\":\"2026-05-08T00:00:00Z\"}",
                encoding="utf-8",
            )

            # Build a synthetic plan with one manifest whose recovery
            # is "proceed", auto_dispatch is success, then _phase2 = rc2.
            from types import SimpleNamespace

            fake_manifest = SimpleNamespace(id="task-1")
            fake_contract = fo.Contract(
                contract_schema_version=1,
                autonomy_mode="full",
                created_at="2026-05-08T00:00:00Z",
                budget={
                    "tokens_in": 1_000_000.0,
                    "tokens_out": 1_000_000.0,
                    "cost_usd": 1000.0,
                    "active_wallclock_minutes": 600.0,
                    "subagent_dispatches": 100.0,
                },
            )
            fake_plan = SimpleNamespace(
                contract=fake_contract,
                manifests=[fake_manifest],
                fallback_reason=None,
            )

            patches = []

            def _patch(obj, name, value):
                patches.append((obj, name, getattr(obj, name)))
                setattr(obj, name, value)

            try:
                _patch(fo, "build_plan", lambda slug: fake_plan)
                _patch(fo, "_resolve_slug_dir",
                       lambda slug: task_dir)
                _patch(fo, "_resolve_or_create_run_id",
                       lambda td_: "run-1")
                _patch(fo, "_resolve_gate_commands",
                       lambda c: {"baseline": "true",
                                  "codex": "true",
                                  "smoke": "true",
                                  "merge_strategy": "merge"})
                _patch(fo, "_resolve_integration_target",
                       lambda c: "main")
                _patch(fo, "_task_already_completed",
                       lambda task_dir, *, run_id, task_id: False)

                # Recovery dispatcher → "proceed".
                class _OkVerdict:
                    action = "proceed"
                    block_type = None
                    blocked_md_path = None
                    details = None

                class _OkDispatcher:
                    def __init__(self, **kw):
                        pass

                    def classify(self):
                        return _OkVerdict()

                _patch(fo, "CrashRecoveryDispatcher", _OkDispatcher)

                # auto_dispatch_task → success (status NOT 'blocked').
                class _OkOutcome:
                    status = "ok"
                    block_type = None
                    blocked_md_path = None
                    ctx = SimpleNamespace()
                    facts = SimpleNamespace()

                _patch(fo, "auto_dispatch_task",
                       lambda **kw: _OkOutcome())

                _patch(fo, "Notifier",
                       lambda **kw: SimpleNamespace(
                           fire_block=lambda **k: None,
                       ))

                _patch(fo, "_phase2_dispatch", _fake_phase2)
                _patch(fo, "MergeRunner", _SpyMerger)
                _patch(fo, "Gate8VerificationRunner", _SpyGate8)

                rc = fo._cmd_auto_execute("t62-park-merge")
                return_codes.append(rc)
            finally:
                for obj, name, val in reversed(patches):
                    setattr(obj, name, val)

        # _cmd_auto_execute must propagate rc=5 (parked) and NEVER
        # reach the merge gate. Caller distinguishes parked (rc=5
        # PARKED_RECOVERABLE) from passed (rc=0) and terminal-blocked
        # (rc=3). v0.8.2.1: was rc=2 in v0.8.2; corrected to rc=5.
        self.assertEqual(merge_calls, [],
                         "merge_task must NOT run when Phase 2 parked")
        self.assertEqual(gate8_calls, [],
                         "gate 8 verify must NOT run when Phase 2 parked")
        self.assertEqual(
            return_codes, [PARKED_RECOVERABLE],
            f"_cmd_auto_execute must return rc=5 (PARKED_RECOVERABLE) "
            f"on park, got {return_codes}",
        )

    def test_cmd_auto_execute_logs_park_message_on_rc5(self):
        """rc=5 (PARKED_RECOVERABLE) should produce an operator-visible
        'Phase 2 parked' message on stderr so a human resuming knows
        to use `/flow:resume`. We capture stderr by redirecting
        sys.stderr during the call. (v0.8.2.1: was rc=2 in v0.8.2.)"""
        import io
        import contextlib
        import flow_orchestrator as fo
        from types import SimpleNamespace

        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / ".flow" / "tasks" / "t62-park-msg"
            task_dir.mkdir(parents=True)
            (task_dir / "contract.json").write_text(
                "{\"contract_schema_version\":1,"
                "\"autonomy_mode\":\"full\","
                "\"created_at\":\"2026-05-08T00:00:00Z\"}",
                encoding="utf-8",
            )

            fake_manifest = SimpleNamespace(id="task-1")
            fake_contract = fo.Contract(
                contract_schema_version=1,
                autonomy_mode="full",
                created_at="2026-05-08T00:00:00Z",
                budget={
                    "tokens_in": 1_000_000.0,
                    "tokens_out": 1_000_000.0,
                    "cost_usd": 1000.0,
                    "active_wallclock_minutes": 600.0,
                    "subagent_dispatches": 100.0,
                },
            )
            fake_plan = SimpleNamespace(
                contract=fake_contract,
                manifests=[fake_manifest],
                fallback_reason=None,
            )

            patches = []

            def _patch(obj, name, value):
                patches.append((obj, name, getattr(obj, name)))
                setattr(obj, name, value)

            class _OkVerdict:
                action = "proceed"
                block_type = None
                blocked_md_path = None
                details = None

            class _OkDispatcher:
                def __init__(self, **kw):
                    pass

                def classify(self):
                    return _OkVerdict()

            class _OkOutcome:
                status = "ok"
                block_type = None
                blocked_md_path = None
                ctx = SimpleNamespace()
                facts = SimpleNamespace()

            buf = io.StringIO()
            try:
                _patch(fo, "build_plan", lambda slug: fake_plan)
                _patch(fo, "_resolve_slug_dir",
                       lambda slug: task_dir)
                _patch(fo, "_resolve_or_create_run_id",
                       lambda td_: "run-1")
                _patch(fo, "_resolve_gate_commands",
                       lambda c: {"baseline": "true",
                                  "codex": "true",
                                  "smoke": "true",
                                  "merge_strategy": "merge"})
                _patch(fo, "_resolve_integration_target",
                       lambda c: "main")
                _patch(fo, "_task_already_completed",
                       lambda task_dir, *, run_id, task_id: False)
                _patch(fo, "CrashRecoveryDispatcher", _OkDispatcher)
                _patch(fo, "auto_dispatch_task",
                       lambda **kw: _OkOutcome())
                _patch(fo, "Notifier",
                       lambda **kw: SimpleNamespace(
                           fire_block=lambda **k: None,
                       ))
                _patch(fo, "_phase2_dispatch", lambda **_kw: PARKED_RECOVERABLE)

                with contextlib.redirect_stderr(buf):
                    rc = fo._cmd_auto_execute("t62-park-msg")
            finally:
                for obj, name, val in reversed(patches):
                    setattr(obj, name, val)

            self.assertEqual(rc, PARKED_RECOVERABLE)
            stderr_text = buf.getvalue()
            # Operator-visible cues: "park" + "/flow:resume".
            self.assertIn(
                "park", stderr_text.lower(),
                f"stderr must mention 'park'; got {stderr_text!r}",
            )
            self.assertIn(
                "flow:resume", stderr_text,
                f"stderr must hint at /flow:resume; got "
                f"{stderr_text!r}",
            )


# ----------------------------------------------------------------------
# T6.2 P1.2 — wallclock budget can be bypassed on the final successful
# round. Pre-tick happens BEFORE impl runs; if impl is slow enough to
# push active_seconds over the cap, the post-impl review "pass" path
# returned without re-checking. Fix: re-tick + re-check after impl,
# BEFORE branching on review_outcome. Budget enforcement wins over
# review verdict.
# ----------------------------------------------------------------------

class TestT62WallclockBudgetPostImplOverridesPass(unittest.TestCase):
    def test_wallclock_budget_hit_post_impl_overrides_review_pass(self):
        """Setup: wallclock limit = 1 minute. now_iso advances by 90s on
        the impl call (simulating a slow round). Pre-tick ticks 0min ok,
        impl runs, post-impl now=90s -> 1.5min > 1min limit -> budget_hit
        even though scripted review said 'pass'."""
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        # Wide AFK so it never fires.
        afk = AfkMonitor(
            start_iso=start_iso,
            mode="abort",
            idle_seconds_threshold=99_999_999.0,
            hard_cap_seconds=99_999_999.0,
        )

        # Hand-crafted now_fn: deterministic sequence so we can simulate
        # impl taking 90s.
        #   Call 1 (pre-tick):           t = start + 0s
        #   Call 2 (impl-end activity):  t = start + 90s
        #   Call 3 (review note):        t = start + 90s
        #   Call 4 (post-impl tick):     t = start + 90s
        #   ... etc.
        # We use a list of pre-set times.
        times = [
            start,                              # pre-tick
            start + timedelta(seconds=90),      # afk heartbeat
            start + timedelta(seconds=90),      # progress log
            start + timedelta(seconds=90),      # post-impl re-tick
            start + timedelta(seconds=90),      # afk pause
            start + timedelta(seconds=90),      # afk resume
            start + timedelta(seconds=90),      # progress log review
            start + timedelta(seconds=90),      # extra
            start + timedelta(seconds=90),      # extra
            start + timedelta(seconds=90),      # extra
        ]
        idx = {"i": 0}

        def now_fn() -> str:
            i = idx["i"]
            idx["i"] = min(i + 1, len(times) - 1)
            return _iso(times[i])

        # Limit 1 minute (60s); 90s elapsed -> 1.5min > 1min.
        budget = bc.make_default_set({**_LIMITS,
                                      "active_wallclock_minutes": 1.0})

        # Scripted impl returns no deltas (all the "slowness" is wallclock,
        # not tokens), so only wallclock can trip.
        impl_calls: list = []
        review_calls: list = []

        def _impl(*, state, prompt_prefix, **__):
            del state, prompt_prefix
            impl_calls.append(True)
            return {}

        def _review(*, state, impl_deltas, **__):
            del state, impl_deltas
            review_calls.append(True)
            return "pass"   # Reviewer says pass; budget MUST override.

        deps = RetryDeps(
            run_implementer_round=_impl, run_codex_review=_review,
        )
        state = _make_state(start_iso)
        cfg = RetryConfig(max_dispatch_retry_rounds=99,
                          max_codex_review_rounds=99)
        outcome, snap = dispatch_with_retry(
            state=state, config=cfg, budget=budget,
            afk=afk, deps=deps, now_iso_fn=now_fn,
        )
        # Budget enforcement wins over review "pass".
        self.assertEqual(
            outcome, "budget_hit",
            f"post-impl wallclock recheck must override review pass; "
            f"got {outcome!r}",
        )
        self.assertIsInstance(snap, HardStopSnapshot)
        self.assertEqual(snap.counter_name, "active_wallclock_minutes")
        self.assertEqual(snap.limit, 1.0)
        # impl ran exactly once (loop terminated post-impl).
        self.assertEqual(len(impl_calls), 1)
        # Review may or may not run depending on implementation
        # (re-check could happen before review). Either way, the loop
        # must NOT reach a "pass" terminal.


# ----------------------------------------------------------------------
# T6.2 P2 — BudgetCounter.DEFAULT_WARN_THRESHOLD aligned to 0.8
# (matching context_estimator.slack_state — 20% headroom for ±20%
# coarseness). T6.1 lowered slack_state to 0.8 but left BudgetCounter
# at 0.9, creating two competing warn policies.
# ----------------------------------------------------------------------

class TestT62BudgetCounterWarnThresholdAligned(unittest.TestCase):
    def test_budget_counter_default_warn_at_80_pct(self):
        """80% used should trip is_warn() with the default threshold."""
        c = bc.BudgetCounter(name="x", value=80.0, limit=100.0)
        self.assertTrue(
            c.is_warn(),
            "default warn threshold must be 0.8 (matches slack_state)",
        )

    def test_budget_counter_just_below_default_warn_is_ok(self):
        """79% used should NOT trip is_warn() with default threshold."""
        c = bc.BudgetCounter(name="x", value=79.0, limit=100.0)
        self.assertFalse(c.is_warn())

    def test_budget_counter_warn_threshold_module_constant_is_080(self):
        """Module-level DEFAULT_WARN_THRESHOLD literal == 0.8."""
        self.assertEqual(bc.DEFAULT_WARN_THRESHOLD, 0.8)

    def test_budget_counter_explicit_threshold_still_honored(self):
        """Caller-supplied threshold overrides default."""
        c = bc.BudgetCounter(name="x", value=85.0, limit=100.0)
        self.assertFalse(c.is_warn(threshold=0.9))
        self.assertTrue(c.is_warn(threshold=0.79))


if __name__ == "__main__":
    unittest.main()
