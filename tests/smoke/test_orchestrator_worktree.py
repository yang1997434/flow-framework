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
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_orchestrator import (  # type: ignore
    WorktreeContext, TaskFacts, TaskManifest,
    DispatchOutcome, ManifestVerdict,
    GateResult, BaselineRecord, Phase2Verdict, GateRunner,
    MergeRunner, MergeResult, detect_mid_merge_crash,
    create_task_worktree, derive_task_facts, auto_dispatch_task,
    verify_manifest_against_facts,
    canonical_issue_id,
)
from flow_state_writer import (  # type: ignore
    EVENT_AUTO_ENGAGED,
    EVENT_TASK_READY_TO_MERGE,
    EVENT_MERGE_STARTED,
    EVENT_MERGE_APPLIED,
    EVENT_TASK_COMPLETED,
    EVENT_POST_MERGE_VERIFY_FAILED,
    ReviewIssueRecord,
    append_autonomy_event,
    append_review_issue,
)
from flow_contract import (  # type: ignore
    Contract, CONTRACT_SCHEMA_VERSION, AcceptanceCriterion,
)


def _empty_manifest(id: str = "T0") -> TaskManifest:
    """Minimal TaskManifest fixture for `auto_dispatch_task` callers that
    don't care about scope-vs-declared-writes interaction (T10-style
    boundary tests). T11's verifier reads `contract.scope_*`, not
    `manifest.*` — `manifest.id` is the only field consumed here."""
    return TaskManifest(
        id=id,
        writes_declared=[],
        allowed_writes=[],
        out_of_scope=[],
        forbidden_hits=[],
        shared_hits=[],
    )


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
            manifest=_empty_manifest(),
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
                manifest=_empty_manifest(),
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

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=writing_dispatch,
            contract=self.contract,
            manifest=_empty_manifest(),
            run_id="run-xyz",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        # T11: return type is DispatchOutcome; facts is a nested attr.
        self.assertIsInstance(outcome, DispatchOutcome)
        self.assertEqual(outcome.status, "ok")
        self.assertIsInstance(outcome.facts, TaskFacts)
        self.assertIn("evidence.py", outcome.facts.changed_files)
        self.assertNotIn("LIES.md", outcome.facts.changed_files)
        self.assertNotEqual(outcome.facts.diff_hash, "0" * 64)


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
            manifest=_empty_manifest(),
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

    def test_wrong_type_manifest_rejected(self) -> None:
        """T11: `manifest` is required and must be a TaskManifest. Same
        F-class fail-closed reasoning as `contract`: without this guard
        a None manifest would create the worktree first and only blow
        up later when reading `manifest.id` for the auto_engaged event.
        """
        with self.assertRaises(ValueError):
            self._call(manifest=None)  # type: ignore[arg-type]


