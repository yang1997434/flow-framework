"""v0.8.3 P0.1 — mini-integration test for fresh-worktree-per-round.

Spins a real tmp git repo + drives the helper across multiple rounds
to verify what unit fakes can't:

- AC2: Round 2 worktree path is genuinely different from Round 1's
  (`+r2+` discriminator works on a real git repo).
- T-G: round_num appears in worktree_id; concurrent rounds don't
  collide; Round 1 keeps legacy naming.
- T-D-prelude: helper produces a WorktreeContext whose `worktree_path`
  exists on disk and is registered in `git worktree list`.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_orchestrator as fo  # noqa: E402  type: ignore
from flow_orchestrator import (  # noqa: E402  type: ignore
    _dispatch_implementer_fresh_worktree,
    create_task_worktree,
)


class TestFreshWorktreePerRound(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Path(self.tmp.name)
        # `git -c user.email=... commit` is allowed inside subprocess
        # (the pre-commit-review hook is a Bash hook on the user's
        # session, not a git hook on the repo). The fixture keeps no
        # subagent state across runs.
        subprocess.run(
            ["git", "init", "-q", "-b", "master", "."],
            cwd=self.repo, check=True,
        )
        subprocess.run(
            [
                "git", "-c", "user.email=t@t", "-c", "user.name=t",
                "commit", "--allow-empty", "-m", "init", "-q",
            ],
            cwd=self.repo, check=True,
        )

        # Patch out the subagent shim — we test the helper's worktree +
        # facts machinery, NOT the T22 dispatch capability.
        self._orig_dispatch = fo._invoke_subagent_dispatch
        fo._invoke_subagent_dispatch = lambda ctx, **kw: None

    def tearDown(self):
        fo._invoke_subagent_dispatch = self._orig_dispatch
        self.tmp.cleanup()

    def test_round_1_uses_legacy_naming(self):
        ctx = create_task_worktree(
            repo_root=self.repo, slug="demo", task_idx=0,
            integration_target="master",
        )
        self.assertNotIn("+r", ctx.worktree_id)
        self.assertEqual(ctx.round_num, 1)
        self.assertTrue(ctx.worktree_path.exists())

    def test_round_2_path_distinct_from_round_1(self):
        """AC2 + T-G: Round 2 fresh worktree is at a different path
        with `+r2+` in its id."""
        ctx1 = create_task_worktree(
            repo_root=self.repo, slug="demo", task_idx=0,
            integration_target="master",
        )
        ctx2, facts2, _ = _dispatch_implementer_fresh_worktree(
            repo_root=self.repo, slug="demo", task_id="task-x",
            task_idx=0, integration_target="master",
            prompt_prefix="r2 brief", round_num=2,
        )
        self.assertNotEqual(ctx1.worktree_path, ctx2.worktree_path)
        self.assertNotEqual(ctx1.worktree_id, ctx2.worktree_id)
        self.assertIn("+r2+", ctx2.worktree_id)
        self.assertEqual(ctx2.round_num, 2)
        self.assertTrue(ctx2.worktree_path.exists())
        # Round 2 starts from base (clean diff against integration_target).
        self.assertEqual(facts2.changed_files, [])

    def test_three_rounds_unique_paths(self):
        """T-G: Round 1, 2, 3 all distinct."""
        ctx1 = create_task_worktree(
            repo_root=self.repo, slug="demo", task_idx=0,
            integration_target="master",
        )
        ctx2, _, _ = _dispatch_implementer_fresh_worktree(
            repo_root=self.repo, slug="demo", task_id="t",
            task_idx=0, integration_target="master",
            prompt_prefix="", round_num=2,
        )
        ctx3, _, _ = _dispatch_implementer_fresh_worktree(
            repo_root=self.repo, slug="demo", task_id="t",
            task_idx=0, integration_target="master",
            prompt_prefix="", round_num=3,
        )
        ids = {ctx1.worktree_id, ctx2.worktree_id, ctx3.worktree_id}
        self.assertEqual(len(ids), 3, "all 3 round ids must be unique")
        # `git worktree list` registers all of them.
        wt_list = subprocess.run(
            ["git", "worktree", "list"],
            cwd=self.repo, capture_output=True, text=True, check=True,
        ).stdout
        for ctx in (ctx1, ctx2, ctx3):
            self.assertIn(ctx.worktree_id, wt_list)


if __name__ == "__main__":
    unittest.main()
