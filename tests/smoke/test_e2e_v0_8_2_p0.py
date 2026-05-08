"""T5 — End-to-end integration suite for v0.8.2 P0 core.

Six scenarios that chain >= 3 of the 4 P0 modules (budget, AFK,
retry-loop, dispatch-template) through the production entrypoint
`_phase2_dispatch` (or `dispatch_with_retry` directly when the
hardcoded retry/review caps in `_phase2_dispatch` make the scenario
infeasible).

Hard rules (per T5 brief):

- TESTS ONLY. No production code changes. If integration reveals a
  T1-T4 bug, the test is `@unittest.skip("v0.8.3 follow-up: ...")`'d
  and reported, NOT papered over.
- Use existing test seams (`deps_factory`, `now_iso_fn`, AfkMonitor
  constructor). No module-level monkeypatching.
- No real subprocess, no real time, no real subagent dispatch.
- Atomic file ops via `tempfile.TemporaryDirectory()` per test.
- Determinism: every `now_iso` value passed in explicitly.
- Do NOT touch `~/.claude/hooks/.review-passed` (K-class).

Self-check (J-class chained-paper-cut guard):

- B-class: each test exercises a state transition (round count,
  AFK reset, budget tick).
- D-class: where feasible we drive `_phase2_dispatch` (prod
  entrypoint), not the loop directly.
- E-class: no shell=True.
- G-class: tmp dir + atomic writes verified by reading back snapshot.
- H-class: no parsing of subprocess output (no subprocess used).
- I-class: each test gets fresh budget/clock/state — no leakage.
- J-class: each test chains >=3 modules.
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
from dispatch_template import (  # noqa: E402  type: ignore
    K_CLASS_SENTINEL_PROHIBITION,
)
from flow_orchestrator import (  # noqa: E402  type: ignore
    Contract,
    RetryConfig,
    RetryDeps,
    RetrySessionState,
    _phase2_dispatch,
    dispatch_with_retry,
)


# ── Shared helpers ──────────────────────────────────────────────────


_BIG_LIMITS = {
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


def _stepping_now_fn(start: datetime, step_seconds: float = 1.0):
    """Deterministic now-iso source. Each call advances by step_seconds."""
    state = {"t": start}

    def f() -> str:
        s = _iso(state["t"])
        state["t"] = state["t"] + timedelta(seconds=step_seconds)
        return s

    return f


def _scripted_review(seq: list):
    seq = list(seq)

    def f(*args, **kwargs):
        if not seq:
            raise AssertionError("review called more times than scripted")
        return seq.pop(0)

    f.remaining = lambda: len(seq)
    return f


class _SpyNotifier:
    def __init__(self):
        self.fired: list = []

    def fire_block(self, **kw):
        self.fired.append(kw)


def _make_contract(budget_overrides: dict | None = None) -> Contract:
    """Big-budget Contract for tests that don't want budget pressure."""
    budget = dict(_BIG_LIMITS)
    if budget_overrides:
        budget.update(budget_overrides)
    return Contract(
        contract_schema_version=1,
        autonomy_mode="full",
        created_at="2026-05-08T00:00:00Z",
        budget=budget,
    )


def _seed_progress(task_dir: Path) -> Path:
    progress = task_dir / "progress.md"
    progress.write_text(
        "# progress\n\n## Execute Log\n\n"
        "| round | role | counters |\n|---|---|---|\n",
        encoding="utf-8",
    )
    return progress


# ────────────────────────────────────────────────────────────────────
# Scenario 1 — budget hit during retry loop terminates with snapshot
# ────────────────────────────────────────────────────────────────────