class TestVerifyManifestAgainstFacts(unittest.TestCase):
    """Plan §11.1 — pure-function verifier cases against a fixed contract.

    contract.scope_allowed = ["src/**", "tests/**"]
    contract.scope_forbidden = ["src/secrets/**"]

    Order of precedence the verifier promises (C-blindspot):
      forbidden glob > shared artifacts > out-of-scope.
    """

    def setUp(self) -> None:
        self.contract = Contract(
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            autonomy_mode="auto",
            created_at="2026-05-06T00:00:00Z",
            scope_allowed=["src/**", "tests/**"],
            scope_forbidden=["src/secrets/**"],
        )
        self.manifest = TaskManifest(
            id="T1",
            writes_declared=["src/foo.py"],
            allowed_writes=["src/foo.py"],
            out_of_scope=[],
            forbidden_hits=[],
            shared_hits=[],
        )

    def test_forbidden_path_in_facts_blocks(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py", "src/secrets/key.pem"],
            newly_added_files=[],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "block")
        self.assertEqual(v.block_row, 3)
        self.assertIn("src/secrets/key.pem", v.violations)

    def test_out_of_scope_in_facts_blocks(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py", "infra/deploy.yml"],
            newly_added_files=[],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "block")
        self.assertEqual(v.block_row, 3)
        self.assertIn("infra/deploy.yml", v.violations)

    def test_untracked_added_outside_scope_blocks_row4(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py", "scripts/rogue.sh"],
            newly_added_files=["scripts/rogue.sh"],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "block")
        self.assertEqual(v.block_row, 4)
        self.assertIn("scripts/rogue.sh", v.violations)

    def test_shared_artifact_warns_not_blocks(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py", "VERSION"],
            newly_added_files=[],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "pass")
        self.assertIn("VERSION", v.shared_artifacts_touched)
        self.assertEqual(v.violations, [])

    def test_clean_facts_pass(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py", "tests/test_foo.py"],
            newly_added_files=["tests/test_foo.py"],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "pass")
        self.assertEqual(v.violations, [])
        self.assertEqual(v.shared_artifacts_touched, [])

    def test_forbidden_precedence_over_allowed(self) -> None:
        """C-blindspot: a path that matches BOTH `scope_forbidden` and
        `scope_allowed` must block (forbidden wins). With our fixture,
        `src/secrets/key.pem` matches BOTH `src/secrets/**` (forbidden)
        AND `src/**` (allowed). The verifier must classify it as a
        forbidden hit, not let allowed-match short-circuit the check.
        """
        facts = TaskFacts(
            changed_files=["src/secrets/key.pem"],
            newly_added_files=[],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(self.contract, self.manifest, facts)
        self.assertEqual(v.decision, "block")
        self.assertEqual(v.block_row, 3)

    def test_empty_scope_allowed_skips_out_of_scope_check(self) -> None:
        """F-blindspot fail-open closure documented behavior: an empty
        `scope_allowed` list means "no allowlist configured" — out-of-
        scope check is skipped (matches v0.8.0 `build_plan()` advisory
        semantics). Forbidden + shared steps still run."""
        contract = Contract(
            contract_schema_version=CONTRACT_SCHEMA_VERSION,
            autonomy_mode="auto",
            created_at="2026-05-06T00:00:00Z",
            scope_allowed=[],
            scope_forbidden=["src/secrets/**"],
        )
        # Forbidden hit still blocks even with empty allowlist.
        facts = TaskFacts(
            changed_files=["anywhere/file.py", "src/secrets/key.pem"],
            newly_added_files=[],
            diff_hash="x",
            target_commit_pre_merge="y",
        )
        v = verify_manifest_against_facts(contract, self.manifest, facts)
        self.assertEqual(v.decision, "block")
        self.assertEqual(v.block_row, 3)
        self.assertEqual(v.violations, ["src/secrets/key.pem"])

    def test_real_world_glob_pattern(self) -> None:
        """E-blindspot: validation rules vs real data. Matcher must
        accept the patterns this project's own contracts will use:
        `src/**` matches files at depth 1, 2, 3+. `scripts/**.py`
        (would-be) etc. Test against actual repo-relative paths.
        """
        import fnmatch
        # Sanity-check the underlying matcher does what the plan
        # expects before the verifier relies on it.
        self.assertTrue(fnmatch.fnmatch("src/foo.py", "src/**"))
        self.assertTrue(fnmatch.fnmatch("src/a/b.py", "src/**"))
        self.assertTrue(fnmatch.fnmatch("src/a/b/c.py", "src/**"))
        self.assertFalse(fnmatch.fnmatch("infra/x.yml", "src/**"))
        self.assertFalse(fnmatch.fnmatch("VERSION", "src/**"))


class TestAutoDispatchManifestBlock(unittest.TestCase):
    """Plan §11.3 — end-to-end: subagent edits forbidden file →
    `auto_dispatch_task` writes blocked.md (block_type=manifest_violation)
    and returns a blocked DispatchOutcome.
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
            scope_allowed=["src/**"],
            scope_forbidden=["secrets/**"],
        )
        self.manifest = TaskManifest(
            id="T1",
            writes_declared=["src/foo.py"],
            allowed_writes=["src/foo.py"],
            out_of_scope=[],
            forbidden_hits=[],
            shared_hits=[],
        )

    def test_dispatch_block_on_manifest_violation(self) -> None:
        """Subagent writes a forbidden file inside the worktree;
        orchestrator must detect it post-dispatch and block."""

        def rogue_dispatch(ctx: WorktreeContext) -> None:
            secrets_dir = ctx.worktree_path / "secrets"
            secrets_dir.mkdir(parents=True)
            (secrets_dir / "key.pem").write_text("PRIVATE\n")
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path), "add", "."],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path),
                 "commit", "-q", "-m", "subagent rogue write"],
                check=True,
            )

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=rogue_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-blk",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertIsInstance(outcome, DispatchOutcome)
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.block_type, "manifest_violation")
        self.assertEqual(outcome.block_row, 3)  # forbidden hit → row 3.
        self.assertIsNotNone(outcome.blocked_md_path)
        # blocked.md content surfaces the violation classification.
        blocked = outcome.blocked_md_path.read_text()
        self.assertIn("manifest_violation", blocked)
        self.assertIn("secrets/key.pem", blocked)

    def test_dispatch_block_on_uncommitted_forbidden_file(self) -> None:
        """Codex T11 round-1 [P1]: subagent leaves a forbidden file in
        the working tree without committing it. `derive_task_facts`
        must include working-tree state via `git status --porcelain`,
        not just `base..HEAD` diff. Without the fix, this dispatch
        returned ``ok`` despite the row-3 violation (silent bypass).
        """

        def stash_dispatch(ctx: WorktreeContext) -> None:
            secrets_dir = ctx.worktree_path / "secrets"
            secrets_dir.mkdir(parents=True)
            (secrets_dir / "key.pem").write_text("PRIVATE\n")
            # Note: NO git add, NO commit. File sits as untracked in
            # the working tree.

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=stash_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-uncomm",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.block_type, "manifest_violation")
        self.assertEqual(outcome.block_row, 3)
        blocked = outcome.blocked_md_path.read_text()
        self.assertIn("secrets/key.pem", blocked)

    def test_dispatch_block_on_untracked_outside_scope(self) -> None:
        """Codex T11 round-1 [P1] — row 4 specifically: subagent creates
        a brand-new file outside scope and never tracks it. The §1 row 4
        rule was designed exactly for this; before the working-tree fix
        the file was invisible to verification.
        """

        def untracked_dispatch(ctx: WorktreeContext) -> None:
            (ctx.worktree_path / "infra").mkdir(parents=True)
            (ctx.worktree_path / "infra" / "deploy.yml").write_text(
                "# rogue\n"
            )

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=untracked_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-untracked",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.block_type, "manifest_violation")
        self.assertEqual(outcome.block_row, 4)  # row 4 = untracked outside scope.
        blocked = outcome.blocked_md_path.read_text()
        self.assertIn("infra/deploy.yml", blocked)

    def test_untracked_filename_with_arrow_separator_does_not_bypass(self) -> None:
        """Codex T11 round-2 [P1]: a malicious subagent could create an
        untracked file whose NAME literally contains ` -> ` — porcelain
        v1 (without -z) renders it identically to a rename record,
        letting the parser trim the forbidden prefix. ``-z`` makes the
        format unambiguous. This test pins that attack vector closed.
        """

        def arrow_dispatch(ctx: WorktreeContext) -> None:
            secrets_dir = ctx.worktree_path / "secrets"
            secrets_dir.mkdir(parents=True)
            # Filename literally contains ` -> ` — would be misparsed
            # by the pre-fix code as "renamed-to" half "ok-decoy.py"
            # (the in-scope safe-looking suffix), bypassing the
            # forbidden ``secrets/...`` prefix entirely.
            arrow_name = "key.pem -> ok-decoy.py"
            (secrets_dir / arrow_name).write_text("PRIVATE\n")

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=arrow_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-arrow",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertEqual(outcome.status, "blocked")
        # The forbidden path is recorded LITERALLY, including the ` -> `.
        blocked = outcome.blocked_md_path.read_text()
        self.assertIn("secrets/key.pem -> ok-decoy.py", blocked)

    def test_dispatch_block_on_staged_but_uncommitted_forbidden(self) -> None:
        """Subagent ``git add``s a forbidden file but never commits.
        Staged-only must also trip the manifest verifier.
        """

        def staged_dispatch(ctx: WorktreeContext) -> None:
            secrets_dir = ctx.worktree_path / "secrets"
            secrets_dir.mkdir(parents=True)
            (secrets_dir / "key.pem").write_text("PRIVATE\n")
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path), "add", "."],
                check=True,
            )
            # NO commit.

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=staged_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-staged",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertEqual(outcome.status, "blocked")
        self.assertEqual(outcome.block_row, 3)

    def test_dispatch_ok_on_clean_diff(self) -> None:
        """Same orchestration shell, but subagent writes only in-scope.
        Outcome.status must be "ok" with no blocked.md side effect."""

        def clean_dispatch(ctx: WorktreeContext) -> None:
            (ctx.worktree_path / "src").mkdir(parents=True)
            (ctx.worktree_path / "src" / "foo.py").write_text("# ok\n")
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path), "add", "."],
                check=True,
            )
            subprocess.run(
                ["git", "-C", str(ctx.worktree_path),
                 "commit", "-q", "-m", "in-scope edit"],
                check=True,
            )

        outcome = auto_dispatch_task(
            slug="demo", task_idx=0, repo_root=self.tmp,
            dispatch_fn=clean_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-ok",
            contract_path=self.tmp / "contract.json",
            contract_hash="deadbeef" * 8,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIsNone(outcome.block_type)
        self.assertIsNone(outcome.block_row)
        self.assertIsNone(outcome.blocked_md_path)
        self.assertFalse(
            (self.tmp / ".flow" / "tasks" / "demo" / "blocked.md").exists()
        )


# ----------------------------------------------------------------------
# T12 — GateRunner: gates 1, 3, 5, 6 + run_phase2 chain.
# ----------------------------------------------------------------------


def _make_gate_runner_ctx(tmp: Path) -> tuple[WorktreeContext, Contract, Path]:
    """Shared fixture for GateRunner tests. Builds a real worktree (so
    gate1/gate6 subprocess `cwd=...` is valid) + a minimal Contract +
    a per-task task_dir.
    """
    _init_repo(tmp)
    ctx = create_task_worktree(
        repo_root=tmp, slug="t12demo", task_idx=0,
        integration_target="master",
    )
    contract = Contract(
        contract_schema_version=CONTRACT_SCHEMA_VERSION,
        autonomy_mode="auto_default",
        created_at="2026-05-06T00:00:00Z",
        scope_allowed=["src/**"],
        scope_forbidden=["secrets/**"],
    )
    task_dir = tmp / ".flow" / "tasks" / "t12demo"
    task_dir.mkdir(parents=True, exist_ok=True)
    return ctx, contract, task_dir


class TestGate1Baseline(unittest.TestCase):
    """§1 row 7: baseline newly broken → block. Q3.1: runs INSIDE worktree."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t12-gate1-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )

    def test_baseline_clean_passes(self) -> None:
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate1_baseline(test_command="true")
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.details["pre_existing_fails"], [])

    def test_baseline_newly_fail_blocks_row7(self) -> None:
        """No prior baseline AND current fails → block row 7 (F-class
        fail-closed)."""
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
            prior_baseline=BaselineRecord(failing=[]),
        )
        r = gr.gate1_baseline(test_command="false")
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.details["block_row"], 7)
        self.assertEqual(r.details["returncode"], 1)

    def test_baseline_timeout_returns_inconclusive(self) -> None:
        """D5 catch-all: subprocess timeout MUST NOT silently pass; routes
        to operator review via inconclusive."""
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate1_baseline(test_command="sleep 5", timeout_sec=1)
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(r.details["reason"], "timeout")


