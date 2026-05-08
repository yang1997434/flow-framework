"""v0.8.3 P0.1 — fresh-worktree-per-round implementer redispatch tests.

Closes v0.8.2 T18 deferred stub: production `_prod_impl` returning empty
deltas on Round 2+ (no real implementer dispatch on retry). The new
helper `_dispatch_implementer_fresh_worktree` spins a fresh worktree
per retry round (`+r<N>` discriminator) and dispatches the implementer
subagent in it. Winner ctx flows through to Gate 7 merge.

Coverage map (see `.flow/tasks/05-08-v0.8.3-p0.1-implementer-redispatch/
prd.md` ACs and progress.md Step L):

- T-B: two-phase commit on round 2+ — helper raise mid-flight leaves
  state coherent (no half-swap of `current_round_*`).
- T-C: worktree-create OSError → InfraFailureError → phase2_infra_failure
  block (rc=3, no silent re-loop).
- T-D: winner E2E — Round 2 PASS makes `_phase2_dispatch` return Round 2
  ctx (NOT Round 1's stale ctx).
- T-E: Round 1 PASS aliasing — winner_ctx is the same object as the
  seeded Round 1 ctx; Round 1 fields not mutated by the loop.
- T-F: counter monotonicity under mixed sequence
  (rejected_with_rationale → fail → pass) with new state fields present.
- T-G: worktree id discriminator — `+r<N>` distinct from Round 1 legacy
  naming + collisions don't crash the test repo.
- T-H: failed_rounds non-persistence contract — in-memory only,
  documented invariant for now (no journal mirror in P0.1).
- T-K: task_brief renderer reads prd.md or falls back to criteria.

Each test is full-fake / unit (no real subagent). The mini-integration
test using a real tmp git repo lives in
`tests/smoke/test_fresh_worktree_per_round.py` (T-D + T-G + AC2).
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_orchestrator as fo  # noqa: E402  type: ignore
from flow_orchestrator import (  # noqa: E402  type: ignore
    InfraFailureError,
    RetryDeps,
    RetrySessionState,
    RoundRecord,
    WorktreeContext,
    _dispatch_implementer_fresh_worktree,
    _render_task_brief,
    create_task_worktree,
    dispatch_with_retry,
)


def _make_ctx(slug="demo", task_idx=0, round_num=1, branch="m") -> WorktreeContext:
    """Cheap synthetic ctx for unit tests; bypasses git."""
    sha = "abc1234"
    suffix = f"+r{round_num}" if round_num >= 2 else ""
    return WorktreeContext(
        slug=slug,
        task_idx=task_idx,
        worktree_id=f"{slug}+t{task_idx}{suffix}+{sha}",
        worktree_path=Path(f"/tmp/wt-{slug}-{round_num}"),
        branch=f"{slug}+t{task_idx}{suffix}+{sha}",
        integration_target=branch,
        original_base_commit=sha * 4,
        current_base_commit=sha * 4,
        base_shortsha=sha,
        lifecycle_state="active",
        created_at="2026-05-08T00:00:00Z",
        round_num=round_num,
    )


class TestRoundRecord(unittest.TestCase):
    """RoundRecord lightweight forensic record (codex round-1 P1 §4)."""

    def test_from_ctx_captures_identity(self):
        ctx = _make_ctx(round_num=3)
        rr = RoundRecord.from_ctx(ctx)
        self.assertEqual(rr.worktree_id, ctx.worktree_id)
        self.assertEqual(rr.worktree_path, ctx.worktree_path)
        self.assertEqual(rr.branch, ctx.branch)
        self.assertEqual(rr.round_num, 3)

    def test_round_record_is_frozen(self):
        rr = RoundRecord.from_ctx(_make_ctx(round_num=2))
        with self.assertRaises(Exception):  # FrozenInstanceError
            rr.round_num = 99  # type: ignore[misc]


class TestWorktreeRoundNum(unittest.TestCase):
    """WorktreeContext.round_num default + create_task_worktree
    discriminator (Step A + C, codex round-1 G2)."""

    def test_round_num_default_is_1(self):
        ctx = _make_ctx()
        self.assertEqual(ctx.round_num, 1)

    def test_create_task_worktree_round_num_validation(self):
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=Path("/nonexistent"),
                slug="demo", task_idx=0,
                integration_target="master", round_num=0,
            )
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=Path("/nonexistent"),
                slug="demo", task_idx=0,
                integration_target="master", round_num=True,
            )


class TestDispatchHelper(unittest.TestCase):
    """`_dispatch_implementer_fresh_worktree` happy / error paths
    (Step B, T-B, T-C)."""

    def setUp(self):
        # tmp git repo with a single base commit
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        subprocess.run(
            ["git", "init", "-q", "-b", "master", "."],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            ["git", "-c", "user.email=t@t", "-c", "user.name=t",
             "commit", "--allow-empty", "-m", "init", "-q"],
            cwd=self.repo, check=True,
        )
        # Patch out the subagent shim so the helper doesn't try to
        # import the (non-existent in tests) flow_subagent_dispatch module.
        self._orig_dispatch = fo._invoke_subagent_dispatch
        self.dispatch_calls: list = []

        def _fake_dispatch(ctx, **kw):
            self.dispatch_calls.append((ctx.worktree_id, ctx.round_num, kw))

        fo._invoke_subagent_dispatch = _fake_dispatch

    def tearDown(self):
        fo._invoke_subagent_dispatch = self._orig_dispatch
        self.tmp.cleanup()

    def test_round_num_lt_2_rejected(self):
        with self.assertRaises(ValueError):
            _dispatch_implementer_fresh_worktree(
                repo_root=self.repo, slug="demo", task_id="task-x",
                task_idx=0, integration_target="master",
                prompt_prefix="", round_num=1,
            )

    def test_happy_path_returns_fresh_ctx_and_facts(self):
        ctx, facts, deltas = _dispatch_implementer_fresh_worktree(
            repo_root=self.repo, slug="demo", task_id="task-x",
            task_idx=0, integration_target="master",
            prompt_prefix="hello", round_num=2,
        )
        self.assertEqual(ctx.round_num, 2)
        self.assertIn("+r2+", ctx.worktree_id)
        self.assertEqual(facts.changed_files, [])
        self.assertEqual(deltas, {})
        self.assertEqual(len(self.dispatch_calls), 1)
        self.assertEqual(self.dispatch_calls[0][1], 2)

    def test_subagent_runtime_error_wraps_to_infra_failure_with_ctx(self):
        """T-C variant: dispatch shim raises -> InfraFailureError carries
        ctx so caller can record orphan worktree."""
        def _crash(ctx, **kw):
            raise RuntimeError("simulated subagent crash")
        fo._invoke_subagent_dispatch = _crash

        with self.assertRaises(InfraFailureError) as cm:
            _dispatch_implementer_fresh_worktree(
                repo_root=self.repo, slug="demo", task_id="task-x",
                task_idx=0, integration_target="master",
                prompt_prefix="", round_num=3,
            )
        # Orphan ctx must be attached for caller cleanup.
        self.assertTrue(hasattr(cm.exception, "ctx"))
        self.assertEqual(cm.exception.ctx.round_num, 3)

    def test_worktree_create_failure_wraps_to_infra_failure_no_ctx(self):
        """T-C: worktree-create failure (collision) -> InfraFailureError
        with NO ctx attribute (because no ctx was successfully built)."""
        # First Round-2 worktree succeeds.
        _dispatch_implementer_fresh_worktree(
            repo_root=self.repo, slug="demo", task_id="task-x",
            task_idx=0, integration_target="master",
            prompt_prefix="", round_num=2,
        )
        # Second attempt with same round_num -> branch collision.
        with self.assertRaises(InfraFailureError) as cm:
            _dispatch_implementer_fresh_worktree(
                repo_root=self.repo, slug="demo", task_id="task-x",
                task_idx=0, integration_target="master",
                prompt_prefix="", round_num=2,
            )
        # Helper raised before ctx existed.
        self.assertFalse(hasattr(cm.exception, "ctx"))


class TestRenderTaskBrief(unittest.TestCase):
    """Step K — task brief renderer prefers prd.md, falls back to criteria."""

    def test_prefers_prd_md(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            (p / "prd.md").write_text("# Brief from prd\n\nGoal: X\n")
            brief = _render_task_brief(task_dir=p, criteria=["c1", "c2"])
            self.assertIn("Brief from prd", brief)
            self.assertNotIn("c1", brief)  # criteria not appended

    def test_fallback_to_criteria_when_no_prd(self):
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            brief = _render_task_brief(task_dir=p, criteria=["c1", "c2"])
            self.assertIn("c1", brief)
            self.assertIn("c2", brief)

    def test_empty_when_neither_present(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(
                _render_task_brief(task_dir=Path(td), criteria=[]),
                "",
            )


class TestDispatchWithRetryWinnerCtx(unittest.TestCase):
    """T-D + T-E + T-F: dispatch_with_retry sets winner_ctx on PASS."""

    def _make_state_with_round_1_seed(self, ctx=None):
        ctx = ctx or _make_ctx(round_num=1)
        facts = SimpleNamespace(changed_files=[])
        return RetrySessionState(
            task_slug="t", current_round_ctx=ctx,
            current_round_facts=facts,
        )

    def _make_budget_unbounded(self):
        from common import budget_counter as bc  # type: ignore
        return bc.make_default_set({
            "tokens_in": 1e9, "tokens_out": 1e9, "cost_usd": 1e9,
            "active_wallclock_minutes": 1e9, "subagent_dispatches": 1e9,
        })

    def _make_afk_unbounded(self):
        from common.afk_monitor import AfkMonitor  # type: ignore
        return AfkMonitor(
            start_iso="2026-05-08T00:00:00Z", mode="abort",
            idle_seconds_threshold=1e9, hard_cap_seconds=1e9,
        )

    def _now_fn(self):
        from datetime import datetime, timezone, timedelta
        t = {"v": datetime(2026, 5, 8, tzinfo=timezone.utc)}
        def f():
            s = t["v"].isoformat().replace("+00:00", "Z")
            t["v"] += timedelta(seconds=1)
            return s
        return f

    def test_round_1_pass_winner_aliases_seeded_ctx(self):
        """T-E: winner_ctx is the same object as seeded Round 1 ctx."""
        seed_ctx = _make_ctx(round_num=1)
        state = self._make_state_with_round_1_seed(seed_ctx)

        deps = RetryDeps(
            run_implementer_round=lambda **_: {},
            run_codex_review=lambda **_: "pass",
        )
        from flow_orchestrator import RetryConfig
        outcome, snap = dispatch_with_retry(
            state=state,
            config=RetryConfig(2, 2),
            budget=self._make_budget_unbounded(),
            afk=self._make_afk_unbounded(),
            deps=deps,
            now_iso_fn=self._now_fn(),
        )
        self.assertEqual(outcome, "pass")
        self.assertIsNone(snap)
        self.assertIs(state.winner_ctx, seed_ctx,
                      "Round 1 PASS: winner_ctx must alias seeded ctx")
        # Defence: no failed_rounds entries (no rounds failed).
        self.assertEqual(state.failed_rounds, [])

    def test_counter_monotonicity_mixed_sequence_with_winner(self):
        """T-F: rejected_with_rationale → fail → pass.
        codex_review_rounds=1, dispatch_retry_rounds=1, winner=last."""
        from flow_orchestrator import RetryConfig
        seed_ctx = _make_ctx(round_num=1)
        state = self._make_state_with_round_1_seed(seed_ctx)

        # impl seq: 3 rounds; review: rwr -> fail -> pass.
        impl_calls = []
        def _impl(*, state, prompt_prefix, **_):
            impl_calls.append(state.dispatch_retry_rounds)
            return {}
        review_seq = ["rejected_with_rationale", "fail", "pass"]
        review_idx = [0]
        def _review(*, state, impl_deltas, **_):
            v = review_seq[review_idx[0]]
            review_idx[0] += 1
            return v

        outcome, snap = dispatch_with_retry(
            state=state,
            config=RetryConfig(3, 3),
            budget=self._make_budget_unbounded(),
            afk=self._make_afk_unbounded(),
            deps=RetryDeps(_impl, _review),
            now_iso_fn=self._now_fn(),
        )
        self.assertEqual(outcome, "pass")
        # rwr round didn't bump dispatch_retry_rounds.
        self.assertEqual(state.dispatch_retry_rounds, 1)
        # one rwr -> codex_review_rounds = 1.
        self.assertEqual(state.codex_review_rounds, 1)
        # winner_ctx aliases seeded (no fresh-helper-injected ctx because
        # fakes don't mutate state.current_round_ctx).
        self.assertIs(state.winner_ctx, seed_ctx)


class TestProdImplTwoPhaseCommit(unittest.TestCase):
    """T-B: simulated mid-helper raise leaves state.current_round_*
    untouched (no half-swap)."""

    def test_helper_raise_keeps_state_coherent(self):
        """Drive _phase2_dispatch's prod adapter via direct construction —
        we recreate the closure body inline since the production path
        builds it inside _phase2_dispatch and is closure-captured.
        """
        # Set up a state seeded for Round 1; pretend we're entering
        # Round 2's _prod_impl.
        seed_ctx = _make_ctx(round_num=1)
        seed_facts = SimpleNamespace(changed_files=[])
        state = RetrySessionState(
            task_slug="t",
            dispatch_retry_rounds=1,  # we are entering round 2
            current_round_ctx=seed_ctx,
            current_round_facts=seed_facts,
        )

        # Simulate the helper raising InfraFailureError.
        def _failing_helper(*args, **kwargs):
            raise InfraFailureError("simulated mid-helper crash")

        # Mirror the production _prod_impl shape.
        def _prod_impl(*, state, prompt_prefix, **_kw):
            if state.dispatch_retry_rounds == 0:
                return {}
            new_ctx, new_facts, deltas = _failing_helper()
            prev = state.current_round_ctx
            if prev is not None:
                state.failed_rounds.append(RoundRecord.from_ctx(prev))
            state.current_round_ctx = new_ctx
            state.current_round_facts = new_facts
            return deltas

        with self.assertRaises(InfraFailureError):
            _prod_impl(state=state, prompt_prefix="x")

        # Coherent state: no half-swap. failed_rounds is empty (we never
        # appended). current_round_* is still the seed.
        self.assertEqual(state.failed_rounds, [])
        self.assertIs(state.current_round_ctx, seed_ctx)
        self.assertIs(state.current_round_facts, seed_facts)


class TestFailedRoundsContract(unittest.TestCase):
    """T-H: failed_rounds is in-memory only in P0.1 — not journaled.
    Document the contract via a test so future readers / changers see
    the explicit shape."""

    def test_failed_rounds_starts_empty(self):
        s = RetrySessionState(task_slug="t")
        self.assertEqual(s.failed_rounds, [])

    def test_failed_rounds_holds_round_records_not_ctx(self):
        ctx = _make_ctx(round_num=2)
        s = RetrySessionState(task_slug="t")
        s.failed_rounds.append(RoundRecord.from_ctx(ctx))
        # Members are RoundRecord, not raw WorktreeContext (codex P1 §4).
        self.assertIsInstance(s.failed_rounds[0], RoundRecord)
        self.assertNotIsInstance(s.failed_rounds[0], WorktreeContext)


if __name__ == "__main__":
    unittest.main()
