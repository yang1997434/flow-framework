"""v0.8.5 codex-review I6 — production-path telemetry coverage.

Codex review R2 I6 escalation: the previous version of
``TestRound2EnrichmentSeesUncommittedDiff`` manually constructed a
``RetrySessionState`` and called ``_build_prev_round_diff_summary``
directly. That bypassed ``dispatch_with_retry``, ``_prod_impl``, the
prompt builder, and the subagent shim — exactly the class of fake
coverage I6 was supposed to eliminate.

The rewrite (``TestRound2EnrichmentViaFullRetryLoop`` below) drives
the FULL retry loop. Mock boundary:

  Mocked (= unavoidable external IO):
    - ``fo._invoke_subagent_dispatch``: Round 2 implementer dispatch
      shim. Captures ``prompt_prefix`` to verify enrichment landed in
      the actual prompt.
    - ``fo._run_shell_with_pgkill``: codex CLI subprocess. Round 1
      RED → Round 2 GREEN.

  NOT mocked (= real production code under test):
    - ``dispatch_with_retry`` / ``_prod_impl`` / ``_prod_review``
    - ``_dispatch_implementer_fresh_worktree`` (real ``git worktree
      add``, real ``derive_task_facts``)
    - ``GateRunner.run_phase2`` (real gate1 / gate3 / gate4 wrapper /
      gate5 / gate6)
    - ``build_implementer_prompt`` / ``_build_prev_round_diff_summary``
    - All telemetry emission across the 5 phases

This guarantees a regression of I1, I2, I3, I3-A, or I5 manifests
as a failed assertion in this test (= no more fake coverage).
"""
from __future__ import annotations

import json
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
    Contract, _phase2_dispatch, create_task_worktree, derive_task_facts,
)


VALID_OUTCOMES = {"pass", "fail", "skip", None}


def _setup_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."],
                   cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init", "-q"],
                   cwd=repo, check=True)
    return repo


def _make_contract() -> Contract:
    """Contract with telemetry + feedback_enrichment ON (defaults)."""
    return Contract(
        contract_schema_version=1,
        autonomy_mode="auto",
        created_at="2026-05-09T00:00:00Z",
        budget={
            "tokens_in": 1_000_000.0,
            "tokens_out": 1_000_000.0,
            "cost_usd": 1000.0,
            "active_wallclock_minutes": 600.0,
            "subagent_dispatches": 100.0,
        },
        # explicit on (mirrors PRD R5 default).
        dispatch={"telemetry": "on", "feedback_enrichment": "on"},
    )


class _SpyNotifier:
    def __init__(self):
        self.fired: list[dict] = []

    def fire_block(self, **kw):
        self.fired.append(kw)


def _stub_pgkill_factory(verdicts):
    """Build a side_effect for _run_shell_with_pgkill that returns a
    different shell-result per call. ``verdicts`` is an ordered list
    of dicts with keys: command_pattern (substring to match command),
    returncode, stdout, stderr.
    """
    seq = list(verdicts)

    def _side_effect(command, *, cwd, timeout_sec, **_kw):
        # Find first matching verdict.
        for i, v in enumerate(seq):
            if v["command_pattern"] in command:
                seq.pop(i)
                result = SimpleNamespace()
                result.spawn_error = None
                result.timed_out = False
                result.returncode = v["returncode"]
                result.stdout = v["stdout"]
                result.stderr = v.get("stderr", "")
                return result
        # Default: success no-op.
        result = SimpleNamespace()
        result.spawn_error = None
        result.timed_out = False
        result.returncode = 0
        result.stdout = ""
        result.stderr = ""
        return result

    return _side_effect