class TestGate3Manifest(unittest.TestCase):
    """Wires T11 verifier. Pass for clean facts; fail with block_row when
    forbidden / out-of-scope facts are detected."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t12-gate3-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )
        self.manifest = TaskManifest(
            id="T0", writes_declared=["src/foo.py"],
            allowed_writes=["src/foo.py"],
            out_of_scope=[], forbidden_hits=[], shared_hits=[],
        )

    def test_gate3_returns_pass_for_clean_facts(self) -> None:
        facts = TaskFacts(
            changed_files=["src/foo.py"], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate3_manifest(manifest=self.manifest, facts=facts)
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.details["shared_artifacts_touched"], [])

    def test_gate3_returns_fail_for_forbidden(self) -> None:
        facts = TaskFacts(
            changed_files=["secrets/key.pem"], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate3_manifest(manifest=self.manifest, facts=facts)
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.details["block_row"], 3)
        self.assertIn("secrets/key.pem", r.details["violations"])


class TestGate5Acceptance(unittest.TestCase):
    """Iterates criteria; halts on first non-PASS via T7 run_one + T8
    evaluate_criterion(phase=2). D6 status guard: every EvalDecision branch
    has an explicit mapping."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t12-gate5-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )

    def test_gate5_passes_when_all_criteria_pass(self) -> None:
        # Use file_exists method so no subprocess is needed; VERSION was
        # created by _init_repo and is committed in the worktree.
        criteria = [
            AcceptanceCriterion(
                description="version present",
                type="unit", method="file_exists",
                path="VERSION", timeout_sec=30,
            ),
        ]
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate5_acceptance(
            criteria=criteria, attempt_id="a1", retry_idx=0,
        )
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.details["criteria_count"], 1)

    def test_gate5_halts_on_first_local_fix_allowed(self) -> None:
        """Phase 2 unit fail → LOCAL_FIX_ALLOWED. Halts at idx 0; the
        second criterion must NOT execute."""
        criteria = [
            AcceptanceCriterion(
                description="failing unit",
                type="unit", method="cmd",
                command="false", timeout_sec=10,
            ),
            AcceptanceCriterion(
                description="would-pass unit",
                type="unit", method="cmd",
                command="true", timeout_sec=10,
            ),
        ]
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate5_acceptance(
            criteria=criteria, attempt_id="a1", retry_idx=0,
        )
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.details["halted_at_idx"], 0)
        self.assertEqual(r.details["decision"], "local_fix_allowed")
        self.assertEqual(r.details["block_row"], 5)
        self.assertFalse(r.escalate)

    def test_gate5_e2e_fail_propagates_escalate(self) -> None:
        """e2e fail → BLOCKED_ESCALATE_ROW6 with escalate=True (Y1)."""
        criteria = [
            AcceptanceCriterion(
                description="e2e flow",
                type="e2e", method="cmd",
                command="false", timeout_sec=10,
            ),
        ]
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate5_acceptance(
            criteria=criteria, attempt_id="a1", retry_idx=0,
        )
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.escalate)
        self.assertEqual(r.details["block_row"], 6)
        self.assertEqual(r.details["decision"], "blocked_escalate_row6")


