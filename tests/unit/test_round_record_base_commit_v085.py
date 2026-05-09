"""v0.8.5 codex-review I4 — RoundRecord.base_commit + Round N>2 enrich.

Codex review I4 root cause:
1. Before Round N+1, ``failed_rounds[-1]`` may still be Round N-1
   (Round N's record gets appended only inside ``_prod_impl`` AFTER
   Round N+1's worktree is created).
2. ``RoundRecord`` doesn't carry the round's base_commit, so the
   fallback ``HEAD~1`` doesn't represent the round's true diff.

Fix:
1. Add ``base_commit`` field to ``RoundRecord`` (default empty string
   for backward compat with frozen-dataclass + existing call sites).
2. ``_build_prev_round_diff_summary`` ALWAYS prefers
   ``state.current_round_ctx`` (which is — at retry-loop top — the
   round whose review just returned non-pass = the true prev round
   for the upcoming round). ``failed_rounds`` is for orphan-cleanup,
   NOT enrichment lookup.
3. When falling back to a RoundRecord (defensive path), use its
   ``base_commit`` if populated, else degrade to empty (skip
   enrichment) rather than the wrong ``HEAD~1``.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import flow_orchestrator as fo  # noqa: E402  type: ignore


class RoundRecordHasBaseCommit(unittest.TestCase):
    def test_round_record_carries_base_commit(self) -> None:
        rec = fo.RoundRecord(
            worktree_id="t+t0+abc",
            worktree_path=Path("/tmp/x"),
            branch="t+t0+abc",
            round_num=2,
            base_commit="abc1234deadbeef",
        )
        self.assertEqual(rec.base_commit, "abc1234deadbeef")

    def test_round_record_default_base_commit_empty(self) -> None:
        # Backward compat: legacy callers omit base_commit.
        rec = fo.RoundRecord(
            worktree_id="t+t0+abc",
            worktree_path=Path("/tmp/x"),
            branch="t+t0+abc",
            round_num=1,
        )
        self.assertEqual(rec.base_commit, "")

    def test_from_ctx_populates_base_commit(self) -> None:
        ctx = fo.WorktreeContext(
            slug="t",
            task_idx=0,
            worktree_id="t+t0+abc",
            worktree_path=Path("/tmp/x"),
            branch="t+t0+abc",
            integration_target="master",
            original_base_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            current_base_commit="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            base_shortsha="aaaaaaa",
            lifecycle_state="active",
            created_at="2026-05-09T00:00:00Z",
            round_num=2,
        )
        rec = fo.RoundRecord.from_ctx(ctx)
        # I4: from_ctx must capture the round's true base commit so
        # enrichment can use it later (instead of the wrong HEAD~1).
        self.assertEqual(
            rec.base_commit,
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        )


class EnrichmentLookupPrefersCurrentRoundCtx(unittest.TestCase):
    """When entering Round N+1, ``state.current_round_ctx`` is the
    round whose review just failed = the TRUE prev round. Even if
    ``failed_rounds`` has earlier records, enrichment must use
    current_round_ctx — not failed_rounds[-1]."""

    def test_round_3_enrichment_uses_round_2_not_round_1(self) -> None:
        # Simulate the state machine at the top of Round 3:
        #   - Round 1 already in failed_rounds
        #   - Round 2 is current_round_ctx (its review returned fail)
        #   - dispatch_retry_rounds == 2 (Round 1 + Round 2 both failed)
        round1_ctx = fo.WorktreeContext(
            slug="t", task_idx=0, worktree_id="t+t0+r1+aaa",
            worktree_path=Path("/tmp/r1"), branch="b1",
            integration_target="master",
            original_base_commit="0" * 40, current_base_commit="0" * 40,
            base_shortsha="0000000", lifecycle_state="active",
            created_at="2026-05-09T00:00:00Z", round_num=1,
        )
        round2_ctx = fo.WorktreeContext(
            slug="t", task_idx=0, worktree_id="t+t0+r2+bbb",
            worktree_path=Path("/tmp/r2"), branch="b2",
            integration_target="master",
            original_base_commit="b" * 40, current_base_commit="b" * 40,
            base_shortsha="bbbbbbb", lifecycle_state="active",
            created_at="2026-05-09T00:01:00Z", round_num=2,
        )
        state = fo.RetrySessionState(
            task_slug="t",
            dispatch_retry_rounds=2,
            current_round_ctx=round2_ctx,
            failed_rounds=[fo.RoundRecord.from_ctx(round1_ctx)],
            feedback_enrichment_enabled=True,
        )

        # Patch the diff_summary helper so we can capture which path
        # / base it was called with (we don't need the actual git
        # data here).
        seen = {}

        def _fake_build(*, worktree_path, base_ref):
            seen["worktree_path"] = worktree_path
            seen["base_ref"] = base_ref
            return "MAP"

        from common import diff_summary as ds  # type: ignore
        orig = ds.build_diff_summary
        ds.build_diff_summary = _fake_build  # type: ignore
        try:
            result = fo._build_prev_round_diff_summary(state)
        finally:
            ds.build_diff_summary = orig  # type: ignore

        self.assertEqual(result, "MAP")
        # The enrichment MUST point at Round 2's worktree, not Round 1's.
        self.assertEqual(seen["worktree_path"], Path("/tmp/r2"))
        # And use Round 2's base commit, not "HEAD~1" / Round 1.
        self.assertEqual(seen["base_ref"], "b" * 40)


if __name__ == "__main__":
    unittest.main()