class TestE2EBudgetHitDuringRetryLoop(unittest.TestCase):
    """Budget pre-tick gate fires BEFORE round 2 impl runs.

    Chains: budget + retry-loop + dispatch-template (prompt prefix
    K-class prohibition is built every loop iteration).

    `_phase2_dispatch` hardcodes max_retry=3, max_review=2 — for this
    scenario we need higher caps so we drive `dispatch_with_retry`
    directly. (The chain still uses budget+retry+template; only the
    notifier wiring in `_phase2_dispatch` is bypassed.)
    """

    def test_budget_hit_pre_tick_round_two_terminates_with_snapshot(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            progress = _seed_progress(task_dir)

            # tokens_in cap = 1000. impl burns 600 per call.
            # Round 1: 600 < 1000 OK; review fail -> retry+=1.
            # Round 2 pre-tick: tokens_in=600 still under cap (next add
            # not yet applied). impl runs again -> 1200; recheck-after
            # tick fires with budget_hit. (Loop also has post-impl
            # recheck.) Either way: terminal=budget_hit, snapshot
            # written.
            #
            # Note: the loop's _first_tripped_counter check is BEFORE
            # impl runs. To trip pre-tick on round 2 we need a counter
            # that's already >= limit at the top of the iteration.
            # Setting tokens_in limit=500 with delta=600 trips
            # post-impl on round 1. To trip pre-tick on round 2 we
            # use limit=600 + delta=600 -> round 1 post-impl is
            # value=600 >= 600 -> budget_hit -> terminate after 1
            # impl + 0 review (budget gates pre-review too).
            #
            # The brief asks: "Round 1: tokens_in 600 < 1000 OK ...
            # Round 2: tokens_in 1200 >= 1000". That maps to limit
            # 1000 + delta 600 + 600 = 1200. Round 1 post-impl
            # check: 600 < 1000 -> proceed to review (fail). Round 2
            # pre-tick: tokens 600 still (delta not applied).
            # Round 2 impl runs: 1200. Round 2 post-impl recheck:
            # 1200 >= 1000 -> budget_hit. So review IS called once
            # (round 1) and impl twice; budget hits AFTER second
            # impl applies its delta, BEFORE second review. Brief
            # says "review called only once" — matches.
            limits = dict(_BIG_LIMITS)
            limits["tokens_in"] = 1000.0
            budget = bc.make_default_set(limits)

            review_calls: list = []

            def _impl(*, state, prompt_prefix, **__):
                # B-class: prompt_prefix MUST contain K-class
                # prohibition every iteration.
                self.assertIn(
                    K_CLASS_SENTINEL_PROHIBITION, prompt_prefix,
                    "K-class prohibition missing from impl prompt",
                )
                return {"tokens_in": 600.0, "tokens_out": 0.0}

            def _review(*, state, impl_deltas, **__):
                review_calls.append(state.dispatch_retry_rounds)
                return "fail"

            cfg = RetryConfig(
                max_dispatch_retry_rounds=5,
                max_codex_review_rounds=5,
            )
            state = RetrySessionState(
                task_slug="e2e-budget",
                progress_path=progress,
            )
            afk = AfkMonitor(
                start_iso=start_iso,
                mode="abort",
                idle_seconds_threshold=99_999_999.0,
                hard_cap_seconds=99_999_999.0,
            )
            outcome, snap = dispatch_with_retry(
                state=state, config=cfg, budget=budget, afk=afk,
                deps=RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                ),
                now_iso_fn=_stepping_now_fn(start),
            )

            # Terminal: budget_hit on tokens_in.
            self.assertEqual(outcome, "budget_hit")
            self.assertIsInstance(snap, HardStopSnapshot)
            self.assertEqual(snap.reason, "budget_hit")
            self.assertEqual(snap.counter_name, "tokens_in")
            self.assertGreaterEqual(snap.value, 1000.0)
            self.assertEqual(snap.schema_version, "v1")

            # Review called only once — pre-review gate (post-impl
            # recheck) trips on round 2 BEFORE the second review.
            self.assertEqual(
                len(review_calls), 1,
                f"expected review called 1x, got {len(review_calls)}",
            )

            # progress.md got round-1 impl + round-1 reviewer rows
            # logged. Round-2 impl row also logged before the post-
            # impl budget recheck. We assert >= 2 rows (round 1 impl
            # + round 1 reviewer) and that no spurious round 2
            # reviewer row landed.
            content = progress.read_text(encoding="utf-8")
            self.assertIn("| 1 | implementer", content)
            self.assertIn("| 1 | reviewer", content)
            self.assertNotIn("| 2 | reviewer", content)


# ────────────────────────────────────────────────────────────────────
# Scenario 2 — AFK hard cap overrides wait mode with snapshot
# ────────────────────────────────────────────────────────────────────