class TestGate6Regression(unittest.TestCase):
    """Final regression smoke. Mirrors gate 1 in shape but later in chain."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t12-gate6-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )

    def test_gate6_passes_when_smoke_returns_zero(self) -> None:
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate6_regression(smoke_command="true")
        self.assertEqual(r.status, "pass")

    def test_gate6_fails_when_smoke_returns_nonzero(self) -> None:
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        r = gr.gate6_regression(smoke_command="false")
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.details["block_row"], 5)
        self.assertIn("returncode", r.details)


class TestRunPhase2Chain(unittest.TestCase):
    """End-to-end: gate1 → gate3 → gate5 → gate6 chain via run_phase2.
    First non-pass halts; later gates do NOT execute."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t12-phase2-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )
        self.manifest = TaskManifest(
            id="T0", writes_declared=["src/foo.py"],
            allowed_writes=["src/foo.py"],
            out_of_scope=[], forbidden_hits=[], shared_hits=[],
        )
        self.clean_facts = TaskFacts(
            changed_files=["src/foo.py"], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        self.criteria = [
            AcceptanceCriterion(
                description="version present",
                type="unit", method="file_exists",
                path="VERSION", timeout_sec=30,
            ),
        ]

    def test_run_phase2_full_pass(self) -> None:
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        # T13 chain extension: full pass now traverses 1 → 3 → 4 → 5 → 6.
        # gate 4 wraps the codex CLI; tests inject a deterministic GREEN
        # stub so the chain reaches gate 5 + 6 without depending on a
        # real codex install.
        v = gr.run_phase2(
            manifest=self.manifest, facts=self.clean_facts,
            criteria=self.criteria,
            attempt_id="a1", retry_idx=0,
            baseline_command="true", smoke_command="true",
            codex_command="echo '{\"verdict\":\"GREEN\",\"issues\":[]}'",
        )
        self.assertEqual(v.status, "pass")
        self.assertIsNone(v.halted_at_gate)
        self.assertIsNone(v.gate_result)

    def test_run_phase2_halts_at_gate1_when_baseline_fails(self) -> None:
        """Baseline failure → halt before gate3/5/6 fire. The forbidden
        facts that would normally fail gate3 are passed but never reached
        — the chain halts at gate1."""
        forbidden_facts = TaskFacts(
            changed_files=["secrets/key.pem"], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        v = gr.run_phase2(
            manifest=self.manifest, facts=forbidden_facts,
            criteria=self.criteria,
            attempt_id="a1", retry_idx=0,
            baseline_command="false",   # halts here
            smoke_command="true",
        )
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.halted_at_gate, "gate1_baseline")
        self.assertIsNotNone(v.gate_result)
        self.assertEqual(v.gate_result.status, "fail")
        self.assertEqual(v.gate_result.details["block_row"], 7)

    def test_run_phase2_halts_at_gate3_when_manifest_violates(self) -> None:
        """Baseline passes; manifest verifier blocks → halt before gate5/6.

        Codex round-1 [P2] update: ``run_phase2`` re-derives facts after
        gate1, so this test must place a real forbidden file in the
        worktree (not pass a stub TaskFacts). We write the forbidden
        file as untracked content; derive_task_facts will surface it
        via the working-tree porcelain scan.
        """
        secrets_dir = self.ctx.worktree_path / "secrets"
        secrets_dir.mkdir(parents=True)
        (secrets_dir / "key.pem").write_text("PRIVATE\n")
        # Initial facts argument is irrelevant after the round-1 fix —
        # run_phase2 re-derives. Pass an empty stub to make that explicit.
        empty_facts = TaskFacts(
            changed_files=[], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        v = gr.run_phase2(
            manifest=self.manifest, facts=empty_facts,
            criteria=self.criteria,
            attempt_id="a1", retry_idx=0,
            baseline_command="true",
            smoke_command="true",
        )
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.halted_at_gate, "gate3_manifest")
        self.assertEqual(v.gate_result.details["block_row"], 3)
        # Pin the round-1 [P2] fix: violation reflects the LIVE
        # post-baseline disk state, not the stale stub.
        self.assertIn(
            "secrets/key.pem", v.gate_result.details["violations"]
        )

    def test_run_phase2_inconclusive_on_corrupted_worktree_post_baseline(self) -> None:
        """Codex round-2 [P2]: when the post-baseline fact refresh
        fails (e.g. baseline corrupted the worktree, ran out of
        inodes, etc.), run_phase2 must return a controlled
        inconclusive verdict — NOT propagate the exception and crash
        the orchestrator.

        We simulate the failure by patching ``derive_task_facts`` for
        the duration of this call to raise ``CalledProcessError``,
        which is what real git invocations under check=True surface
        when the worktree is unreadable. Constructing a real worktree
        corruption is fragile (git falls back to parent .git on a
        missing internal pointer) and not worth the indirection.
        """
        import flow_orchestrator as fo  # type: ignore

        empty_facts = TaskFacts(
            changed_files=[], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        original = fo.derive_task_facts
        try:
            fo.derive_task_facts = lambda _ctx: (_ for _ in ()).throw(
                subprocess.CalledProcessError(
                    returncode=128,
                    cmd=["git", "diff"],
                    stderr="fatal: not a git repository",
                )
            )
            v = gr.run_phase2(
                manifest=self.manifest, facts=empty_facts,
                criteria=self.criteria,
                attempt_id="a1", retry_idx=0,
                baseline_command="true",
                smoke_command="true",
            )
        finally:
            fo.derive_task_facts = original

        # Must produce a verdict (no exception escape).
        self.assertEqual(v.status, "blocked")
        # Codex round-3 [P2]: halt site is BEFORE gate3 — must label
        # accordingly so audit logs point at the real failure step.
        self.assertEqual(v.halted_at_gate, "post_baseline_fact_refresh")
        self.assertEqual(v.gate_result.status, "inconclusive")
        self.assertEqual(
            v.gate_result.details["reason"],
            "post_baseline_fact_refresh_failed",
        )
        self.assertIn(
            "CalledProcessError", v.gate_result.details["error"]
        )
        # Codex round-3 [P3]: git stderr is the actionable clue — must
        # be preserved in details, not dropped by str(CalledProcessError).
        self.assertIn(
            "fatal: not a git repository",
            v.gate_result.details["stderr_tail"],
        )

    def test_run_phase2_rederive_catches_baseline_side_effects(self) -> None:
        """Codex round-1 [P2]: a baseline command that itself writes
        forbidden files into the worktree must be caught by gate 3.
        Pre-fix code passed the original facts straight through and
        gate3 missed the violation entirely.
        """
        empty_facts = TaskFacts(
            changed_files=[], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )
        # Baseline command is contract-author-supplied (E-class trusted).
        # Here we simulate a "baseline that accidentally writes to a
        # forbidden path" — e.g. a test runner with an enabled cache
        # plugin that writes outside its configured cachedir.
        baseline_cmd = (
            f"mkdir -p {self.ctx.worktree_path}/secrets && "
            f"echo CACHED > {self.ctx.worktree_path}/secrets/key.pem"
        )
        v = gr.run_phase2(
            manifest=self.manifest, facts=empty_facts,
            criteria=self.criteria,
            attempt_id="a1", retry_idx=0,
            baseline_command=baseline_cmd,
            smoke_command="true",
        )
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.halted_at_gate, "gate3_manifest")
        self.assertIn(
            "secrets/key.pem", v.gate_result.details["violations"]
        )


# ----------------------------------------------------------------------
# T13 — gate 4 codex review + churn detection + run_phase2 chain extension.
# Design refs §3 line 129/141 + §6 S7 (canonical issue_id).
# ----------------------------------------------------------------------


class TestCanonicalIssueId(unittest.TestCase):
    """S7: sha256(file|line_range|class|msg-normalized)[:12].

    Same issue across codex rounds collides on id (whitespace-insensitive
    message normalization), enabling churn detection. Different files /
    classes / line ranges yield distinct ids.
    """

    def test_same_issue_same_id(self) -> None:
        a = canonical_issue_id(
            "src/foo.py", "L10-15", "sql_safety",
            "  Possible SQL injection.  ",
        )
        b = canonical_issue_id(
            "src/foo.py", "L10-15", "sql_safety",
            "Possible SQL injection.",
        )
        self.assertEqual(a, b)
        self.assertEqual(len(a), 12)

    def test_different_files_different_id(self) -> None:
        a = canonical_issue_id("src/foo.py", "L10-15", "sql", "x")
        b = canonical_issue_id("src/bar.py", "L10-15", "sql", "x")
        self.assertNotEqual(a, b)


class TestGate4CodexReview(unittest.TestCase):
    """Gate 4 wraps codex CLI; parses GREEN / YELLOW / RED verdict and
    persists issues with S7 canonical ids. RED + churn (same issue id 3+
    rounds) escalates without consuming retry budget (§3 line 141).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t13-gate4-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )

    def _make_runner(self) -> GateRunner:
        return GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r1", task_id="T0",
        )

    def test_gate4_red_writes_review_issues_jsonl(self) -> None:
        gr = self._make_runner()
        stub_output = json.dumps({"verdict": "RED", "issues": [
            {"file": "src/foo.py", "line_range": "L10-15",
             "class": "sql_safety", "message": "SQL injection",
             "severity": "critical"},
        ]})
        # Single-quote the JSON for shell echo; stub_output JSON contains
        # only safe characters (no single quotes inside the canned payload).
        r = gr.gate4_codex_review(codex_command=f"echo '{stub_output}'")
        self.assertEqual(r.status, "fail")
        self.assertEqual(r.details["verdict"], "RED")
        jsonl = (gr.task_dir / "review-issues.jsonl").read_text()
        issues = [json.loads(l) for l in jsonl.splitlines()]
        self.assertEqual(len(issues), 1)
        self.assertEqual(len(issues[0]["id"]), 12)  # S7

    def test_gate4_green_passes(self) -> None:
        gr = self._make_runner()
        stub_output = json.dumps({"verdict": "GREEN", "issues": []})
        r = gr.gate4_codex_review(codex_command=f"echo '{stub_output}'")
        self.assertEqual(r.status, "pass")
        self.assertEqual(r.details["verdict"], "GREEN")
        # No churn key when no issues hit the threshold (J-class watch:
        # callers may rely on `assertNotIn("churn", ...)` semantics).
        self.assertNotIn("churn", r.details)

    def test_gate4_churn_escalates_after_repeat(self) -> None:
        """Same issue_id appearing 3+ times → churn → escalate flag set."""
        gr = self._make_runner()
        stub_output = json.dumps({
            "verdict": "RED",
            "issues": [{"file": "x.py", "line_range": "L1",
                         "class": "c", "message": "m",
                         "severity": "high"}],
        })
        gr.gate4_codex_review(codex_command=f"echo '{stub_output}'")
        gr.gate4_codex_review(codex_command=f"echo '{stub_output}'")
        r = gr.gate4_codex_review(codex_command=f"echo '{stub_output}'")
        # Third hit triggers churn.
        self.assertEqual(r.status, "fail")
        self.assertTrue(r.escalate)
        self.assertIn("churn", r.details)
        self.assertEqual(len(r.details["churn"]), 1)

    def test_gate4_codex_cli_failure_returns_inconclusive(self) -> None:
        """D5 catch-all: codex CLI rc != 0 → inconclusive, not silent
        pass. Operator review owns the resolution path (§3 line 141)."""
        gr = self._make_runner()
        # `false` exits non-zero without producing JSON.
        r = gr.gate4_codex_review(codex_command="false")
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("error", r.details)

    def test_gate4_non_json_output_returns_inconclusive(self) -> None:
        """F fail-closed: malformed codex output must not be silently
        treated as GREEN. Routes to inconclusive with stdout_tail."""
        gr = self._make_runner()
        r = gr.gate4_codex_review(codex_command="echo not-json")
        self.assertEqual(r.status, "inconclusive")
        self.assertIn("stdout_tail", r.details)

    # ------------------------------------------------------------------
    # Fix-pass tests (P1-1, P1-2, P2-2 — codex review YELLOW post-T13).
    # ------------------------------------------------------------------

    def test_gate4_malformed_batch_writes_nothing(self) -> None:
        """Fix-pass P1-1: one malformed issue in a RED batch must NOT
        leave the prefix issues on disk. Pre-fix, validation happened
        per-issue during write — issues 0..k-1 leaked into
        review-issues.jsonl before issue k failed validation, inflating
        next-round churn counts on any matching id.
        """
        gr = self._make_runner()
        stub = json.dumps({
            "verdict": "RED",
            "issues": [
                {"file": "a.py", "line_range": "L1",
                 "class": "c", "message": "msg1",
                 "severity": "high"},
                {"file": "b.py"},  # missing line_range / class / message
            ],
        })
        r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(r.details["reason"], "issue_missing_required_field")
        issues_path = gr.task_dir / "review-issues.jsonl"
        # NO rows must be written when batch validation fails — even
        # the well-formed first issue is rolled back.
        self.assertFalse(
            issues_path.is_file(),
            "no rows should be written when batch validation fails",
        )

    def test_gate4_unknown_verdict_inconclusive(self) -> None:
        """Fix-pass P1-2: explicit verdict allow-list. Anything outside
        ``ALLOWED_VERDICTS`` (typo, future tag, ``"INCONCLUSIVE"``)
        MUST route to inconclusive — not silently fall through to
        ``status="pass"`` (the same fail-open pattern T9/T10 had P1s
        for).
        """
        gr = self._make_runner()
        for stub_verdict in ("BLUE", "INCONCLUSIVE", ""):
            stub = json.dumps({"verdict": stub_verdict, "issues": []})
            r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
            self.assertEqual(
                r.status, "inconclusive",
                f"verdict={stub_verdict!r} must route to inconclusive",
            )
            self.assertEqual(r.details["reason"], "unknown_verdict")
            self.assertEqual(r.details["verdict"], stub_verdict)

    def test_gate4_missing_verdict_inconclusive(self) -> None:
        """Fix-pass P1-2: a JSON object with no ``verdict`` key at all
        must also route to inconclusive (the pre-fix
        ``output.get("verdict", "INCONCLUSIVE")`` fallback then
        treated everything-but-RED as pass)."""
        gr = self._make_runner()
        stub = json.dumps({"issues": []})  # no verdict field at all
        r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(r.details["reason"], "unknown_verdict")
        # ``verdict`` key in details captures the actual seen value
        # (None for missing) so audit logs show the malformed payload.
        self.assertIsNone(r.details["verdict"])

    def test_gate4_timeout_routes_to_inconclusive(self) -> None:
        """Fix-pass P2-2: a hung codex CLI must NOT hang Phase 2.
        Override the default 600s timeout to 1s and run ``sleep 5``
        (which exceeds the override) — the gate must surface the
        timeout as ``inconclusive`` with ``reason=codex_timeout``."""
        gr = self._make_runner()
        r = gr.gate4_codex_review(
            codex_command="sleep 5",
            codex_timeout_sec=1,
        )
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(r.details["reason"], "codex_timeout")
        self.assertEqual(r.details["timeout_sec"], 1)

    # ------------------------------------------------------------------
    # Codex round-1 fix-pass tests (P1-1 pgkill, P1-2 non-string fail-
    # closed, P2-3 task-scoped churn, P2-4 per-round dedupe).
    # ------------------------------------------------------------------

    @staticmethod
    def _pgrep_sleep_children() -> set[int]:
        """Return the PIDs of every ``sleep`` process whose ppid chain
        includes the current Python process. Pure ``/proc`` scan — no
        ``pgrep`` shell-out (we are testing process-group cleanup; we
        must not use a helper that itself spawns its own children).
        """
        my_pid = os.getpid()
        # Walk /proc once, build pid → ppid + comm map.
        pid_info: dict[int, tuple[int, str]] = {}
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            pid = int(entry)
            try:
                with open(f"/proc/{pid}/stat", "r") as f:
                    line = f.read()
                # /proc/<pid>/stat: pid (comm) state ppid ...
                # ``comm`` may contain spaces or parens — locate the
                # rightmost ``)`` to find its end deterministically.
                rparen = line.rindex(")")
                comm = line[line.index("(") + 1:rparen]
                rest = line[rparen + 2:].split()
                ppid = int(rest[1])
                pid_info[pid] = (ppid, comm)
            except (FileNotFoundError, ProcessLookupError, ValueError,
                    OSError):
                continue
        # Find every PID whose ancestor chain includes my_pid.
        descendants: set[int] = set()
        for pid, (ppid, comm) in pid_info.items():
            cur = ppid
            seen = {pid}
            while cur and cur != 1 and cur not in seen:
                if cur == my_pid:
                    descendants.add(pid)
                    break
                nxt = pid_info.get(cur)
                if nxt is None:
                    break
                seen.add(cur)
                cur = nxt[0]
        return {
            pid for pid in descendants
            if pid_info.get(pid, (0, ""))[1] == "sleep"
        }

    def test_gate4_timeout_kills_child_process_group(self) -> None:
        """Codex round-1 [P1] fix-1: timeout MUST SIGKILL the entire
        process group, not just the shell. With ``shell=True``, the
        old ``subprocess.run(timeout=...)`` only signaled the shell —
        the child ``sleep 30`` survived as an orphan. The fix routes
        gate 4 through ``_run_shell_with_pgkill`` (same helper gate 1
        and gate 6 use), which calls ``killpg(SIGTERM)`` then
        ``killpg(SIGKILL)`` on the whole session.

        This test runs ``sleep 30`` with ``timeout=1`` and asserts no
        ``sleep`` PID descended from this Python process survives the
        gate's return.
        """
        gr = self._make_runner()
        pre = self._pgrep_sleep_children()
        r = gr.gate4_codex_review(
            codex_command="sleep 30",
            codex_timeout_sec=1,
        )
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(r.details["reason"], "codex_timeout")
        # Give the OS up to 2s to reap. The helper SIGKILLs by then.
        deadline = time.time() + 2
        leaked: set[int] = set()
        while time.time() < deadline:
            post = self._pgrep_sleep_children()
            leaked = post - pre
            if not leaked:
                break
            time.sleep(0.1)
        self.assertFalse(
            leaked,
            f"sleep child leaked after timeout (pgkill regression): "
            f"{leaked}",
        )

    def test_gate4_non_string_message_fail_closed(self) -> None:
        """Codex round-1 [P1] fix-2: ``"message": null`` must route
        to ``inconclusive`` (D5/F deeper). Pre-fix, presence-check
        passed but ``.lower()`` later in the canonical-id pipeline
        threw an uncaught AttributeError — NOT fail-closed.
        """
        gr = self._make_runner()
        # Build the JSON via json.dumps so ``null`` makes it through
        # the shell echo → JSON parse pipeline as a Python None.
        stub = json.dumps({
            "verdict": "RED",
            "issues": [{
                "file": "x.py", "line_range": "L1",
                "class": "c", "message": None,
                "severity": "high",
            }],
        })
        r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(
            r.details["reason"], "malformed_issue_non_string_field",
        )
        self.assertEqual(r.details["field"], "message")
        # P1-1 invariant — no rows persist on validation failure.
        issues_path = gr.task_dir / "review-issues.jsonl"
        self.assertFalse(issues_path.is_file())

    def test_gate4_non_string_file_fail_closed(self) -> None:
        """Codex round-1 [P1] fix-2: ``"file": 42`` would silently
        ``str()``-ify into the SHA hash, producing a wrong canonical
        id and unreliable churn detection. Now fail-closes."""
        gr = self._make_runner()
        stub = json.dumps({
            "verdict": "RED",
            "issues": [{
                "file": 42, "line_range": "L1",
                "class": "c", "message": "m",
                "severity": "high",
            }],
        })
        r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
        self.assertEqual(r.status, "inconclusive")
        self.assertEqual(
            r.details["reason"], "malformed_issue_non_string_field",
        )
        self.assertEqual(r.details["field"], "file")
        self.assertEqual(r.details["type"], "int")

    def test_count_issue_id_history_excludes_other_tasks(self) -> None:
        """Codex round-1 [P2] fix-3: ``review-issues.jsonl`` is
        slug-task-dir scoped, but a slug can host multiple tasks.
        Pre-fix, count was just ``rec["id"] == issue_id`` —
        previous-task entries inflated the current task's churn
        count, potentially escalating WITHOUT 3 codex rounds for
        THIS task. Now filter on ``rec["task"] == self.task_id``.
        """
        gr = self._make_runner()
        other_id = canonical_issue_id("z.py", "L9", "c", "m")
        # Pre-seed 5 entries from a DIFFERENT task with same canonical id.
        for _ in range(5):
            append_review_issue(gr.task_dir, ReviewIssueRecord(
                id=other_id,
                ts="2026-05-07T00:00:00Z",
                task="T_OTHER",  # ← different task id
                severity="high",
                reviewer="codex",
                description="m",
                disposition="open",
            ))
        # From THIS task's perspective the count must be 0 (none of
        # the entries are scoped to ``self.task_id``).
        self.assertEqual(gr._count_issue_id_in_history(other_id), 0)

    def test_gate4_dedupes_duplicate_issues_in_one_round(self) -> None:
        """Codex round-1 [P2] fix-4: 3 duplicate-id issues in one
        codex response → 1 row written, churn does NOT fire on round
        1 alone. Cross-round churn is the documented behavior;
        single-response duplicates are NOT.
        """
        gr = self._make_runner()
        dup_issue = {
            "file": "x.py", "line_range": "L1",
            "class": "c", "message": "m", "severity": "high",
        }
        stub = json.dumps({
            "verdict": "RED",
            "issues": [dup_issue, dup_issue, dup_issue],
        })
        r = gr.gate4_codex_review(codex_command=f"echo '{stub}'")
        self.assertEqual(r.status, "fail")
        self.assertFalse(
            r.escalate,
            "churn must NOT fire from dupes within 1 round",
        )
        self.assertNotIn("churn", r.details)
        issues_path = gr.task_dir / "review-issues.jsonl"
        rows = [
            json.loads(line)
            for line in issues_path.read_text().splitlines()
        ]
        self.assertEqual(
            len(rows), 1,
            "duplicates within a single round must collapse to 1 row",
        )


class TestRunPhase2InsertsGate4(unittest.TestCase):
    """T13 chain extension: full Phase 2 sequence is 1 → 3 → 4 → 5 → 6.
    Pins both ordering (gate 4 between 3 and 5) and halt semantics
    (gate 4 RED halts before gate 5 + 6 fire).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t13-phase2-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_gate_runner_ctx(
            self.tmp,
        )
        self.manifest = TaskManifest(
            id="T0", writes_declared=["src/foo.py"],
            allowed_writes=["src/foo.py"],
            out_of_scope=[], forbidden_hits=[], shared_hits=[],
        )
        self.facts = TaskFacts(
            changed_files=["src/foo.py"], newly_added_files=[],
            diff_hash="x", target_commit_pre_merge="y",
        )

    def _stub_runner_with_calls(
        self,
        gr: GateRunner,
        calls: list,
        gate4_result: GateResult,
    ) -> None:
        """Replace each gate method with a stub that records its name and
        returns a configured GateResult. Gate 4 result is parameterized
        to drive ordering vs halt-semantics tests with the same stub."""
        gr.gate1_baseline = (
            lambda **_: (calls.append("1") or GateResult(status="pass"))
        )
        gr.gate3_manifest = (
            lambda **_: (calls.append("3") or GateResult(status="pass"))
        )
        gr.gate4_codex_review = (
            lambda **_: (calls.append("4") or gate4_result)
        )
        gr.gate5_acceptance = (
            lambda **_: (calls.append("5") or GateResult(status="pass"))
        )
        gr.gate6_regression = (
            lambda **_: (calls.append("6") or GateResult(status="pass"))
        )

    def test_gate4_runs_between_gate3_and_gate5(self) -> None:
        """Pass-all dry-run records gate execution order = [1,3,4,5,6]."""
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        calls: list = []
        self._stub_runner_with_calls(
            gr, calls, GateResult(status="pass"),
        )
        v = gr.run_phase2(
            manifest=self.manifest, facts=self.facts,
            criteria=[], attempt_id="a", retry_idx=0,
            baseline_command="true",
            codex_command="echo '{\"verdict\":\"GREEN\",\"issues\":[]}'",
            smoke_command="true",
        )
        self.assertEqual(v.status, "pass")
        self.assertEqual(calls, ["1", "3", "4", "5", "6"])

    def test_gate4_red_halts_before_gate5_and_gate6(self) -> None:
        """Gate 4 RED → run_phase2 returns blocked + halted_at_gate
        ='gate4_codex_review'; gates 5 + 6 must NOT execute."""
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        calls: list = []
        self._stub_runner_with_calls(
            gr, calls,
            GateResult(status="fail", details={"verdict": "RED"}),
        )
        v = gr.run_phase2(
            manifest=self.manifest, facts=self.facts,
            criteria=[], attempt_id="a", retry_idx=0,
            baseline_command="true",
            codex_command="codex review",
            smoke_command="true",
        )
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.halted_at_gate, "gate4_codex_review")
        self.assertEqual(calls, ["1", "3", "4"])

    def test_gate4_yellow_churn_escalates_phase2(self) -> None:
        """Fix-pass P2-1: gate 4 can return ``status=pass +
        escalate=True`` on YELLOW + churn. Pre-fix, ``run_phase2``'s
        halt condition was ``if r.status != "pass":`` — escalate was
        silently dropped and the chain advanced to gate 5/6.

        Design §3 line 141: churn → escalate regardless of verdict; do
        NOT consume more retry budget. ``run_phase2`` must halt at
        gate 4 even when the verdict itself is "pass" (YELLOW).
        """
        gr = GateRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        # Pre-seed review-issues.jsonl with 2 prior occurrences of the
        # canonical id we're about to emit. The third (this round) hits
        # CHURN_THRESHOLD=3 → escalate fires.
        target_id = canonical_issue_id("x.py", "L1", "c", "m")
        for _ in range(2):
            append_review_issue(gr.task_dir, ReviewIssueRecord(
                id=target_id,
                ts="2026-05-07T00:00:00Z",
                task=gr.task_id,
                severity="high",
                reviewer="codex",
                description="m",
                disposition="open",
            ))
        # Stub out gates 1, 3, 5, 6 (cheap pass) and let gate 4 execute
        # against the real codex stub so we exercise the actual
        # status=pass + escalate=True return path that triggered the
        # P2-1 bug.
        calls: list = []
        gr.gate1_baseline = (
            lambda **_: (calls.append("1") or GateResult(status="pass"))
        )
        gr.gate3_manifest = (
            lambda **_: (calls.append("3") or GateResult(status="pass"))
        )
        # Wrap real gate4 so we can record the call AND get the genuine
        # YELLOW + churn outcome.
        real_gate4 = gr.gate4_codex_review
        gr.gate4_codex_review = (
            lambda **kw: (
                calls.append("4") or real_gate4(**kw)
            )
        )
        gr.gate5_acceptance = (
            lambda **_: (calls.append("5") or GateResult(status="pass"))
        )
        gr.gate6_regression = (
            lambda **_: (calls.append("6") or GateResult(status="pass"))
        )
        stub = json.dumps({
            "verdict": "YELLOW",
            "issues": [{
                "file": "x.py", "line_range": "L1",
                "class": "c", "message": "m",
                "severity": "high",
            }],
        })
        v = gr.run_phase2(
            manifest=self.manifest, facts=self.facts,
            criteria=[], attempt_id="a", retry_idx=0,
            baseline_command="true",
            codex_command=f"echo '{stub}'",
            smoke_command="true",
        )
        # Halts at gate 4 even though gate 4 returned status="pass".
        self.assertEqual(v.status, "blocked")
        self.assertEqual(v.halted_at_gate, "gate4_codex_review")
        self.assertEqual(v.gate_result.status, "pass")
        self.assertTrue(v.gate_result.escalate)
        # Gates 5 + 6 must NOT have executed.
        self.assertEqual(calls, ["1", "3", "4"])


