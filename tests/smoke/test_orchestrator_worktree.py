"""T10 — per-task worktree + dual-base + orchestrator-derives-facts.

Plan §10 cases:
  - `create_task_worktree` builds id with shortsha (Q4.1).
  - dual-base commits equal at creation (S6).
  - `derive_task_facts` reads `git diff` (subagent narrative IGNORED).
  - `auto_engaged` event written to `decisions.jsonl` BEFORE first
    subagent dispatch (Q7.2 + §8.4 row `auto_engaged` 14-field schema).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_orchestrator import (  # type: ignore
    WorktreeContext, TaskFacts,
    create_task_worktree, derive_task_facts, auto_dispatch_task,
)
from flow_state_writer import EVENT_AUTO_ENGAGED  # type: ignore
from flow_contract import Contract, CONTRACT_SCHEMA_VERSION  # type: ignore


def _init_repo(path: Path) -> None:
    """Bootstrap a clean git repo at `path` with one commit."""
    subprocess.run(["git", "init", "-q", "-b", "master", str(path)], check=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "x@y"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "x"], check=True,
    )
    (path / "VERSION").write_text("0.0.0\n")
    subprocess.run(["git", "-C", str(path), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True,
    )


class TestCreateTaskWorktree(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)

    def test_worktree_id_uses_shortsha_naming(self) -> None:
        ctx = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=1, integration_target="master",
        )
        self.assertEqual(ctx.task_idx, 1)
        self.assertTrue(ctx.worktree_id.startswith("demo+t1+"))
        # 7-char shortsha (Q4.1).
        self.assertEqual(len(ctx.worktree_id.split("+")[-1]), 7)
        self.assertTrue(ctx.worktree_path.is_dir())
        self.assertEqual(ctx.lifecycle_state, "active")
        self.assertEqual(ctx.branch, ctx.worktree_id)

    def test_dual_base_commits_match_at_creation(self) -> None:
        ctx = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=0, integration_target="master",
        )
        # S6: at creation, original == current.
        self.assertEqual(ctx.original_base_commit, ctx.current_base_commit)
        self.assertEqual(len(ctx.original_base_commit), 40)  # full sha
        # base_shortsha is the 7-char prefix of the full sha.
        self.assertEqual(ctx.base_shortsha, ctx.original_base_commit[:7])

    def test_worktree_id_stable_across_recreation(self) -> None:
        """Q4.1 rationale: same (slug, task_idx, base) → same worktree_id.
        We tear down the first worktree, then recreate — the second call
        must produce an id that ONLY differs if the base commit moved.
        """
        ctx1 = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=2, integration_target="master",
        )
        # Remove the worktree so the second create can use the same id.
        subprocess.run(
            ["git", "-C", str(self.tmp), "worktree", "remove",
             "--force", str(ctx1.worktree_path)],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "branch", "-D", ctx1.branch],
            check=True,
        )
        ctx2 = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=2, integration_target="master",
        )
        # Base commit is unchanged → ids must be identical (rerun-safe).
        self.assertEqual(ctx1.worktree_id, ctx2.worktree_id)
        self.assertEqual(ctx1.base_shortsha, ctx2.base_shortsha)


class TestDeriveTaskFacts(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)
        self.ctx = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=0, integration_target="master",
        )

    def test_derive_task_facts_reads_git_diff(self) -> None:
        # Simulate a subagent edit + commit inside the worktree.
        (self.ctx.worktree_path / "feature.py").write_text("def x(): pass\n")
        subprocess.run(
            ["git", "-C", str(self.ctx.worktree_path), "add", "."],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.ctx.worktree_path),
             "commit", "-q", "-m", "add feature"],
            check=True,
        )
        facts = derive_task_facts(self.ctx)
        self.assertIn("feature.py", facts.changed_files)
        self.assertIn("feature.py", facts.newly_added_files)
        self.assertNotEqual(
            facts.target_commit_pre_merge, self.ctx.original_base_commit,
        )
        self.assertEqual(len(facts.diff_hash), 64)  # sha256 hex

    def test_derive_task_facts_no_changes_is_distinct_from_failure(self) -> None:
        """F-class fail-closed: an empty diff (no work yet) is a real
        answer represented by empty lists + a stable hash, NOT the
        same code path as 'git command failed' (which raises).
        """
        facts = derive_task_facts(self.ctx)
        self.assertEqual(facts.changed_files, [])
        self.assertEqual(facts.newly_added_files, [])
        self.assertEqual(
            facts.target_commit_pre_merge, self.ctx.original_base_commit,
        )
        # sha256 of empty string is a known constant — not the all-zero
        # hash a "treat as failure" branch might emit.
        self.assertEqual(
            facts.diff_hash,
            "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",
        )


class TestAutoDispatchTask(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)
        # auto_dispatch_task expects `<repo>/.flow/tasks/<slug>/` to exist.
        (self.tmp / ".flow" / "tasks" / "demo").mkdir(parents=True)
        # Minimal valid Contract for the event payload (only
        # contract_schema_version is read by auto_dispatch_task).
        self.contract = Contract(
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            autonomy_mode="auto",
            created_at="2026-05-06T00:00:00Z",
        )

    def test_auto_engaged_written_before_dispatch(self) -> None:
        """Q7.2: the boundary marker must exist on disk BEFORE the
        subagent runs, even if dispatch crashes. We assert this by
        inspecting `decisions.jsonl` from inside the dispatch_fn — at
        that point, dispatch hasn't returned yet.
        """
        captured_lines: list[str] = []
        decisions_path = self.tmp / ".flow" / "tasks" / "demo" / "decisions.jsonl"

        def fake_dispatch(ctx: WorktreeContext) -> None:
            # Inside dispatch — auto_engaged must already be on disk.
            captured_lines.extend(decisions_path.read_text().splitlines())

        auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=fake_dispatch,
            contract=self.contract,
            run_id="run-abc",
            contract_path=self.tmp / "contract.json",
            contract_hash="cafebabe" * 8,
        )
        events_seen = [json.loads(line)["event"] for line in captured_lines]
        self.assertIn(EVENT_AUTO_ENGAGED, events_seen)

    def test_auto_engaged_emitted_even_when_dispatch_crashes(self) -> None:
        """Q7.2 strict: even if dispatch raises immediately, the
        auto_engaged event must already be on disk so §6 R10 lock-state
        recovery can distinguish 'never started' from 'crashed mid-run'.
        """
        decisions_path = self.tmp / ".flow" / "tasks" / "demo" / "decisions.jsonl"

        def crashing_dispatch(ctx: WorktreeContext) -> None:
            raise RuntimeError("subagent died")

        with self.assertRaises(RuntimeError):
            auto_dispatch_task(
                slug="demo", task_idx=0, repo_root=self.tmp,
                dispatch_fn=crashing_dispatch,
                contract=self.contract,
                run_id="run-abc",
                contract_path=self.tmp / "contract.json",
                contract_hash="cafebabe" * 8,
            )
        # Event made it to disk despite the crash.
        self.assertTrue(decisions_path.is_file())
        records = [json.loads(ln) for ln in decisions_path.read_text().splitlines() if ln]
        events = [r["event"] for r in records]
        self.assertIn(EVENT_AUTO_ENGAGED, events)
        # 14-field payload sanity check (§8.4 row `auto_engaged`).
        engaged = next(r for r in records if r["event"] == EVENT_AUTO_ENGAGED)
        for key in (
            "event_id", "ts", "slug", "run_id", "task_id",
            "worktree_id", "worktree_path",
            "original_base_commit", "current_base_commit",
            "lifecycle_state", "checkpoint_id",
            "contract_path", "contract_hash", "contract_schema_version",
        ):
            self.assertIn(key, engaged)

    def test_auto_dispatch_returns_facts_derived_from_disk(self) -> None:
        """PRD §1.2: subagent narrative is IGNORED. dispatch_fn is called
        for side effects (it writes files inside the worktree) but its
        return value is never propagated. The TaskFacts that
        auto_dispatch_task returns come from `derive_task_facts(ctx)`,
        which reads git directly.
        """
        def writing_dispatch(ctx: WorktreeContext):
            (ctx.worktree_path / "evidence.py").write_text("# hello\n")
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path), "add", "."],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path),
                 "commit", "-q", "-m", "subagent edit"],
                check=True,
            )
            # Lying narrative — must be ignored by orchestrator.
            return {"changed_files": ["LIES.md"], "diff_hash": "0" * 64}

        facts = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=writing_dispatch,
            contract=self.contract,
            run_id="run-xyz",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertIsInstance(facts, TaskFacts)
        self.assertIn("evidence.py", facts.changed_files)
        self.assertNotIn("LIES.md", facts.changed_files)
        self.assertNotEqual(facts.diff_hash, "0" * 64)


class TestCreateTaskWorktreeValidation(unittest.TestCase):
    """Path-traversal + shell-metachar fail-closed checks. Slug and
    integration_target end up in filesystem paths; an unvalidated value
    could escape `.claude/worktrees/`. We allowlist a narrow charset and
    fail BEFORE any disk side effect.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)

    def test_slug_with_path_traversal_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=self.tmp, slug="../escape",
                task_idx=0, integration_target="master",
            )

    def test_slug_with_shell_metachar_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=self.tmp, slug="demo;rm",
                task_idx=0, integration_target="master",
            )

    def test_negative_task_idx_rejected(self) -> None:
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=self.tmp, slug="demo",
                task_idx=-1, integration_target="master",
            )

    def test_bool_task_idx_rejected(self) -> None:
        # `bool` is a subclass of int — make sure it doesn't smuggle past.
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=self.tmp, slug="demo",
                task_idx=True,  # type: ignore[arg-type]
                integration_target="master",
            )

    def test_real_world_dotted_slug_accepted(self) -> None:
        """T10 spec-review must-fix: the project's own slug shape uses
        dots (``05-05-autonomous-mode-v0.8``). Allowlist must accept it.
        """
        ctx = create_task_worktree(
            repo_root=self.tmp,
            slug="05-05-autonomous-mode-v0.8",
            task_idx=0, integration_target="master",
        )
        self.assertTrue(
            ctx.worktree_id.startswith("05-05-autonomous-mode-v0.8+t0+")
        )

    def test_slug_with_double_dot_segment_rejected(self) -> None:
        """Even though ``.`` is in the allowed charset, ``..`` is a
        path-traversal token and must be denylisted explicitly.
        """
        with self.assertRaises(ValueError):
            create_task_worktree(
                repo_root=self.tmp,
                slug="demo..secret",
                task_idx=0, integration_target="master",
            )