class TestE2EAfkHardCapOverridesWaitMode(unittest.TestCase):
    """24h hard cap overrides `mode='wait'` and produces a snapshot.

    Chains: AFK + retry-loop + dispatch-template (prompt prefix runs
    only if hard cap doesn't fire pre-tick).
    """

    def test_hard_cap_in_wait_mode_produces_terminal_snapshot(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        # AfkMonitor is constructed externally so we control the
        # clock — the contract path through `_phase2_dispatch` always
        # builds a fresh monitor anyway. We use `dispatch_with_retry`
        # directly to inject the AfkMonitor with hard_cap_seconds set
        # such that the FIRST evaluate() trips it.
        afk = AfkMonitor(
            start_iso=start_iso,
            mode="wait",
            idle_seconds_threshold=1800.0,
            hard_cap_seconds=10.0,  # tiny cap so eval trips immediately
        )
        # Advance the clock past 10s before first iteration: stepping
        # fn returns t0, t0+1s, ... but we want active_seconds(now) to
        # already be >= 10. So set step=20s -> first call returns t0
        # (eval at t0: active_seconds=0, no trip). To trip on the
        # FIRST iteration we instead pre-advance: build a now_fn that
        # starts at start+20s.
        bumped_start = start + timedelta(seconds=20)
        now_fn = _stepping_now_fn(bumped_start, step_seconds=1.0)

        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            progress = _seed_progress(task_dir)
            state = RetrySessionState(
                task_slug="e2e-afk",
                progress_path=progress,
            )
            cfg = RetryConfig(
                max_dispatch_retry_rounds=3,
                max_codex_review_rounds=2,
            )

            impl_calls: list = []

            def _impl(*, state, prompt_prefix, **__):
                impl_calls.append(state.dispatch_retry_rounds)
                return {}

            def _review(*, state, impl_deltas, **__):
                return "pass"

            outcome, snap = dispatch_with_retry(
                state=state, config=cfg,
                budget=bc.make_default_set(_BIG_LIMITS),
                afk=afk,
                deps=RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                ),
                now_iso_fn=now_fn,
            )

            # AFK hard cap fired pre-tick -> impl never invoked.
            self.assertEqual(outcome, "afk_hard_cap")
            self.assertEqual(impl_calls, [])
            self.assertIsInstance(snap, HardStopSnapshot)
            # AfkMonitor.to_snapshot pins reason="afk_timeout" for
            # both timeout and hard_cap; the trigger discriminator
            # lives in extra["trigger"].
            self.assertEqual(snap.reason, "afk_timeout")
            self.assertEqual(snap.extra.get("mode"), "wait")
            self.assertEqual(snap.extra.get("trigger"), "hard_cap")
            self.assertIn("hard_cap_seconds", snap.extra)


# ────────────────────────────────────────────────────────────────────
# Scenario 3 — codex_review_cap independent of retry counter
# ────────────────────────────────────────────────────────────────────

class TestE2ECodexReviewCapIndependentOfRetry(unittest.TestCase):
    """3 RWR verdicts in a row -> codex_review_cap on round 3 pre-tick.

    Critical invariant (R3.4 + invariant 2): RWR advances ONLY
    `codex_review_rounds`, NOT `dispatch_retry_rounds`. So with
    max_dispatch_retry_rounds=10 (high) and max_codex_review_rounds=2
    (low), the loop hits review_cap with retry_rounds == 0.

    Chains: retry-loop + dispatch-template + budget (counters
    monitored every iteration even when not tripped).
    """

    def test_three_rwr_terminates_with_review_cap_retry_zero(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        cfg = RetryConfig(
            max_dispatch_retry_rounds=10,
            max_codex_review_rounds=2,
        )
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            progress = _seed_progress(task_dir)
            state = RetrySessionState(
                task_slug="e2e-review-cap",
                progress_path=progress,
            )
            afk = AfkMonitor(
                start_iso=start_iso,
                mode="abort",
                idle_seconds_threshold=99_999_999.0,
                hard_cap_seconds=99_999_999.0,
            )

            impl_calls: list = []

            def _impl(*, state, prompt_prefix, **__):
                impl_calls.append(state.codex_review_rounds)
                return {"tokens_in": 1.0, "tokens_out": 1.0}

            outcome, snap = dispatch_with_retry(
                state=state, config=cfg,
                budget=bc.make_default_set(_BIG_LIMITS),
                afk=afk,
                deps=RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_scripted_review([
                        "rejected_with_rationale",
                        "rejected_with_rationale",
                        "rejected_with_rationale",
                    ]),
                ),
                now_iso_fn=_stepping_now_fn(start),
            )

            self.assertEqual(outcome, "review_cap")
            self.assertEqual(state.dispatch_retry_rounds, 0,
                             "RWR must not consume retry counter")
            self.assertEqual(state.codex_review_rounds, 2)
            self.assertIsInstance(snap, HardStopSnapshot)
            # Round-cap snapshots use the precise reason name.
            self.assertEqual(snap.reason, "codex_review_cap")
            self.assertEqual(snap.extra.get("max"), 2)
            # impl ran twice (rounds 1 + 2); the 3rd iteration
            # tripped pre-tick before impl.
            self.assertEqual(len(impl_calls), 2)


