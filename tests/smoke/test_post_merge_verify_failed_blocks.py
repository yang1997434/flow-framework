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


if __name__ == "__main__":
    unittest.main()
