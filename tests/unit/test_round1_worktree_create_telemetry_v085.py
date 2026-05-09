"""v0.8.5 codex-review I5 — Round 1 worktree_create telemetry.

Codex review I5: ``_dispatch_implementer_fresh_worktree`` (Round 2+)
emitted the ``worktree_create`` event but Round 1's worktree (created
in ``auto_dispatch_task``) had NO instrumentation. Most tasks only
run Round 1 → ADR Revisit trigger 1 ("worktree p50 >15s") was
permanently un-evaluatable.

Fix: ``auto_dispatch_task`` accepts optional ``telemetry_emit_fn``
callable; when supplied, brackets ``create_task_worktree`` with a
``timed_span`` and emits one ``worktree_create`` event with
``round_num=1``.

Tests:
- Constructor accepts the new kwarg + default None backward compat
- When supplied, exactly one worktree_create event emitted with
  round_num=1 + outcome (frozen schema) + duration_ms (real)
- worktree_id populated post-create
- Failure path (worktree create raises) still emits event with
  outcome=fail + fail_reason_raw
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_orchestrator as fo  # noqa: E402  type: ignore
from flow_contract import Contract  # noqa: E402  type: ignore


def _setup_repo(td: Path) -> Path:
    repo = td / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "master", "."],
                   cwd=repo, check=True)
    subprocess.run(
        ["git", "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "--allow-empty", "-m", "init", "-q"],
        cwd=repo, check=True,
    )
    (repo / ".flow" / "tasks" / "test-slug").mkdir(parents=True)
    return repo


def _make_contract() -> Contract:
    return Contract(
        contract_schema_version=1,
        autonomy_mode="auto",
        created_at="2026-05-09T00:00:00Z",
    )


def _make_manifest() -> "fo.TaskManifest":
    return fo.TaskManifest(
        id="t1", writes_declared=[], allowed_writes=[],
        out_of_scope=[], forbidden_hits=[], shared_hits=[],
    )


class AutoDispatchAcceptsTelemetryEmitFn(unittest.TestCase):
    def test_kwarg_default_none_backward_compat(self) -> None:
        # Smoke test that the kwarg name exists and default is None.
        import inspect
        sig = inspect.signature(fo.auto_dispatch_task)
        self.assertIn("telemetry_emit_fn", sig.parameters)
        self.assertIsNone(sig.parameters["telemetry_emit_fn"].default)


class AutoDispatchEmitsRound1WorktreeCreate(unittest.TestCase):
    def test_emits_one_worktree_create_event_round_1(self) -> None:
        with TemporaryDirectory() as td:
            repo = _setup_repo(Path(td))
            calls = []
            contract = _make_contract()
            manifest = _make_manifest()
            # Stub dispatch_fn so we don't actually invoke a subagent.
            dispatch_fn = mock.Mock()
            outcome = fo.auto_dispatch_task(
                slug="test-slug",
                task_idx=0,
                repo_root=repo,
                dispatch_fn=dispatch_fn,
                contract=contract,
                manifest=manifest,
                run_id="r1",
                contract_path=repo / ".flow" / "tasks" / "test-slug" / "contract.json",
                contract_hash="deadbeef",
                integration_target="master",
                telemetry_emit_fn=lambda **kw: calls.append(kw),
            )
            self.assertIsNotNone(outcome)
            wc = [c for c in calls if c.get("phase") == "worktree_create"]
            self.assertEqual(len(wc), 1)
            ev = wc[0]
            self.assertEqual(ev["round_num"], 1)
            self.assertIn(ev["outcome"], {"pass", "fail", "skip", None})
            self.assertEqual(ev["outcome"], "pass")
            self.assertIsInstance(ev["duration_ms"], int)
            self.assertGreaterEqual(ev["duration_ms"], 0)
            # worktree_id populated post-create.
            self.assertIsNotNone(ev["worktree_id"])

    def test_no_emit_fn_no_events_no_crash(self) -> None:
        """Backward compat: legacy callers without telemetry_emit_fn
        must continue to work."""
        with TemporaryDirectory() as td:
            repo = _setup_repo(Path(td))
            contract = _make_contract()
            manifest = _make_manifest()
            dispatch_fn = mock.Mock()
            # Must not raise.
            outcome = fo.auto_dispatch_task(
                slug="test-slug",
                task_idx=0,
                repo_root=repo,
                dispatch_fn=dispatch_fn,
                contract=contract,
                manifest=manifest,
                run_id="r1",
                contract_path=repo / ".flow" / "tasks" / "test-slug" / "contract.json",
                contract_hash="deadbeef",
                integration_target="master",
            )
            self.assertIsNotNone(outcome)


class WorktreeCreateFailureEmitsFail(unittest.TestCase):
    def test_create_failure_still_emits_event_with_fail_outcome(self) -> None:
        with TemporaryDirectory() as td:
            repo = _setup_repo(Path(td))
            calls = []
            contract = _make_contract()
            manifest = _make_manifest()
            dispatch_fn = mock.Mock()
            # Patch create_task_worktree to raise after telemetry
            # bracket starts.
            with mock.patch.object(
                fo, "create_task_worktree",
                side_effect=subprocess.CalledProcessError(1, "git worktree add"),
            ):
                with self.assertRaises(subprocess.CalledProcessError):
                    fo.auto_dispatch_task(
                        slug="test-slug",
                        task_idx=0,
                        repo_root=repo,
                        dispatch_fn=dispatch_fn,
                        contract=contract,
                        manifest=manifest,
                        run_id="r1",
                        contract_path=repo / ".flow" / "tasks" / "test-slug" / "contract.json",
                        contract_hash="deadbeef",
                        integration_target="master",
                        telemetry_emit_fn=lambda **kw: calls.append(kw),
                    )
            wc = [c for c in calls if c.get("phase") == "worktree_create"]
            self.assertEqual(len(wc), 1)
            ev = wc[0]
            self.assertEqual(ev["outcome"], "fail")
            self.assertIsNotNone(ev["fail_reason_raw"])


if __name__ == "__main__":
    unittest.main()
