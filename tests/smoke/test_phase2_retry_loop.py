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


if __name__ == "__main__":
    unittest.main()
