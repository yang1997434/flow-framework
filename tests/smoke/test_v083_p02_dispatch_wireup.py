"""v0.8.3 P0.2 — integration tests for dispatch shim wire-up of
``prompt_prefix`` via the file-based transport.

Two integration tests:

* ``test_round1_auto_dispatch_passes_prefix_through`` — exercises
  ``_cmd_auto_execute`` Round 1 path. Asserts the prefix file is
  written under ``<repo_root>/.flow/.runtime/<slug>+<task_id>+r1/
  dispatch_prefix.txt`` AND its content embeds the K-class sentinel
  prohibition AND the file path is NOT recorded in
  ``TaskFacts.changed_files`` / ``newly_added_files`` (the file lives
  outside the worktree, so a manifest_violation row 4 is impossible).

* ``test_round2_fresh_worktree_passes_prefix_through`` — exercises the
  Round 2+ helper ``_dispatch_implementer_fresh_worktree``. Asserts the
  prefix file lands in the ``+r2`` runtime directory AND its content
  embeds the synthetic reviewer feedback that the round-2 prompt
  composes via ``build_implementer_prompt(reviewer_feedback=...)``.

Both integration tests stand up a real tmp git repo (init + initial
commit on master) so the worktree creation + lock + auto_engaged
boundary writes have a real working tree to operate on.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _git(*args, cwd):
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True,
        capture_output=True, text=True,
    )


def _make_tmp_repo(tmp: Path) -> Path:
    """Initialise a minimal git repo with a master branch + initial commit.

    Returns the repo root path.
    """
    repo = tmp / "repo"
    repo.mkdir()
    _git("init", "-b", "master", cwd=repo)
    _git("config", "user.email", "test@example.com", cwd=repo)
    _git("config", "user.name", "Test User", cwd=repo)
    (repo / "README.md").write_text("test\n", encoding="utf-8")
    _git("add", "README.md", cwd=repo)
    _git("commit", "-m", "init", cwd=repo)
    # .claude/worktrees/ dir for round-discriminated worktree creation.
    (repo / ".claude" / "worktrees").mkdir(parents=True)
    return repo


class TestRound1AutoDispatchPrefixWireUp(unittest.TestCase):
    """Round 1 path: ``_cmd_auto_execute`` builds prefix + passes it
    through ``auto_dispatch_task`` → dispatch_fn → ``invoke``."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p02-r1-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._orig_cwd = os.getcwd()
        self._orig_env = os.environ.get("FLOW_SUBAGENT_DISPATCH_CMD")
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        sys.modules.pop("flow_subagent_dispatch", None)
        sys.modules.pop("flow_orchestrator", None)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        if self._orig_env is not None:
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = self._orig_env

    def test_round1_auto_dispatch_passes_prefix_through(self):
        repo = _make_tmp_repo(self.tmp)

        # Minimal dispatch template: invoke writes the prefix file BEFORE
        # the subagent would run (file-based transport). The template
        # also needs `cat $prompt_prefix_file` semantics — but for the
        # integration test we just need to assert the file landed at the
        # expected path. Use a touch-style template that ALSO references
        # `{prompt_prefix_file}` so fail-closed is satisfied.
        marker = self.tmp / "round1-dispatch-marker"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {marker.as_posix()}"
        )

        # Build a minimal task dir + contract + manifest layout that
        # ``_cmd_auto_execute`` understands. The simplest path is to
        # exercise ``auto_dispatch_task`` directly with a fake
        # dispatch_fn that mirrors the production wire (calls
        # ``_invoke_subagent_dispatch`` → ``flow_subagent_dispatch.invoke``)
        # but skips the build_plan / run_id / contract scaffolding. That
        # keeps the integration test focused on the wire boundary.
        from flow_orchestrator import (
            auto_dispatch_task,
            _invoke_subagent_dispatch,
            derive_task_facts,
            TaskManifest,
        )
        from flow_contract import Contract, CONTRACT_SCHEMA_VERSION
        from dispatch_template import (
            build_implementer_prompt, K_CLASS_SENTINEL_PROHIBITION,
        )

        slug = "p02demo"
        task_id = "T1"
        task_dir = repo / ".flow" / "tasks" / slug
        task_dir.mkdir(parents=True)
        contract = Contract(
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            autonomy_mode="auto",
            created_at="2026-05-08T00:00:00Z",
            scope_allowed=["**"],
            scope_forbidden=[],
        )
        manifest = TaskManifest(
            id=task_id,
            writes_declared=[],
            allowed_writes=[],
            out_of_scope=[],
            forbidden_hits=[],
            shared_hits=[],
        )
        run_id = "p02-run-1"
        contract_path = task_dir / "contract.json"
        contract_path.write_text(json.dumps({"slug": slug}), encoding="utf-8")
        contract_hash = "deadbeef" * 8

        # Build the prefix the same way ``_cmd_auto_execute`` does.
        prefix = build_implementer_prompt(
            task_brief="round1 brief",
            is_first_pass=True,
            is_doc_only=False,
        )

        outcome = auto_dispatch_task(
            slug=slug,
            task_idx=0,
            repo_root=repo,
            dispatch_fn=_invoke_subagent_dispatch,
            contract=contract,
            manifest=manifest,
            run_id=run_id,
            contract_path=contract_path,
            contract_hash=contract_hash,
            integration_target="master",
            prompt_prefix=prefix,
        )

        # AC: not blocked (manifest_violation row 4 would mean the
        # prefix file leaked into the worktree).
        self.assertNotEqual(
            outcome.status, "blocked",
            f"Round 1 wire produced manifest_violation: "
            f"{outcome.block_type} / {outcome.blocked_md_path}",
        )

        # AC: prefix file exists at <repo>/.flow/.runtime/<slug>+<id>+r1/
        expected = (
            repo / ".flow" / ".runtime"
            / f"{slug}+{task_id}+r1" / "dispatch_prefix.txt"
        )
        self.assertTrue(
            expected.is_file(),
            f"prefix file not found at {expected}",
        )
        body = expected.read_text(encoding="utf-8")
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, body)

        # AC: prefix file path NOT in changed_files / newly_added_files.
        # The file lives at <repo>/.flow/.runtime/... which is OUTSIDE
        # the worktree (worktree is <repo>/.claude/worktrees/<id>/).
        # ``derive_task_facts`` enumerates files inside the worktree so
        # the runtime dir cannot leak in. Re-derive and assert.
        facts = derive_task_facts(outcome.ctx)
        for member in (facts.changed_files, facts.newly_added_files):
            for path_str in member:
                self.assertNotIn(
                    "dispatch_prefix.txt", path_str,
                    f"prefix file leaked into facts: {path_str}",
                )

        # Sanity: the marker file the dispatch template wrote should
        # match the prefix body byte-for-byte (the template `cat`-ed it).
        self.assertTrue(marker.is_file())
        self.assertEqual(
            marker.read_text(encoding="utf-8"),
            body,
            "dispatch template `cat {prompt_prefix_file}` produced a "
            "different body than the prefix file on disk — wire is broken",
        )


