"""T15 ship-required smoke (design §7 line 317 — gate 8 post-merge verify).

Pins the contract for `Gate8VerificationRunner` (R3 step 8 + 9a/9b sub-
steps + S1 wave block + S3 skip rule):

  - Verification worktree path scheme:
    `.claude/worktrees/verify/<run_id>+t<task_idx>+<post_merge_sha[:7]>/`
    (§4 Y4 — created from `target_commit_post_merge` SHA, NOT a ref;
    N-class disk-vs-ref-identity hardening from T14).

  - PASS path (9a): emits `post_merge_verification_started` →
    `post_merge_verification_completed` → `task_completed` events in
    that order, writes a `phase=post_merge` checkpoint, cleans up BOTH
    the task worktree and the verification worktree.

  - FAIL path (9b): emits `post_merge_verify_failed`, writes
    `blocked.md` with `block_type: post_merge_verify_failed` and the
    §1 row 18 user-choice set
    `{keep_and_fix_interactive, revert_merge, split_followup,
    abort_run}`. Merge stays intact (NO auto-revert). Verification
    worktree preserved at
    `.claude/worktrees/verify/aborted/<id>+failed/`.

  - S3 skip rule: per-criterion `post_merge_skip=true` excludes from
    gate 8 (T1 enforces the cross-field rule that `type=regression`
    cannot set `post_merge_skip=true` unless
    `contract.post_merge_regression_optional=true`).

T15 pitfall coverage (per `.flow/pitfalls/claude-review-blindspots.md`):
  - I/K (helper-reuse + plausible-justification trap): Gate 8 routes
    every subprocess through `_run_argv_with_pgkill` /
    `_run_shell_with_pgkill`. The smoke covers the regression-smoke
    path so the helper choice is exercised end-to-end.
  - N (disk vs ref identity): the verification worktree is created
    via `git worktree add --detach <SHA>`; the path scheme uses the
    7-char shortsha so two reruns at different post-merge commits get
    distinct paths.
  - G2 (merge-time bypass): the verification worktree HEAD == post-
    merge SHA by construction — gate 8 reads acceptance facts from
    that fresh checkout, not from the (now-removable) task worktree.
  - L (type-check vs presence): the test asserts on event-string
    presence via `json.loads(...).get("event")` with explicit string
    match, not substring scanning.
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
    Gate8VerificationRunner,
    create_task_worktree,
)
from flow_contract import AcceptanceCriterion, Contract  # type: ignore


def _init_repo(path: Path) -> None:
    """Bootstrap a clean git repo at `path` with one commit on master."""
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


def _post_merge_commit(path: Path) -> str:
    """Add another commit on master so we have a distinct post-merge SHA.

    Adds only the README file by name — `git add .` would also pick up
    the `.claude/worktrees/...` task dir, surfacing a benign but noisy
    "embedded git repository" warning.
    """
    (path / "README.md").write_text("# demo\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "README.md"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-q", "-m", "post-merge"],
        check=True,
    )
    res = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True, capture_output=True, text=True,
    )
    return res.stdout.strip()


def _crit(desc: str = "x", *, command: str = "true",
          post_merge_skip: bool = False,
          ctype: str = "unit") -> AcceptanceCriterion:
    return AcceptanceCriterion(
        description=desc, type=ctype, method="cmd",
        command=command, timeout_sec=30,
        post_merge_skip=post_merge_skip,
    )


class _Gate8Fixture(unittest.TestCase):
    """Shared setUp that mirrors T14's MergeRunner fixtures.

    A single repo with master @ A, one task worktree at A+t0, and a
    distinct master commit B used as the post-merge SHA so the
    verification worktree path encodes a stable 7-char shortsha.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="t15-gate8-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        _init_repo(self.tmp)
        self.task_dir = self.tmp / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)
        self.ctx = create_task_worktree(
            repo_root=self.tmp, slug="demo",
            task_idx=0, integration_target="master",
        )
        # Distinct post-merge sha (advance master one commit).
        self.post_merge_sha = _post_merge_commit(self.tmp)
        self.contract = Contract(
            contract_schema_version=1, autonomy_mode="auto",
            created_at="2026-05-07T00:00:00Z",
        )

    def _decisions(self) -> list[dict]:
        path = self.task_dir / "decisions.jsonl"
        if not path.is_file():
            return []
        out: list[dict] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


