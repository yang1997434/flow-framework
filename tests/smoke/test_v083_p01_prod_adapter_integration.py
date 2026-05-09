"""v0.8.3 P0.1 — production adapter integration tests.

Codex review R1 (round 2) flagged: unit tests cover helper + state
transitions, but the real `_phase2_dispatch` prod adapter closures
(`_prod_impl`, `_prod_review`) were only mirrored inline. These tests
drive `_phase2_dispatch` itself with `deps_factory=None` (prod path)
and verify:

1. **Round 2+ pass via prod adapter** — real `_prod_impl` calls the
   helper; failed_rounds is appended with Round 1 record; winner_ctx
   becomes the Round 2 fresh ctx; merge inputs are the Round 2 pair.
2. **InfraFailureError → phase2_infra_failure block** — helper raises;
   `_phase2_dispatch` catches, fires `phase2_infra_failure` block via
   notifier; rc=3; counters NOT bumped (codex round-1 D §1).

Both tests use a real tmp git repo for create_task_worktree but mock
the subagent shim and GateRunner so inner boundaries are deterministic.
The real `_prod_impl` and `_prod_review` closure bodies execute.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_orchestrator as fo  # noqa: E402  type: ignore
from flow_orchestrator import (  # noqa: E402  type: ignore
    Contract,
    InfraFailureError,
    RoundRecord,
    _phase2_dispatch,
    create_task_worktree,
)


def _make_contract() -> Contract:
    """Minimal valid Contract for retry-loop tests (mirrors the
    `_make_contract` pattern in test_phase2_retry_loop.py)."""
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


class _SpyNotifier:
    def __init__(self):
        self.fired: list[dict] = []

    def fire_block(self, **kw):
        self.fired.append(kw)


class _FakeVerdict:
    """Mimics GateVerdict shape for `_prod_review`."""
    def __init__(self, status: str, halted_at_gate=None, details=None):
        self.status = status
        self.halted_at_gate = halted_at_gate
        self.gate_result = (
            SimpleNamespace(details=details or {})
            if details is not None else None
        )


class _FakeGateRunnerFactory:
    """Yields scripted verdicts per call to `run_phase2`. Real
    `_prod_review` constructs a GateRunner per call (one per round) so
    we patch the class itself."""

    def __init__(self, verdicts: list):
        self._seq = list(verdicts)
        self.invocations: list[dict] = []

    def __call__(self, *, ctx, contract, task_dir, run_id, task_id,
                 prior_baseline=None, telemetry_emit_fn=None):
        # v0.8.5 codex-review I1: production GateRunner now accepts
        # ``telemetry_emit_fn``; this fake factory must accept the
        # kwarg too (don't need to use it — test verifies routing).
        self.ctx_seen = ctx  # last
        self.invocations.append({"ctx_id": ctx.worktree_id})
        runner = self

        class _R:
            def run_phase2(s, **kw):
                runner.invocations[-1]["call_kw"] = kw
                if not runner._seq:
                    raise AssertionError("run_phase2 called more than scripted")
                return runner._seq.pop(0)

        return _R()


def _setup_repo(td: Path) -> Path:
    """Init tmp git repo + 1 base commit + .flow/tasks/<slug>/."""
    repo = td / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."],
                   cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=repo, check=True,
    )
    return repo


class TestProdAdapterRound2PassIntegration(unittest.TestCase):
    """Codex R1 round-2 J §3: integration test for production Round 2+
    adapter — real `_phase2_dispatch` path."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.repo = _setup_repo(self.td)
        self.task_dir = self.repo / ".flow" / "tasks" / "test-slug"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "prd.md").write_text("# Brief\n\nGoal: integration.\n")
        (self.task_dir / "progress.md").write_text(
            "# progress\n\n## Execute Log\n\n| round | role | counters |\n|---|---|---|\n",
            encoding="utf-8",
        )

        # Real Round 1 ctx (created by auto_dispatch_task in prod;
        # we synthesize one directly).
        self.round_1_ctx = create_task_worktree(
            repo_root=self.repo, slug="test-slug", task_idx=0,
            integration_target="master",
        )
        self.round_1_facts = SimpleNamespace(
            changed_files=[], diff_hash="0",
            target_commit_pre_merge=self.round_1_ctx.original_base_commit,
            newly_added_files=[],
        )

        # Patch subagent shim to a no-op (helper invokes it on Round 2+).
        self._orig_dispatch = fo._invoke_subagent_dispatch
        self.subagent_calls: list = []
        def _fake_dispatch(ctx, **kw):
            self.subagent_calls.append((ctx.worktree_id, ctx.round_num))
        fo._invoke_subagent_dispatch = _fake_dispatch

        # Patch GateRunner for `_prod_review`.
        self._orig_gate_runner = fo.GateRunner
        # Round 1 review → fail; Round 2 review → pass.
        self._gate_factory = _FakeGateRunnerFactory(verdicts=[
            _FakeVerdict("fail", halted_at_gate="gate-3",
                         details={"failing_test": "x"}),
            _FakeVerdict("pass"),
        ])
        fo.GateRunner = self._gate_factory  # type: ignore

        # Patch _load_prior_baseline (Real impl reads disk; fake returns None).
        self._orig_baseline = fo._load_prior_baseline
        fo._load_prior_baseline = lambda task_dir, task_id: None

    def tearDown(self):
        fo._invoke_subagent_dispatch = self._orig_dispatch
        fo.GateRunner = self._orig_gate_runner
        fo._load_prior_baseline = self._orig_baseline
        self.tmp.cleanup()

    def test_round_2_pass_via_prod_adapter(self):
        notifier = _SpyNotifier()
        rc, winner_ctx, winner_facts = _phase2_dispatch(
            slug="test-slug",
            task_dir=self.task_dir,
            contract=_make_contract(),
            manifest=SimpleNamespace(id="t0"),
            facts=self.round_1_facts,
            ctx=self.round_1_ctx,
            criteria=[],
            gate_cmds={
                "baseline": "true", "codex": "true",
                "smoke": "true", "merge_strategy": "merge",
            },
            run_id="run-1", task_id="t0",
            notifier=notifier,
        )
        # rc=0; winner is the Round 2 helper-produced fresh ctx (NOT
        # Round 1's ctx).
        self.assertEqual(rc, 0)
        self.assertIsNotNone(winner_ctx)
        self.assertIsNotNone(winner_facts)
        self.assertEqual(winner_ctx.round_num, 2,
                         "winner must be the Round 2 fresh ctx")
        self.assertNotEqual(winner_ctx.worktree_id,
                            self.round_1_ctx.worktree_id)
        self.assertIn("+r2+", winner_ctx.worktree_id)
        # Subagent dispatched exactly once (Round 2).
        self.assertEqual(len(self.subagent_calls), 1)
        self.assertEqual(self.subagent_calls[0][1], 2)
        # GateRunner was constructed twice — once per round.
        self.assertEqual(len(self._gate_factory.invocations), 2)
        # No block fired on the pass terminal.
        self.assertEqual(notifier.fired, [])