class TestRound2FreshWorktreePrefixWireUp(unittest.TestCase):
    """Round 2+ helper: ``_dispatch_implementer_fresh_worktree`` passes
    the prefix to the shim, which writes it under ``+r<N>`` runtime."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p02-r2-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._orig_cwd = os.getcwd()
        self._orig_env = os.environ.get("FLOW_SUBAGENT_DISPATCH_CMD")
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        sys.modules.pop("flow_subagent_dispatch", None)
        sys.modules.pop("flow_orchestrator", None)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        if self._orig_env is not None:
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = self._orig_env

    def test_round2_fresh_worktree_passes_prefix_through(self):
        repo = _make_tmp_repo(self.tmp)

        marker = self.tmp / "round2-dispatch-marker"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {marker.as_posix()}"
        )

        from flow_orchestrator import _dispatch_implementer_fresh_worktree
        from dispatch_template import (
            build_implementer_prompt, K_CLASS_SENTINEL_PROHIBITION,
        )

        slug = "p02r2demo"
        task_id = "T1"

        # Synthetic reviewer feedback the round-2 prompt would carry.
        feedback = "phase 2 halted at gate=acceptance: missing test"
        prefix = build_implementer_prompt(
            task_brief="round2 brief",
            reviewer_feedback=feedback,
            is_first_pass=True,
            is_doc_only=False,
        )

        ctx, facts, deltas = _dispatch_implementer_fresh_worktree(
            repo_root=repo,
            slug=slug,
            task_id=task_id,
            task_idx=0,
            integration_target="master",
            prompt_prefix=prefix,
            round_num=2,
        )

        # AC: prefix file exists at <repo>/.flow/.runtime/<slug>+<id>+r2/
        expected = (
            repo / ".flow" / ".runtime"
            / f"{slug}+{task_id}+r2" / "dispatch_prefix.txt"
        )
        self.assertTrue(
            expected.is_file(),
            f"r2 prefix file not found at {expected}",
        )
        body = expected.read_text(encoding="utf-8")
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, body)
        self.assertIn(feedback, body)
        # And the dispatch template wrote the same content to marker.
        self.assertTrue(marker.is_file())
        self.assertEqual(marker.read_text(encoding="utf-8"), body)


class TestCmdAutoExecutePrefixWireUp(unittest.TestCase):
    """R2 P1#1 — drive ``_cmd_auto_execute`` itself, not just
    ``auto_dispatch_task`` directly. Two complementary assertions:

    * Happy path (recovery → proceed, dispatch path runs): prefix file
      lands at the expected runtime path with K-class text.
    * Skip path (``_task_already_completed`` returns True): the
      build/dispatch sequence is short-circuited; NO prefix file is
      written. Codex R2 P1#2 ordering invariant: prefix-build is a
      side-effect that MUST NOT execute on already-completed tasks.
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p02-cmd-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self._orig_cwd = os.getcwd()
        self._orig_env = os.environ.get("FLOW_SUBAGENT_DISPATCH_CMD")
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        sys.modules.pop("flow_subagent_dispatch", None)
        sys.modules.pop("flow_orchestrator", None)
        self._patches = []

    def tearDown(self):
        for obj, name, val in reversed(self._patches):
            setattr(obj, name, val)
        os.chdir(self._orig_cwd)
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        if self._orig_env is not None:
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = self._orig_env

    def _patch(self, obj, name, value):
        self._patches.append((obj, name, getattr(obj, name)))
        setattr(obj, name, value)

    def _stand_up_cmd_auto_execute_scaffolding(
        self, *, slug: str, task_id: str, already_completed: bool,
    ):
        """Reusable harness: monkeypatch the front-of-loop machinery so
        the test reaches the dispatch-fn boundary quickly. Mirrors the
        established pattern in
        ``tests/smoke/test_phase2_retry_loop.py::TestT821CmdAutoExecuteHonorsParkRc5``.
        Returns (fo module, repo path, task_dir, captured-kwargs dict)."""
        from types import SimpleNamespace
        import flow_orchestrator as fo

        repo = _make_tmp_repo(self.tmp)
        task_dir = repo / ".flow" / "tasks" / slug
        task_dir.mkdir(parents=True)
        # Real prd.md so _render_task_brief returns a meaningful brief.
        (task_dir / "prd.md").write_text(
            "# Test PRD\n\nDo the thing.\n", encoding="utf-8",
        )
        (task_dir / "contract.json").write_text(
            "{\"contract_schema_version\":1,"
            "\"autonomy_mode\":\"full\","
            "\"created_at\":\"2026-05-08T00:00:00Z\"}",
            encoding="utf-8",
        )

        fake_manifest = SimpleNamespace(id=task_id)
        fake_contract = fo.Contract(
            contract_schema_version=1,
            autonomy_mode="full",
            created_at="2026-05-08T00:00:00Z",
            acceptance_criteria=[],
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

        self._patch(fo, "build_plan", lambda s: fake_plan)
        self._patch(fo, "_resolve_slug_dir", lambda s: task_dir)
        self._patch(fo, "_resolve_or_create_run_id", lambda td_: "run-1")
        self._patch(
            fo, "_resolve_gate_commands",
            lambda c: {"baseline": "true", "codex": "true",
                       "smoke": "true", "merge_strategy": "merge"},
        )
        self._patch(fo, "_resolve_integration_target", lambda c: "master")
        self._patch(
            fo, "_task_already_completed",
            lambda td_, *, run_id, task_id: already_completed,
        )

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

        self._patch(fo, "CrashRecoveryDispatcher", _OkDispatcher)
        self._patch(
            fo, "Notifier",
            lambda **kw: SimpleNamespace(fire_block=lambda **k: None),
        )

        # Replace auto_dispatch_task with a thin wrapper that ACTUALLY
        # invokes the dispatch shim with the same kwargs the real one
        # would. This isolates the prefix wire from the lock/event/
        # manifest-verify plumbing already covered by the Round 1 test.
        captured: dict = {}

        def _capturing_auto_dispatch(**kw):
            captured.update(kw)
            wt = repo / ".claude" / "worktrees" / f"{slug}+t0+abcdef0"
            wt.mkdir(parents=True, exist_ok=True)
            ctx_obj = SimpleNamespace(
                worktree_path=wt, slug=slug, task_id=task_id,
            )
            fo._invoke_subagent_dispatch(
                ctx_obj,
                subagent_env=os.environ.copy(),
                task_id=task_id,
                prompt_prefix=kw.get("prompt_prefix", ""),
                round_num=1,
            )
            return SimpleNamespace(
                status="ok", block_type=None, blocked_md_path=None,
                ctx=ctx_obj, facts=SimpleNamespace(),
            )

        self._patch(fo, "auto_dispatch_task", _capturing_auto_dispatch)

        # _phase2_dispatch → return rc=5 (park) so we don't have to
        # stub MergeRunner / Gate8 (downstream of the prefix wire).
        from common.exit_codes import PARKED_RECOVERABLE
        self._patch(
            fo, "_phase2_dispatch",
            lambda **_kw: (PARKED_RECOVERABLE, None, None),
        )

        return fo, repo, task_dir, captured

    def test_cmd_auto_execute_round1_builds_and_passes_prefix(self):
        """Happy path: recovery proceeds → prefix is built + the
        runtime file lands at <repo>/.flow/.runtime/<slug>+<id>+r1/
        dispatch_prefix.txt with K-class text in the body."""
        from dispatch_template import K_CLASS_SENTINEL_PROHIBITION

        slug = "p02cmd"
        task_id = "T1"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {(self.tmp / 'mark1').as_posix()}"
        )

        fo, repo, _td, captured = self._stand_up_cmd_auto_execute_scaffolding(
            slug=slug, task_id=task_id, already_completed=False,
        )

        rc = fo._cmd_auto_execute(slug)

        from common.exit_codes import PARKED_RECOVERABLE
        self.assertEqual(rc, PARKED_RECOVERABLE)

        # auto_dispatch_task got prompt_prefix forwarded.
        self.assertIn("prompt_prefix", captured)
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, captured["prompt_prefix"])
        # And the brief from prd.md is included.
        self.assertIn("# Test PRD", captured["prompt_prefix"])

        # Runtime file exists and content matches.
        expected = (
            repo / ".flow" / ".runtime" / f"{slug}+{task_id}+r1"
            / "dispatch_prefix.txt"
        )
        self.assertTrue(
            expected.is_file(),
            f"prefix file not at {expected}",
        )
        body = expected.read_text(encoding="utf-8")
        self.assertIn(K_CLASS_SENTINEL_PROHIBITION, body)
        self.assertEqual(body, captured["prompt_prefix"])

    def test_cmd_auto_execute_skip_path_writes_no_prefix(self):
        """Codex R2 P1#2 ordering invariant: when
        ``_task_already_completed`` returns True, ``_cmd_auto_execute``
        continues to the next manifest WITHOUT building a prefix or
        writing a runtime file. Building prefix BEFORE the skip check
        would create dangling forensic files for tasks the operator
        meant to no-op."""
        slug = "p02skip"
        task_id = "T1"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file}"

        fo, repo, _td, captured = self._stand_up_cmd_auto_execute_scaffolding(
            slug=slug, task_id=task_id, already_completed=True,
        )

        rc = fo._cmd_auto_execute(slug)
        # All manifests skipped → reach end of loop → rc=0.
        self.assertEqual(rc, 0)
        self.assertEqual(captured, {},
                         "auto_dispatch_task must NOT be called on skip path")
        runtime = repo / ".flow" / ".runtime"
        if runtime.exists():
            for p in runtime.rglob("dispatch_prefix.txt"):
                self.fail(
                    f"prefix file written despite already_completed=True: {p}"
                )


if __name__ == "__main__":
    unittest.main()