class TestVerificationWorktreePath(_Gate8Fixture):
    """Step 15.1 — path scheme: run_id + t<idx> + 7-char post-merge sha."""

    def test_path_uses_run_id_taskidx_postmerge_sha(self) -> None:
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="abc12345", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        path = runner._verification_path()
        self.assertEqual(
            path.name, f"abc12345+t0+{self.post_merge_sha[:7]}",
        )
        self.assertEqual(path.parent.name, "verify")
        # Path is rooted under `<repo_root>/.claude/worktrees/verify/<name>`.
        # parents: [0]=verify [1]=worktrees [2]=.claude [3]=repo_root.
        self.assertEqual(path.parents[3], self.tmp)


class TestGate8PassPath(_Gate8Fixture):
    """Step 15.3 / 15.4 — PASS path completes 9a sequence."""

    def test_pass_path_emits_completion_and_cleans_up(self) -> None:
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        result = runner.verify(
            criteria=[_crit("x", command="true")],
            regression_command="true",
        )
        self.assertEqual(result.status, "completed")
        events = [
            r.get("event") for r in self._decisions()
            if isinstance(r.get("event"), str)
        ]
        self.assertIn("post_merge_verification_started", events)
        self.assertIn("post_merge_verification_completed", events)
        self.assertIn("task_completed", events)
        # 9a-1/2/3 ordering: started precedes completed precedes task_completed.
        i_started = events.index("post_merge_verification_started")
        i_completed = events.index("post_merge_verification_completed")
        i_task = events.index("task_completed")
        self.assertLess(i_started, i_completed)
        self.assertLess(i_completed, i_task)
        # 9a-4/5: BOTH worktrees cleaned up on PASS.
        self.assertFalse(runner._verification_path().exists())
        self.assertFalse(self.ctx.worktree_path.exists())
        # 9a-2: post_merge checkpoint written.
        cps = list((self.task_dir / "checkpoints").glob("*.md"))
        self.assertTrue(any("phase: post_merge" in p.read_text() for p in cps))