# ----------------------------------------------------------------------
# T14 — MergeRunner R3 9-step sequence (steps 1-7) + R9 HEAD safety +
# detect_mid_merge_crash state machine.
# ----------------------------------------------------------------------


def _make_merge_runner_ctx(
    tmp: Path,
) -> tuple[WorktreeContext, Contract, Path]:
    """Fixture for MergeRunner tests. Builds a real repo + worktree
    (so `git merge` against `repo_root = worktree_path.parents[2]`
    actually exercises subprocess + filesystem).

    Differs from `_make_gate_runner_ctx` only in the slug — keeps test
    fixtures isolated when both gate + merge tests run in the same
    process.
    """
    _init_repo(tmp)
    ctx = create_task_worktree(
        repo_root=tmp, slug="t14demo", task_idx=0,
        integration_target="master",
    )
    contract = Contract(
        contract_schema_version=CONTRACT_SCHEMA_VERSION,
        autonomy_mode="auto_default",
        created_at="2026-05-07T00:00:00Z",
        scope_allowed=["src/**"],
        scope_forbidden=["secrets/**"],
    )
    task_dir = tmp / ".flow" / "tasks" / "t14demo"
    task_dir.mkdir(parents=True, exist_ok=True)
    return ctx, contract, task_dir


def _commit_in_worktree(ctx: WorktreeContext, filename: str, content: str) -> str:
    """Make + commit a file inside the worktree. Returns the new HEAD sha."""
    f = ctx.worktree_path / filename
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(content)
    subprocess.run(
        ["git", "-C", str(ctx.worktree_path), "add", filename],
        check=True,
    )
    subprocess.run(
        ["git", "-C", str(ctx.worktree_path), "commit", "-q",
         "-m", f"add {filename}"],
        check=True,
    )
    return subprocess.run(
        ["git", "-C", str(ctx.worktree_path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    ).stdout.strip()


def _decisions_jsonl_events(task_dir: Path) -> list[str]:
    """Return event-name list (in append order) from decisions.jsonl —
    skip non-autonomy rows (v0.8.0 records that have no `event` key)."""
    path = task_dir / "decisions.jsonl"
    if not path.is_file():
        return []
    out: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        ev = rec.get("event") if isinstance(rec, dict) else None
        if isinstance(ev, str):
            out.append(ev)
    return out


class TestMergeTaskHappyPath(unittest.TestCase):
    """R3 transactional sequence steps 2-7 happy path. Pins:
      - 4 autonomy events emitted in the right relative order.
      - Pre-merge checkpoint written.
      - Repo HEAD advances to the worktree branch's commit.
      - MergeResult fields populated (target_commit_pre_merge +
        target_commit_post_merge).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t14-merge-happy-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_merge_runner_ctx(
            self.tmp,
        )
        # Make + commit a change in the worktree branch.
        self.head_sha = _commit_in_worktree(
            self.ctx, "src/foo.py", "print('hi')\n",
        )
        self.facts = TaskFacts(
            changed_files=["src/foo.py"], newly_added_files=["src/foo.py"],
            diff_hash="x" * 64, target_commit_pre_merge=self.head_sha,
        )

    def test_merge_task_emits_4_events_in_order(self) -> None:
        """task_ready_to_merge → merge_started → merge_applied appear
        in this relative order in decisions.jsonl. (4 events =
        auto_engaged optional + the 3 above; this test asserts the 3
        merge-side events explicitly per Step 14.1 plan.)"""
        merger = MergeRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        result = merger.merge_task(
            facts=self.facts, merge_strategy="--ff-only",
        )
        self.assertEqual(result.status, "merged")
        self.assertEqual(
            result.target_commit_pre_merge, self.head_sha,
        )
        self.assertIsNotNone(result.target_commit_post_merge)
        self.assertEqual(len(result.target_commit_post_merge), 40)
        events = _decisions_jsonl_events(self.task_dir)
        # Among any other events, these 3 must appear in this order.
        for needle, prior in [
            ("task_ready_to_merge", None),
            ("merge_started", "task_ready_to_merge"),
            ("merge_applied", "merge_started"),
        ]:
            self.assertIn(needle, events)
            if prior:
                self.assertLess(
                    events.index(prior), events.index(needle),
                )

    def test_pre_merge_checkpoint_written(self) -> None:
        """Step 3 — checkpoints/<ts>.md exists with phase=pre_merge."""
        merger = MergeRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        result = merger.merge_task(
            facts=self.facts, merge_strategy="--ff-only",
        )
        self.assertEqual(result.status, "merged")
        cp_dir = self.task_dir / "checkpoints"
        self.assertTrue(cp_dir.is_dir())
        cp_files = list(cp_dir.glob("*.md"))
        self.assertEqual(len(cp_files), 1)
        body = cp_files[0].read_text(encoding="utf-8")
        self.assertIn("phase: pre_merge", body)
        self.assertIn(f"diff_hash: {self.facts.diff_hash}", body)
        self.assertIn(
            f"target_commit_pre_merge: {self.facts.target_commit_pre_merge}",
            body,
        )

    def test_progress_md_status_set_to_merging(self) -> None:
        """Step 4 — progress.md task_status marker reflects ``merging``."""
        merger = MergeRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        merger.merge_task(facts=self.facts, merge_strategy="--ff-only")
        progress_text = (self.task_dir / "progress.md").read_text(
            encoding="utf-8",
        )
        self.assertIn("task_status[T0]: merging", progress_text)


class TestMergeRefusesWrongHead(unittest.TestCase):
    """R9 HEAD safety: refuse to merge when repo HEAD is not on
    integration_target. Without this, ``git merge`` would silently merge
    into the user's currently-checked-out feature branch — a destructive
    footgun.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t14-merge-r9-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ctx, self.contract, self.task_dir = _make_merge_runner_ctx(
            self.tmp,
        )
        self.head_sha = _commit_in_worktree(
            self.ctx, "src/foo.py", "print('hi')\n",
        )
        self.facts = TaskFacts(
            changed_files=["src/foo.py"], newly_added_files=["src/foo.py"],
            diff_hash="x" * 64, target_commit_pre_merge=self.head_sha,
        )

    def test_refuses_when_head_is_feature_branch(self) -> None:
        """Repo HEAD on user-feature → block; no merge attempted."""
        # Switch repo_root HEAD to a different branch.
        subprocess.run(
            ["git", "-C", str(self.tmp), "checkout", "-q",
             "-b", "user-feature"],
            check=True,
        )
        head_before = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        merger = MergeRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        result = merger.merge_task(
            facts=self.facts, merge_strategy="--ff-only",
        )
        self.assertEqual(result.status, "blocked")
        self.assertIn(
            "refusing to merge into HEAD='user-feature'",
            result.block_reason,
        )
        self.assertIn(
            "integration_target='master'", result.block_reason,
        )
        # Repo HEAD unchanged — no merge happened.
        head_after = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head_before, head_after)
        # No merge_applied event written (pre-merge events ran first).
        events = _decisions_jsonl_events(self.task_dir)
        self.assertIn("task_ready_to_merge", events)
        self.assertIn("merge_started", events)
        self.assertNotIn("merge_applied", events)

    def test_proceeds_when_head_matches_integration_target(self) -> None:
        """Repo HEAD on master → merge applies cleanly."""
        # _init_repo + create_task_worktree leaves repo HEAD on master.
        merger = MergeRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="r", task_id="T0",
        )
        result = merger.merge_task(
            facts=self.facts, merge_strategy="--ff-only",
        )
        self.assertEqual(result.status, "merged")
        # Repo HEAD now points at the worktree's commit (ff-merge).
        head_after = subprocess.run(
            ["git", "-C", str(self.tmp), "rev-parse", "HEAD"],
            check=True, capture_output=True, text=True,
        ).stdout.strip()
        self.assertEqual(head_after, self.head_sha)