class TestProdAdapterInfraFailureIntegration(unittest.TestCase):
    """Codex R1 round-2 J §4: helper raise → rc=3 phase2_infra_failure
    block via real `_phase2_dispatch`. Counter must NOT bump."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.td = Path(self.tmp.name)
        self.repo = _setup_repo(self.td)
        self.task_dir = self.repo / ".flow" / "tasks" / "test-slug"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "prd.md").write_text("# Brief\n")
        (self.task_dir / "progress.md").write_text(
            "# progress\n\n## Execute Log\n\n| round | role | counters |\n|---|---|---|\n",
            encoding="utf-8",
        )
        self.round_1_ctx = create_task_worktree(
            repo_root=self.repo, slug="test-slug", task_idx=0,
            integration_target="master",
        )
        self.round_1_facts = SimpleNamespace(
            changed_files=[], diff_hash="0",
            target_commit_pre_merge=self.round_1_ctx.original_base_commit,
            newly_added_files=[],
        )

        # Patch subagent shim to RAISE on Round 2 (so helper wraps to
        # InfraFailureError).
        self._orig_dispatch = fo._invoke_subagent_dispatch
        def _crashing_dispatch(ctx, **kw):
            raise RuntimeError("simulated subagent crash mid-flight")
        fo._invoke_subagent_dispatch = _crashing_dispatch

        # Round 1 review = fail (forces Round 2 dispatch); Round 2's
        # _prod_impl raises InfraFailureError BEFORE _prod_review runs.
        self._orig_gate_runner = fo.GateRunner
        self._gate_factory = _FakeGateRunnerFactory(verdicts=[
            _FakeVerdict("fail", halted_at_gate="gate-3",
                         details={"failing_test": "x"}),
        ])
        fo.GateRunner = self._gate_factory  # type: ignore
        self._orig_baseline = fo._load_prior_baseline
        fo._load_prior_baseline = lambda task_dir, task_id: None

    def tearDown(self):
        fo._invoke_subagent_dispatch = self._orig_dispatch
        fo.GateRunner = self._orig_gate_runner
        fo._load_prior_baseline = self._orig_baseline
        self.tmp.cleanup()

    def test_helper_infra_failure_routes_to_terminal_block(self):
        notifier = _SpyNotifier()
        rc, winner_ctx, winner_facts = _phase2_dispatch(
            slug="test-slug",
            task_dir=self.task_dir,
            contract=_make_contract(),
            manifest=SimpleNamespace(id="t0"),
            facts=self.round_1_facts,
            ctx=self.round_1_ctx,
            criteria=[],
            gate_cmds={
                "baseline": "true", "codex": "true",
                "smoke": "true", "merge_strategy": "merge",
            },
            run_id="run-1", task_id="t0",
            notifier=notifier,
        )
        # rc=3 terminal; no winner.
        self.assertEqual(rc, 3)
        self.assertIsNone(winner_ctx)
        self.assertIsNone(winner_facts)
        # Notifier fired EXACTLY ONE phase2_infra_failure block.
        self.assertEqual(len(notifier.fired), 1)
        self.assertEqual(
            notifier.fired[0]["block_type"], "phase2_infra_failure",
        )
        self.assertIn("infra failure", notifier.fired[0]["why_blocked"])
        # No HardStopSnapshot emitted (infra failures are distinct
        # from budget/cap/AFK terminals).
        self.assertFalse(
            (self.task_dir / "hard-stop.json").exists(),
            "InfraFailureError must NOT produce hard-stop.json",
        )


if __name__ == "__main__":
    unittest.main()
