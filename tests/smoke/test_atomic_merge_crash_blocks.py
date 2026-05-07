"""T14 ship-required smoke (design §7 line 317).

Pins the contract that mid-merge crash detection consumes by T19's
dispatch-side recovery path: a `merge_started` event without a paired
`merge_applied` event for the same `(run_id, task_id)` MUST surface as
`state=mid_merge_crash` with the R3 reconcile choice set
`{replay_merge_from_diff_hash, abort_and_revert_partial,
switch_to_interactive}` (design §6 line 249).

T14 owns `detect_mid_merge_crash`; T19 will route this state into the
`blocked.md` writer + Tier 1+2 notification chain. Until T19 lands, this
smoke + the unit-style tests in `test_orchestrator_worktree.py` are the
sole guarantors that the state machine returns the right verdict.

T13 pitfall coverage:
  - L (type vs presence): JSON-loaded events use `.get("event")` with
    explicit None comparison; the helper never assumes value type.
  - M (cross-task pollution): events filtered by `run_id + task_id`
    before any kind inspection — a merge_started for run="other" /
    task="T1" must NOT trigger mid_merge_crash for run="r"/task="T0".
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_orchestrator import detect_mid_merge_crash  # type: ignore
from flow_state_writer import (  # type: ignore
    EVENT_MERGE_STARTED,
    EVENT_MERGE_APPLIED,
    EVENT_TASK_COMPLETED,
    append_autonomy_event,
)


class TestAtomicMergeCrashBlocks(unittest.TestCase):
    """Ship-required smoke per design §7 line 317."""

    def setUp(self) -> None:
        self.task_dir = Path(tempfile.mkdtemp(prefix="t14-atomic-crash-"))
        self.addCleanup(shutil.rmtree, self.task_dir, ignore_errors=True)

    def _started_fields(self, run_id: str = "r", task_id: str = "T0") -> dict:
        # EVENT_MERGE_STARTED required fields per state_writer
        # EVENT_REQUIRED_FIELDS map: event_id, ts, slug, run_id, task_id,
        # worktree_id, worktree_path, integration_target,
        # target_commit_pre_merge.
        return {
            "event_id": "e1",
            "ts": "2026-05-07T00:00:00Z",
            "slug": "demo",
            "run_id": run_id,
            "task_id": task_id,
            "worktree_id": "demo+t0+abc1234",
            "worktree_path": "/tmp/wt",
            "integration_target": "master",
            "target_commit_pre_merge": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        }

    def _applied_fields(self, run_id: str = "r", task_id: str = "T0") -> dict:
        return {
            "event_id": "e2",
            "ts": "2026-05-07T00:00:01Z",
            "slug": "demo",
            "run_id": run_id,
            "task_id": task_id,
            "worktree_id": "demo+t0+abc1234",
            "target_commit_post_merge": "cafebabecafebabecafebabecafebabecafebabe",
            "merge_strategy": "--ff-only",
        }

    def test_simulated_crash_between_merge_started_and_applied(self) -> None:
        """Crash between steps 5 and 7 of R3 9-step sequence."""
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

    def test_other_task_merge_started_does_not_pollute(self) -> None:
        """M-class: events for a different (run, task) MUST NOT trigger
        a mid_merge_crash verdict for our (run, task)."""
        # Event for a DIFFERENT task in the same task_dir (M-class shared
        # state — decisions.jsonl can carry events from multiple
        # (run_id, task_id) tuples since the slug task dir is shared).
        append_autonomy_event(
            self.task_dir, EVENT_MERGE_STARTED,
            self._started_fields(run_id="other", task_id="T1"),
        )
        state = detect_mid_merge_crash(
            self.task_dir, run_id="r", task_id="T0",
        )
        # No events for our (run, task) → state="none", NOT mid_merge_crash.
        self.assertEqual(state["state"], "none")


if __name__ == "__main__":
    unittest.main()