class TestGate8FailPath(_Gate8Fixture):
    """Step 15.5 / 15.6 — FAIL path 9b."""

    def test_fail_path_writes_blocked_md_and_preserves_worktree(self) -> None:
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        result = runner.verify(
            criteria=[_crit("x", command="false")],  # criterion fails
            regression_command="true",
        )
        self.assertEqual(result.status, "blocked_post_merge")
        blocked = (self.task_dir / "blocked.md").read_text()
        # §1 row 18: block_type + four user choices in blocked.md.
        self.assertIn("block_type: post_merge_verify_failed", blocked)
        for choice in (
            "keep_and_fix_interactive", "revert_merge",
            "split_followup", "abort_run",
        ):
            self.assertIn(choice, blocked)
        # Verification worktree preserved at verify/aborted/<id>+failed.
        aborted_dir = (
            self.tmp / ".claude" / "worktrees" / "verify" / "aborted"
        )
        self.assertTrue(aborted_dir.is_dir())
        preserved = list(aborted_dir.glob("*+failed"))
        self.assertEqual(len(preserved), 1)
        # The original verification path no longer exists (renamed).
        self.assertFalse(runner._verification_path().exists())
        # Merge stays intact: task worktree NOT cleaned up on FAIL (operator
        # chooses keep_and_fix_interactive / revert_merge / split / abort).
        self.assertTrue(self.ctx.worktree_path.exists())

    def test_fail_path_preserved_worktree_is_git_usable(self) -> None:
        """The whole point of preserve is post-mortem (operator runs
        ``git status``/``git log`` inside the preserved dir). Plain
        ``Path.rename`` would leave ``.git/worktrees/<id>/gitdir``
        pointing to the OLD path → broken worktree. ``git worktree
        move`` keeps the registry in sync."""
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        result = runner.verify(
            criteria=[_crit("x", command="false")],
            regression_command="true",
        )
        self.assertEqual(result.status, "blocked_post_merge")
        aborted_root = (
            self.tmp / ".claude" / "worktrees" / "verify" / "aborted"
        )
        preserved = list(aborted_root.glob("*+failed"))
        self.assertEqual(len(preserved), 1, msg=f"found: {preserved}")
        # git status inside preserved dir MUST work (registry tracks).
        proc = subprocess.run(
            ["git", "-C", str(preserved[0]), "status", "--porcelain"],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(
            proc.returncode, 0,
            msg=f"git status broken in preserved worktree: {proc.stderr}",
        )

    def test_regression_smoke_failure_routes_to_9b(self) -> None:
        """Regression smoke command rc!=0 must route to 9b even when
        all per-criterion checks pass. Pins the helper choice — must
        be `_run_shell_with_pgkill`, not plain subprocess.run."""
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        result = runner.verify(
            criteria=[_crit("x", command="true")],
            regression_command="false",  # rc=1 → fail
        )
        self.assertEqual(result.status, "blocked_post_merge")
        blocked = (self.task_dir / "blocked.md").read_text()
        self.assertIn("block_type: post_merge_verify_failed", blocked)


class TestGate8S3SkipRule(_Gate8Fixture):
    """Step 15.9 — S3: per-criterion `post_merge_skip=true` excluded."""

    def test_post_merge_skip_excludes_criterion(self) -> None:
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        criteria = [
            _crit("skipped", command="false", post_merge_skip=True),  # would fail
            _crit("kept", command="true"),
        ]
        result = runner.verify(
            criteria=criteria, regression_command="true",
        )
        # Skipped criterion never reaches the runner — gate PASSES on
        # the kept one alone, and the criteria_results list contains
        # only the non-skipped entry.
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.criteria_results), 1)


