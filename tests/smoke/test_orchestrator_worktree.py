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
    create_task_worktree, derive_task_facts, auto_dispatch_task,
    verify_manifest_against_facts,
)
from flow_state_writer import EVENT_AUTO_ENGAGED  # type: ignore
from flow_contract import Contract, CONTRACT_SCHEMA_VERSION  # type: ignore


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


if __name__ == "__main__":
    unittest.main()
