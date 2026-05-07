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
    WorktreeContext, TaskFacts, TaskManifest,
    DispatchOutcome, ManifestVerdict,
    GateResult, BaselineRecord, Phase2Verdict, GateRunner,
    create_task_worktree, derive_task_facts, auto_dispatch_task,
    verify_manifest_against_facts,
)
from flow_state_writer import EVENT_AUTO_ENGAGED  # type: ignore
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
        v = gr.run_phase2(
            manifest=self.manifest, facts=self.clean_facts,
            criteria=self.criteria,
            attempt_id="a1", retry_idx=0,
            baseline_command="true", smoke_command="true",
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


if __name__ == "__main__":
    unittest.main()