class TestGate8CodexRound1Fixes(_Gate8Fixture):
    """Codex round-1 RED — three [P1]+[P2] regressions targeted by the
    round-2 fix-pass. One test per finding so a future regression names
    itself in the failure header."""

    def test_post_merge_checkpoint_does_not_collide_with_pre_merge(self) -> None:
        """[P1] Fix-1 — checkpoint filename collision (TOCTOU).

        MergeRunner's pre_merge checkpoint and Gate8 9a's post_merge
        checkpoint are both derived from a wall-clock timestamp via
        ``write_checkpoint``. A fast task can hit the same second on
        both writes; ``write_checkpoint`` raises ``FileExistsError``
        on derived-filename clashes. The 9a path crashes AFTER
        ``task_completed`` is appended → recovery sees a completed
        task whose progress/cleanup never ran.

        Microsecond-precision timestamps disambiguate the filenames
        without a retry loop or shared lock.
        """
        from flow_state_writer import write_checkpoint  # type: ignore
        from flow_orchestrator import _now_iso  # type: ignore

        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        # Pre-write a second-precision checkpoint to simulate
        # MergeRunner's pre_merge artifact landing in the same second
        # the 9a path will hit. Same-second second-precision is
        # exactly the prior crash signature — the fix MUST tolerate
        # it.
        pre_ts = _now_iso()
        write_checkpoint(
            self.task_dir, ts=pre_ts,
            body="phase: pre_merge\nworktree_id: simulated\n",
            git_hash="aaaaaaa",
        )
        result = runner.verify(
            criteria=[_crit("x", command="true")],
            regression_command="true",
        )
        self.assertEqual(result.status, "completed")
        cps = sorted((self.task_dir / "checkpoints").glob("*.md"))
        # Both checkpoints exist — pre_merge (second precision) AND
        # post_merge (microsecond precision). No collision.
        self.assertEqual(
            len(cps), 2,
            msg=f"expected 2 checkpoints, got {[p.name for p in cps]}",
        )
        bodies = [p.read_text(encoding="utf-8") for p in cps]
        self.assertTrue(
            any("phase: pre_merge" in b for b in bodies),
            msg="pre_merge checkpoint missing from glob",
        )
        self.assertTrue(
            any("phase: post_merge" in b for b in bodies),
            msg="post_merge checkpoint missing from glob",
        )

    def test_post_merge_attempt_id_includes_task_id(self) -> None:
        """[P2] Fix-2 — attempt_id must be task-scoped.

        ``AcceptanceRunner.find_resume_point`` keys resume state on
        ``attempt_id`` only. Run-scoped ``post_merge_<run_id>`` lets
        every task in a multi-task run share the same key →
        cross-task pollution on resume.
        """
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="abc12345", task_id="T7",
            target_commit_post_merge=self.post_merge_sha,
        )
        result = runner.verify(
            criteria=[_crit("x", command="true")],
            regression_command="true",
        )
        self.assertEqual(result.status, "completed")
        accept_path = self.task_dir / "acceptance-progress.jsonl"
        self.assertTrue(
            accept_path.is_file(),
            msg="expected acceptance-progress.jsonl from gate 8 run",
        )
        rows = [
            json.loads(line)
            for line in accept_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        self.assertTrue(rows, msg="no acceptance-progress rows written")
        attempt_ids = {r.get("attempt_id") for r in rows if r.get("attempt_id")}
        # attempt_id must include BOTH the run_id and the task_id so
        # the ``post_merge_*`` key is unique per (run, task).
        for aid in attempt_ids:
            self.assertIn("T7", aid, msg=f"task_id missing from {aid!r}")
            self.assertIn("abc12345", aid, msg=f"run_id missing from {aid!r}")
            self.assertTrue(
                aid.startswith("post_merge_"),
                msg=f"unexpected attempt_id prefix: {aid!r}",
            )

    def test_post_merge_skip_preserves_original_indices_in_results(self) -> None:
        """[P2] Fix-3 — original contract indices preserved through skip.

        With criterion 0 marked ``post_merge_skip=true`` and criterion
        1 kept, the kept criterion must report ``idx=1`` (its original
        contract index) in ``criteria_results``, not ``idx=0`` (the
        post-filter enumerate position). Audit trail correctness +
        downstream resume keying both depend on this.
        """
        runner = Gate8VerificationRunner(
            ctx=self.ctx, contract=self.contract, task_dir=self.task_dir,
            run_id="run1", task_id="T0",
            target_commit_post_merge=self.post_merge_sha,
        )
        criteria = [
            _crit("skipped", command="false", post_merge_skip=True),  # idx 0
            _crit("kept", command="true"),  # idx 1
        ]
        result = runner.verify(
            criteria=criteria, regression_command="true",
        )
        self.assertEqual(result.status, "completed")
        self.assertEqual(len(result.criteria_results), 1)
        self.assertEqual(
            result.criteria_results[0]["idx"], 1,
            msg="kept criterion must keep its ORIGINAL contract idx (1), "
                "not its post-filter enumerate position (0)",
        )
        # The acceptance-progress row for the kept criterion must
        # carry criterion_idx=1 too — the resume key downstream is
        # derived from that field.
        accept_path = self.task_dir / "acceptance-progress.jsonl"
        rows = [
            json.loads(line)
            for line in accept_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        idxs = {r.get("criterion_idx") for r in rows if r.get("criterion_idx") is not None}
        self.assertIn(
            1, idxs,
            msg=f"acceptance-progress.jsonl missing criterion_idx=1; got {idxs}",
        )
        self.assertNotIn(
            0, idxs,
            msg="skipped criterion (idx 0) should not appear in acceptance "
                "progress — but it did, indicating S3 filter bypassed",
        )


if __name__ == "__main__":
    unittest.main()
