"""T15 ship-required unit (design §3 S1 — wave-block serialization).

Pins `MergeQueue.can_proceed`: while ANY task in the current run has
an unresolved `post_merge_verify_failed` event, every later task MUST
halt at `pending` (never enters gate 7). Earlier tasks complete; later
tasks remain queued until the operator resolves the block (T19's
recovery dispatcher writes the resolution event — this T15 unit only
checks raw event presence by `run_id`).

Pitfall coverage (per `.flow/pitfalls/claude-review-blindspots.md`):
  - M (cross-task pollution in shared state): the queue MUST filter
    decisions.jsonl by `run_id`. A `post_merge_verify_failed` from an
    older or unrelated run cannot be allowed to halt the current run.
  - L (type-check vs presence): `rec.get("event")` is compared by
    string equality only — never substring / lower() / split.
  - A (.get falsy): missing run_id / event fields skip; we never
    silently treat absent as match.
"""
from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_orchestrator import MergeQueue  # noqa: E402  type: ignore
from flow_state_writer import (  # noqa: E402  type: ignore
    EVENT_POST_MERGE_VERIFY_FAILED,
    append_autonomy_event,
)


def _failed_fields(*, run_id: str, task_id: str,
                   event_id: str = "e1") -> dict:
    return {
        "event_id": event_id,
        "ts": "2026-05-07T00:00:00Z",
        "slug": "demo",
        "run_id": run_id,
        "task_id": task_id,
        "verification_worktree_id": f"vw-{event_id}",
        "blocked_md_path": "/tmp/blocked.md",
        "user_choices": [
            "keep_and_fix_interactive", "revert_merge",
            "split_followup", "abort_run",
        ],
    }


class TestMergeQueueSerialization(unittest.TestCase):
    """S1 wave-block: a single task's failure halts the rest of the run."""

    def setUp(self) -> None:
        self.task_dir = Path(tempfile.mkdtemp(prefix="t15-mq-"))
        self.addCleanup(shutil.rmtree, self.task_dir, ignore_errors=True)

    def test_blocked_post_merge_halts_later_tasks(self) -> None:
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFY_FAILED,
            _failed_fields(run_id="r", task_id="T0"),
        )
        q = MergeQueue(task_dir=self.task_dir, run_id="r")
        # Later tasks (T1, T2, ...) MUST halt.
        self.assertFalse(q.can_proceed(task_id="T1"))
        self.assertFalse(q.can_proceed(task_id="T2"))

    def test_clean_run_proceeds(self) -> None:
        # No events yet, no decisions.jsonl → queue is open.
        q = MergeQueue(task_dir=self.task_dir, run_id="r")
        self.assertTrue(q.can_proceed(task_id="T0"))

    def test_other_run_does_not_halt_current(self) -> None:
        """M-class: a `post_merge_verify_failed` event for a DIFFERENT
        run_id MUST NOT block the current run's queue."""
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFY_FAILED,
            _failed_fields(run_id="other-run", task_id="T0"),
        )
        q = MergeQueue(task_dir=self.task_dir, run_id="r")
        self.assertTrue(q.can_proceed(task_id="T1"))


if __name__ == "__main__":
    unittest.main()