class TestAutoDispatchTaskValidation(unittest.TestCase):
    """T10 spec-review should-fix #1: `auto_dispatch_task` must reject
    empty/whitespace ``run_id`` and ``contract_hash`` (F-class fail-open
    closure — `append_autonomy_event` only validates key presence, so
    empty strings would pass and journal a corrupt audit record).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)
        (self.tmp / ".flow" / "tasks" / "demo").mkdir(parents=True)
        self.contract = Contract(
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            autonomy_mode="auto",
            created_at="2026-05-06T00:00:00Z",
        )

    def _call(self, **overrides):
        kwargs = dict(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=lambda _ctx: None,
            contract=self.contract,
            run_id="run-abc",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        kwargs.update(overrides)
        return auto_dispatch_task(**kwargs)

    def test_empty_run_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._call(run_id="")

    def test_whitespace_run_id_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._call(run_id="   ")

    def test_empty_contract_hash_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._call(contract_hash="")

    def test_non_path_contract_path_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._call(contract_path="contract.json")  # type: ignore[arg-type]

    def test_none_contract_rejected_before_side_effects(self) -> None:
        """Codex round-1 [P2]: a None / wrong-type contract must fail
        BEFORE create_task_worktree runs — otherwise an orphaned worktree
        would be left on disk and the Q7.2 auto_engaged boundary marker
        would be skipped for the invalid-input path.
        """
        worktrees_root = self.tmp / ".claude" / "worktrees"
        before = (
            sorted(p.name for p in worktrees_root.iterdir())
            if worktrees_root.exists() else []
        )
        with self.assertRaises(ValueError):
            self._call(contract=None)  # type: ignore[arg-type]
        after = (
            sorted(p.name for p in worktrees_root.iterdir())
            if worktrees_root.exists() else []
        )
        self.assertEqual(
            before, after,
            "auto_dispatch_task created a worktree before validating contract",
        )

    def test_wrong_type_contract_rejected(self) -> None:
        with self.assertRaises(ValueError):
            self._call(contract={"contract_schema_version": 1})  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