# ────────────────────────────────────────────────────────────────────
# Scenario 4 — K-class prohibition present in implementer prompt
# ────────────────────────────────────────────────────────────────────

class TestE2EKClassProhibitionInImplementerPrompt(unittest.TestCase):
    """Prompt prefix built every loop iteration carries the verbatim
    K-class sentinel prohibition.

    Chains: dispatch-template + retry-loop + budget (counters live
    even if not tripped).
    Driven through `_phase2_dispatch` (prod entrypoint, D-class).
    """

    def test_prompt_prefix_carries_k_class_prohibition_verbatim(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            _seed_progress(task_dir)

            captured_prefixes: list = []

            def _factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    captured_prefixes.append(prompt_prefix)
                    return {}

                def _review(*, state, impl_deltas, **__):
                    return "pass"

                return RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                )

            notifier = _SpyNotifier()
            rc = _phase2_dispatch(
                slug="e2e-kclass",
                task_dir=task_dir,
                contract=_make_contract(),
                manifest=object(),
                facts=object(),
                ctx=object(),
                criteria=[],
                gate_cmds={
                    "baseline": "true",
                    "codex": "true",
                    "smoke": "true",
                },
                run_id="run-kclass",
                task_id="task-kclass",
                notifier=notifier,
                deps_factory=_factory,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(notifier.fired, [])
            self.assertEqual(
                len(captured_prefixes), 1,
                "expected 1 impl call on a pass path",
            )
            prefix = captured_prefixes[0]
            # Verbatim presence — not a paraphrased lookalike.
            self.assertIn(K_CLASS_SENTINEL_PROHIBITION, prefix)
            # Sanity: the K-class identity anchor (forensic count) is
            # not silently weakened.
            self.assertIn("2 real bugs in v0.8.1", prefix)


# ────────────────────────────────────────────────────────────────────
# Scenario 5 — 18-class redaction integrated with retry feedback
# ────────────────────────────────────────────────────────────────────

class TestE2EReviewerFeedbackRedactedBeforeImplementer(unittest.TestCase):
    """Round-2 implementer prompt strips 18-class trigger labels but
    preserves the SPECIFIC findings (line refs / behaviours).

    Chains: dispatch-template + retry-loop + redaction integration.
    Driven through `_phase2_dispatch`.
    """

    def test_round_two_prompt_strips_class_letters_keeps_findings(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            _seed_progress(task_dir)

            captured_prefixes: list = []
            review_outcomes = ["fail", "pass"]

            def _factory(**_kw):
                def _impl(*, state, prompt_prefix, **__):
                    captured_prefixes.append(prompt_prefix)
                    return {}

                def _review(*, state, impl_deltas, **__):
                    verdict = review_outcomes.pop(0)
                    if verdict == "fail":
                        # Findings the reviewer would emit. Includes
                        # 18-class trigger labels (must be stripped
                        # on the path back to the implementer) AND
                        # specific findings (must be preserved).
                        # Trigger labels live on dedicated header
                        # lines (matches the redaction line-mode
                        # contract: whole-line drop). The findings
                        # ride on separate lines and survive.
                        state.last_reviewer_feedback = (
                            "B. blindspot category note\n"
                            "Specific finding: missed transition at "
                            "line 42 (lock not released)\n"
                            "Class A: blindspot category note\n"
                            "Specific finding: get vs in — tokens_out "
                            "parsed as int\n"
                            "[J] chained paper-cut suspected\n"
                            "Specific finding: review feedback fix-chain "
                            "interaction with retry-counter advance\n"
                        )
                    return verdict

                return RetryDeps(
                    run_implementer_round=_impl,
                    run_codex_review=_review,
                )

            notifier = _SpyNotifier()
            rc = _phase2_dispatch(
                slug="e2e-redact",
                task_dir=task_dir,
                contract=_make_contract(),
                manifest=object(),
                facts=object(),
                ctx=object(),
                criteria=[],
                gate_cmds={
                    "baseline": "true",
                    "codex": "true",
                    "smoke": "true",
                },
                run_id="run-redact",
                task_id="task-redact",
                notifier=notifier,
                deps_factory=_factory,
            )

            self.assertEqual(rc, 0)
            self.assertEqual(len(captured_prefixes), 2)
            round2 = captured_prefixes[1]

            # Specific findings preserved (they ride on dedicated
            # lines that don't match the redaction trigger pattern).
            self.assertIn("missed transition at line 42", round2)
            self.assertIn("lock not released", round2)
            self.assertIn("get vs in", round2)
            self.assertIn("tokens_out parsed as int", round2)
            self.assertIn("retry-counter advance", round2)

            # Class-label trigger headers stripped.
            self.assertNotIn("B. blindspot", round2)
            self.assertNotIn("Class A: blindspot", round2)
            self.assertNotIn("[J] chained paper-cut", round2)


# ────────────────────────────────────────────────────────────────────
# Scenario 6 — pass path short-circuits without snapshot
# ────────────────────────────────────────────────────────────────────

class TestE2EPassPathShortCircuits(unittest.TestCase):
    """Round 1 pass -> rc=0, no `hard-stop.json`, no notifier fire,
    AFK heartbeat noted.

    Chains: retry-loop + AFK heartbeat + dispatch-template + budget
    register_dispatch increment (subagent_dispatches).
    Driven through `_phase2_dispatch`.
    """

    def test_pass_round_one_no_snapshot_no_block(self):
        start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)
        start_iso = _iso(start)
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td)
            _seed_progress(task_dir)

            # Spy on AfkMonitor.note_subagent_heartbeat by injecting
            # a custom AfkMonitor that records the call. We can't do
            # that through `_phase2_dispatch` (which builds its own
            # monitor) without monkey-patching the class — so we use
            # `dispatch_with_retry` directly here. The pass-path
            # semantics (no notifier fire / no snapshot file) belong
            # to `_phase2_dispatch`'s wrapper; we verify those via
            # the RC + tmp-dir contents in a separate sub-assertion
            # by ALSO running through `_phase2_dispatch`.

            # ── Sub-assertion A: heartbeat seen via direct loop ──
            heartbeat_calls: list = []
            real_afk = AfkMonitor(
                start_iso=start_iso,
                mode="abort",
                idle_seconds_threshold=99_999_999.0,
                hard_cap_seconds=99_999_999.0,
            )
            orig_note = real_afk.note_subagent_heartbeat

            def _spy_note(now_iso: str) -> None:
                heartbeat_calls.append(now_iso)
                orig_note(now_iso)

            real_afk.note_subagent_heartbeat = _spy_note  # type: ignore

            state_a = RetrySessionState(
                task_slug="e2e-pass-a",
                progress_path=task_dir / "progress.md",
            )
            outcome, snap = dispatch_with_retry(
                state=state_a,
                config=RetryConfig(
                    max_dispatch_retry_rounds=3,
                    max_codex_review_rounds=2,
                ),
                budget=bc.make_default_set(_BIG_LIMITS),
                afk=real_afk,
                deps=RetryDeps(
                    run_implementer_round=lambda *, state, prompt_prefix, **_: {},
                    run_codex_review=lambda *, state, impl_deltas, **_: "pass",
                ),
                now_iso_fn=_stepping_now_fn(start),
            )
            self.assertEqual(outcome, "pass")
            self.assertIsNone(snap)
            self.assertEqual(state_a.dispatch_retry_rounds, 0)
            self.assertEqual(state_a.codex_review_rounds, 0)
            self.assertEqual(
                len(heartbeat_calls), 1,
                "expected exactly 1 AFK heartbeat on a 1-round pass",
            )

            # ── Sub-assertion B: prod entrypoint, no terminal file ──
            with tempfile.TemporaryDirectory() as td2:
                task_dir2 = Path(td2)
                _seed_progress(task_dir2)

                def _factory(**_kw):
                    def _impl(*, state, prompt_prefix, **__):
                        return {}

                    def _review(*, state, impl_deltas, **__):
                        return "pass"

                    return RetryDeps(
                        run_implementer_round=_impl,
                        run_codex_review=_review,
                    )

                notifier = _SpyNotifier()
                rc = _phase2_dispatch(
                    slug="e2e-pass-b",
                    task_dir=task_dir2,
                    contract=_make_contract(),
                    manifest=object(),
                    facts=object(),
                    ctx=object(),
                    criteria=[],
                    gate_cmds={
                        "baseline": "true",
                        "codex": "true",
                        "smoke": "true",
                    },
                    run_id="run-pass-b",
                    task_id="task-pass-b",
                    notifier=notifier,
                    deps_factory=_factory,
                )
                self.assertEqual(rc, 0)
                self.assertEqual(notifier.fired, [])
                snap_path = task_dir2 / "hard-stop.json"
                self.assertFalse(
                    snap_path.exists(),
                    "pass path must NOT write hard-stop.json",
                )


if __name__ == "__main__":
    unittest.main()