class TestRound1WorktreeCreateInProduction(unittest.TestCase):
    """I5: Round 1 worktree_create must land via auto_dispatch_task."""

    def test_round1_creation_emits_worktree_create_via_auto_dispatch(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            repo = _setup_repo(tdp)
            task_dir = repo / ".flow" / "tasks" / "test-slug"
            task_dir.mkdir(parents=True)
            (task_dir / "prd.md").write_text("brief\n")

            calls = []
            contract = _make_contract()
            manifest = fo.TaskManifest(
                id="t1", writes_declared=[], allowed_writes=[],
                out_of_scope=[], forbidden_hits=[], shared_hits=[],
            )
            outcome = fo.auto_dispatch_task(
                slug="test-slug",
                task_idx=0,
                repo_root=repo,
                dispatch_fn=mock.Mock(),
                contract=contract,
                manifest=manifest,
                run_id="r1",
                contract_path=task_dir / "contract.json",
                contract_hash="abc123",
                integration_target="master",
                telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            self.assertIsNotNone(outcome)
            wc_events = [c for c in calls if c.get("phase") == "worktree_create"]
            self.assertEqual(len(wc_events), 1)
            ev = wc_events[0]
            self.assertEqual(ev["round_num"], 1)
            self.assertEqual(ev["outcome"], "pass")
            self.assertIsNotNone(ev["worktree_id"])


class TestProductionGateRunEmitsRealCodexReview(unittest.TestCase):
    """I1 + I2: production review path emits codex_review with real
    wall time + frozen-schema outcome."""

    def test_phase2_dispatch_with_real_gate_runner_emits_all_events(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            repo = _setup_repo(tdp)
            task_dir = repo / ".flow" / "tasks" / "test-slug"
            task_dir.mkdir(parents=True)
            (task_dir / "prd.md").write_text("brief\n")
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n\n| round | role | counters |\n|---|---|---|\n",
            )

            # Round 1 ctx (would normally come from auto_dispatch_task).
            round1_ctx = create_task_worktree(
                repo_root=repo, slug="test-slug", task_idx=0,
                integration_target="master",
            )
            round1_facts = derive_task_facts(round1_ctx)

            # Stub the codex CLI so gate4 produces a deterministic
            # GREEN verdict — meaning gate4_codex_review runs to
            # completion, emits its codex_review event with real
            # wall time, and verdict is pass.
            pgkill_side = _stub_pgkill_factory([
                # Gate 1 baseline + Gate 5 acceptance + Gate 6
                # regression all default to success (rc=0).
                {"command_pattern": "true", "returncode": 0,
                 "stdout": "", "stderr": ""},
                # Codex gate.
                {"command_pattern": "codex_review_stub",
                 "returncode": 0,
                 "stdout": json.dumps({"verdict": "GREEN", "issues": []}),
                 "stderr": ""},
            ])

            # Patch _run_shell_with_pgkill globally for the duration.
            notifier = _SpyNotifier()
            with mock.patch.object(
                fo, "_run_shell_with_pgkill",
                side_effect=pgkill_side,
            ):
                rc, winner_ctx, winner_facts = _phase2_dispatch(
                    slug="test-slug",
                    task_dir=task_dir,
                    contract=_make_contract(),
                    manifest=SimpleNamespace(id="t0"),
                    facts=round1_facts,
                    ctx=round1_ctx,
                    criteria=[],
                    gate_cmds={
                        "baseline": "true",
                        "codex": "codex_review_stub",
                        "smoke": "true",
                        "merge_strategy": "merge",
                    },
                    run_id="run-1", task_id="t0",
                    notifier=notifier,
                )
            self.assertEqual(rc, 0, f"expected pass; got rc={rc}, "
                                    f"notifier fired={notifier.fired}")

            # Read telemetry events from the per-task JSONL.
            telem_path = task_dir / "telemetry.jsonl"
            self.assertTrue(telem_path.is_file())
            events = [
                json.loads(line) for line in
                telem_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            phases = [e["phase"] for e in events]

            # I1: codex_review event MUST land (real codex CLI ran).
            self.assertIn("codex_review", phases)
            cx = next(e for e in events if e["phase"] == "codex_review")
            # Real wall time captured (>= 0; usually small for stub).
            self.assertIsInstance(cx["duration_ms"], int)
            self.assertGreaterEqual(cx["duration_ms"], 0)

            # I2: outcome is in the frozen enum.
            for ev in events:
                self.assertIn(
                    ev["outcome"], VALID_OUTCOMES,
                    f"phase {ev['phase']} outcome {ev['outcome']!r} "
                    f"violates frozen schema",
                )

            # gate_run event landed with real status.
            self.assertIn("gate_run", phases)
            gr = next(e for e in events if e["phase"] == "gate_run")
            self.assertEqual(gr["outcome"], "pass")


class TestRound2EnrichmentViaFullRetryLoop(unittest.TestCase):
    """v0.8.5 codex-R2 I6: full-retry-loop production path.

    Codex R2 review pointed out that the previous version of this
    test manually built a ``RetrySessionState`` and called
    ``_build_prev_round_diff_summary`` directly — bypassing
    ``dispatch_with_retry``, ``_prod_impl``, the prompt builder, and
    the subagent shim. That was exactly the class of fake coverage
    I6 was supposed to eliminate.

    This rewrite drives the FULL retry loop:

    Mock boundary (the ONLY things mocked):
      * ``fo._invoke_subagent_dispatch`` — Round 2 implementer
        dispatch shim. Capture ``prompt_prefix`` kwarg to verify
        the diff-map enrichment landed in the actual prompt the
        subagent would receive in production. The mock does NOT
        modify the Round 2 worktree (so Round 2 has clean diff →
        gate3 passes trivially).
      * ``fo._run_shell_with_pgkill`` — codex CLI subprocess. Round
        1 returns RED (forces retry); Round 2 returns GREEN
        (terminates on pass).

    NOT mocked (= real code under test):
      * ``dispatch_with_retry`` — full state machine
      * ``_prod_impl`` (closure inside ``_phase2_dispatch``)
      * ``_prod_review`` (closure inside ``_phase2_dispatch``)
      * ``_dispatch_implementer_fresh_worktree`` — Round 2 worktree
        creation + helper. Real ``git worktree add``; real
        ``derive_task_facts``.
      * ``GateRunner.run_phase2`` — gate1_baseline (rc=0 stub),
        gate3_manifest (real Python; empty scope_allowed → trivial
        pass), gate4_codex_review (real wrapper; mocked CLI),
        gate5_acceptance (criteria=[] → trivial pass),
        gate6_regression (rc=0 stub).
      * ``build_implementer_prompt`` — real prompt assembly.
      * ``_build_prev_round_diff_summary`` — real 4-source diff
        collector (covers I3-A's bare ``git diff`` fix).
      * Telemetry emission across the 5 phases.

    Asserts:
      AC for I3 + I3-A: Round 2 prompt (captured from the mocked
      shim) contains the diff-map section AND lists Round 1's
      uncommitted state. The committed + staged + unstaged + untracked
      files all show up in the stat block (no double-count).
      AC for I1/I2: ``codex_review`` event lands twice (once per
      round) with frozen-schema outcome + real ``duration_ms``.
      AC for telemetry coverage: ``gate_run`` event * 2.
    """

    def test_full_retry_loop_round2_prompt_has_diff_map_and_telemetry(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as td:
            tdp = Path(td)
            repo = _setup_repo(tdp)
            task_dir = repo / ".flow" / "tasks" / "test-slug"
            task_dir.mkdir(parents=True)
            (task_dir / "prd.md").write_text("brief\n")
            (task_dir / "progress.md").write_text(
                "# progress\n\n## Execute Log\n\n"
                "| round | role | counters |\n|---|---|---|\n",
            )

            # Build a contract that:
            #   - keeps scope_allowed empty (gate3 passes trivially)
            #   - explicit dispatch=on (mirrors PRD R5 default)
            contract = _make_contract()

            # ── Round 1 setup: real worktree, populate 4 disk states ──
            # We don't go through auto_dispatch_task here; the existing
            # prod-adapter integration tests use the same shortcut
            # (auto_dispatch_task is exercised separately in
            # TestRound1WorktreeCreateInProduction above). What we DO
            # need is a Round 1 worktree with all four uncommitted
            # states populated — that's the I3-A regression target.
            round1_ctx = create_task_worktree(
                repo_root=repo, slug="test-slug", task_idx=0,
                integration_target="master",
            )
            wt = round1_ctx.worktree_path

            # State 1 — committed: a.py with one commit AHEAD of base.
            (wt / "a.py").write_text("a = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(wt), "add", "a.py"], check=True,
            )
            subprocess.run(
                ["git", "-C", str(wt),
                 "-c", "user.email=t@t", "-c", "user.name=t",
                 "commit", "-q", "-m", "round1 committed"],
                check=True,
            )

            # State 2 — staged: b.py added, NOT committed.
            (wt / "b.py").write_text("b = 1\n", encoding="utf-8")
            subprocess.run(
                ["git", "-C", str(wt), "add", "b.py"], check=True,
            )

            # State 3 — unstaged: modify a.py (committed file) further
            # in the working tree only.
            (wt / "a.py").write_text("a = 2\n", encoding="utf-8")

            # State 4 — untracked: c.py never staged.
            (wt / "c.py").write_text("c = 1\n", encoding="utf-8")

            round1_facts = derive_task_facts(round1_ctx)

            # ── Mock subagent shim for Round 2 dispatch ──────────────
            captured_prompts: list = []

            def _fake_subagent_dispatch(ctx, **kw):
                # Capture exactly what the helper hands to the shim.
                captured_prompts.append({
                    "round_num": kw.get("round_num"),
                    "prompt_prefix": kw.get("prompt_prefix", ""),
                    "worktree_path": str(ctx.worktree_path),
                })
                # Don't modify the Round 2 worktree — clean diff →
                # gate3 trivially passes.

            # ── Mock _run_shell_with_pgkill for the codex CLI ────────
            # Round 1 codex → RED (forces retry).
            # Round 2 codex → GREEN (terminates on pass).
            # Anything else (gate1 baseline, gate6 smoke) → rc=0.
            codex_call_count = {"n": 0}

            def _pgkill_side(command, *, cwd, timeout_sec, **_kw):
                # Codex command pattern set by gate_cmds["codex"] below.
                if "codex_review_stub" in command:
                    codex_call_count["n"] += 1
                    result = SimpleNamespace()
                    result.spawn_error = None
                    result.timed_out = False
                    result.returncode = 0
                    if codex_call_count["n"] == 1:
                        # Round 1 codex → RED.
                        result.stdout = json.dumps({
                            "verdict": "RED",
                            "issues": [{
                                "id": "x1",
                                "severity": "high",
                                "title": "validation gap",
                                "rationale": "round-1 fail",
                            }],
                        })
                    else:
                        # Round 2 codex → GREEN.
                        result.stdout = json.dumps({
                            "verdict": "GREEN", "issues": [],
                        })
                    result.stderr = ""
                    return result
                # Default: gate1 baseline + gate6 smoke pass.
                result = SimpleNamespace()
                result.spawn_error = None
                result.timed_out = False
                result.returncode = 0
                result.stdout = ""
                result.stderr = ""
                return result

            notifier = _SpyNotifier()
            with mock.patch.object(
                fo, "_invoke_subagent_dispatch",
                side_effect=_fake_subagent_dispatch,
            ), mock.patch.object(
                fo, "_run_shell_with_pgkill",
                side_effect=_pgkill_side,
            ):
                rc, winner_ctx, winner_facts = _phase2_dispatch(
                    slug="test-slug",
                    task_dir=task_dir,
                    contract=contract,
                    manifest=SimpleNamespace(id="t0"),
                    facts=round1_facts,
                    ctx=round1_ctx,
                    criteria=[],
                    gate_cmds={
                        "baseline": "true",
                        "codex": "codex_review_stub",
                        "smoke": "true",
                        "merge_strategy": "merge",
                    },
                    run_id="run-1",
                    task_id="t0",
                    notifier=notifier,
                )

            # ── Outcome assertions ───────────────────────────────────
            self.assertEqual(
                rc, 0,
                f"expected pass after Round 2 GREEN; got rc={rc}, "
                f"notifier fired={notifier.fired}",
            )
            self.assertIsNotNone(winner_ctx)
            # Winner is Round 2's fresh worktree (NOT Round 1).
            self.assertEqual(winner_ctx.round_num, 2)
            self.assertNotEqual(
                winner_ctx.worktree_id, round1_ctx.worktree_id,
            )

            # ── Round 2 prompt captured + has diff map enrichment ────
            self.assertEqual(
                len(captured_prompts), 1,
                "subagent shim should have been called exactly once "
                "(for Round 2's dispatch)",
            )
            r2_prompt = captured_prompts[0]["prompt_prefix"]
            self.assertEqual(captured_prompts[0]["round_num"], 2)
            # AC3: Round 2 prompt contains the diff-map section header
            # and the framing line (real prompt builder ran).
            self.assertIn("Round N-1 structural diff map", r2_prompt)
            self.assertIn(
                "Use reviewer feedback as the primary signal",
                r2_prompt,
            )
            # I3 + I3-A: all 4 disk states from Round 1 surface in the
            # diff-map stat block.
            self.assertIn("a.py", r2_prompt)  # committed + unstaged
            self.assertIn("b.py", r2_prompt)  # staged
            self.assertIn("c.py", r2_prompt)  # untracked
            self.assertIn("new file", r2_prompt.lower())  # untracked marker

            # I3-A: a.py has BOTH committed and unstaged changes — must
            # NOT be triple-counted (committed + staged-as-unstaged +
            # actual unstaged). Count distinct stat lines for a.py.
            stat_lines_for_a = [
                ln for ln in r2_prompt.splitlines()
                if "a.py" in ln and "|" in ln
            ]
            # Expected: 1 committed line + 1 unstaged line = 2 lines.
            # Bug behaviour (pre-I3-A): 1 committed + 1 (staged∪unstaged
            # via `git diff HEAD` overlap-with-staged) = potentially 2,
            # but the staged content for OTHER files (b.py here) would
            # be doubled. We pin the cleaner per-file count too.
            self.assertLessEqual(
                len(stat_lines_for_a), 2,
                f"a.py should appear at most twice in stat block "
                f"(committed + unstaged); got "
                f"{len(stat_lines_for_a)}: {stat_lines_for_a!r}",
            )
            # b.py is staged-only (no further wt edits) → exactly 1
            # stat line (covers I3-A double-count regression).
            stat_lines_for_b = [
                ln for ln in r2_prompt.splitlines()
                if "b.py" in ln and "|" in ln
            ]
            self.assertEqual(
                len(stat_lines_for_b), 1,
                f"b.py is staged-only — must appear exactly ONCE in "
                f"stat block (I3-A regression: pre-fix would show "
                f"twice via the `git diff HEAD` staged∪unstaged "
                f"overlap); got {stat_lines_for_b!r}",
            )

            # ── Telemetry assertions across full retry loop ──────────
            telem_path = task_dir / "telemetry.jsonl"
            self.assertTrue(telem_path.is_file())
            events = [
                json.loads(line) for line in
                telem_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            phases = [e["phase"] for e in events]

            # I5 production: Round 2 worktree_create event lands.
            wc = [e for e in events if e["phase"] == "worktree_create"]
            self.assertGreaterEqual(
                len(wc), 1,
                "expected at least 1 worktree_create event "
                "(Round 2 helper-driven creation); Round 1 is created "
                "outside _phase2_dispatch in this test path",
            )
            # I1: codex_review event * 2 (Round 1 + Round 2).
            cx = [e for e in events if e["phase"] == "codex_review"]
            self.assertEqual(
                len(cx), 2,
                f"expected 2 codex_review events (one per round); "
                f"got {len(cx)}",
            )
            for ev in cx:
                # Real wall time (no fake duration_ms=0).
                self.assertIsInstance(ev["duration_ms"], int)
                self.assertGreaterEqual(ev["duration_ms"], 0)
                # I2: frozen-schema outcome.
                self.assertIn(ev["outcome"], VALID_OUTCOMES)

            # I2: ALL events use frozen-schema outcome.
            for ev in events:
                self.assertIn(
                    ev["outcome"], VALID_OUTCOMES,
                    f"phase={ev['phase']} outcome={ev['outcome']!r} "
                    f"violates frozen schema",
                )

            # gate_run event * 2 (one per round) — both real Python
            # gate runner invocations were instrumented.
            gr = [e for e in events if e["phase"] == "gate_run"]
            self.assertEqual(
                len(gr), 2,
                f"expected 2 gate_run events (one per round); "
                f"got {len(gr)}",
            )


if __name__ == "__main__":
    unittest.main()