class TestMidMergeCrashDetection(unittest.TestCase):
    """Y5 gap-by-gap state machine. Pinning T19 dispatch-side recovery
    contract — a `merge_started` without paired `merge_applied` MUST
    return state=mid_merge_crash with R3 reconcile choices. M-class:
    cross-task event filter.
    """

    def setUp(self) -> None:
        self.task_dir = Path(tempfile.mkdtemp(prefix="t14-detect-"))
        self.addCleanup(shutil.rmtree, self.task_dir, ignore_errors=True)

    def _started_fields(
        self, run_id: str = "r", task_id: str = "T0",
    ) -> dict:
        return {
            "event_id": "e1",
            "ts": "2026-05-07T00:00:00Z",
            "slug": "demo",
            "run_id": run_id,
            "task_id": task_id,
            "worktree_id": "demo+t0+abc1234",
            "worktree_path": "/tmp/wt",
            "integration_target": "master",
            "target_commit_pre_merge": "deadbeef" * 5,
        }

    def _applied_fields(
        self, run_id: str = "r", task_id: str = "T0",
        event_id: str = "e2",
    ) -> dict:
        return {
            "event_id": event_id,
            "ts": "2026-05-07T00:00:01Z",
            "slug": "demo",
            "run_id": run_id,
            "task_id": task_id,
            "worktree_id": "demo+t0+abc1234",
            "target_commit_post_merge": "cafebabe" * 5,
            "merge_strategy": "--ff-only",
        }

    def _completed_fields(
        self, run_id: str = "r", task_id: str = "T0",
        event_id: str = "e3",
    ) -> dict:
        return {
            "event_id": event_id,
            "ts": "2026-05-07T00:00:02Z",
            "slug": "demo",
            "run_id": run_id,
            "task_id": task_id,
            "worktree_id": "demo+t0+abc1234",
            "final_diff_hash": "deadbeef" * 5,
            "target_commit_post_merge": "cafebabe" * 5,
        }

    def test_no_events_returns_state_none(self) -> None:
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "none")

    def test_merge_started_without_merge_applied_blocks(self) -> None:
        """R3: gap between step 5 and step 7 → mid_merge_crash."""
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED, self._started_fields(),
        )
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "mid_merge_crash")
        self.assertEqual(state["block_type"], "atomic_merge_crashed")
        for needed in (
            "replay_merge_from_diff_hash",
            "abort_and_revert_partial",
            "switch_to_interactive",
        ):
            self.assertIn(needed, state["choices"])

    def test_merge_applied_without_terminal_blocks_mid_gate8(self) -> None:
        """Y5: merge_applied without task_completed AND without
        post_merge_verify_failed → mid_gate8_crash (T15 owns gate 8)."""
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED, self._started_fields(),
        )
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_APPLIED, self._applied_fields(),
        )
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "mid_gate8_crash")
        self.assertEqual(
            state["block_type"], "post_merge_verify_in_progress_crash",
        )
        for needed in (
            "rerun_post_merge_verify",
            "abort_and_revert_partial",
            "switch_to_interactive",
        ):
            self.assertIn(needed, state["choices"])

    def test_merge_completed_after_task_completed(self) -> None:
        """Happy path: merge_applied + task_completed → merge_completed."""
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED, self._started_fields(),
        )
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_APPLIED, self._applied_fields(),
        )
        append_autonomy_event(
            self.task_dir, EVENT_TASK_COMPLETED, self._completed_fields(),
        )
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "merge_completed")

    def test_cross_task_pollution_filtered(self) -> None:
        """M-class: an unrelated (run, task) writing merge_started in
        the SAME shared decisions.jsonl MUST NOT skew our verdict."""
        # Other task crashed mid-merge.
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED,
            self._started_fields(run_id="other", task_id="T1"),
        )
        # Our task has nothing → state=none, not mid_merge_crash.
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "none")
        # Verify the other (run, task) is detected when filtered FOR it.
        other_state = detect_mid_merge_crash(
            self.task_dir, run_id="other", task_id="T1",
        )
        self.assertEqual(other_state["state"], "mid_merge_crash")

    def test_garbled_jsonl_line_does_not_crash(self) -> None:
        """D2 typed except: a single non-JSON line MUST NOT poison the
        scan; valid lines around it should still be read."""
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED, self._started_fields(),
        )
        # Inject garbage + a v0.8.0-style row (no `event` field).
        path = self.task_dir / "decisions.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write("not-valid-json\n")
            f.write('{"id": "v080-row", "ts": "x"}\n')  # no `event` key
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        self.assertEqual(state["state"], "mid_merge_crash")


if __name__ == "__main__":
    unittest.main()
