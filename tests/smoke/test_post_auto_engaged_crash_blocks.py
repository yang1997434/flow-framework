"""T19 ship-required smoke — CrashRecoveryDispatcher 5-state classify
+ orchestrator-subprocess recovery assertions.

Owns ALL three orchestrator-entry recovery subprocess paths
(post-engaged crash + mid-merge crash + auto_prepare-lock crash) so
the test-file owner is unambiguous: T19 owns orchestrator-entry
recovery behavior; T5 + T14 unit tests stay in their respective
ship-required files.

Per design §7 line 312 (ship-required): orchestrator MUST NEVER
silently flip auto → interactive after a post-auto_engaged crash.
Each subprocess scenario asserts:

  - returncode == 3 (block exit, distinct from 0/2/4)
  - blocked.md exists with the expected block_type frontmatter
  - progress.md autonomy_mode unchanged (the no-silent-mode-switch
    hard rule)

Pitfall coverage (per .flow/pitfalls/claude-review-blindspots.md):

  - K-class (plausible-justification): every classify branch has its
    own explicit unit case; we never mock the underlying detectors.
  - F-class (fail-closed): state 1 is the ONLY legal silent fallback;
    states 2/3/4/5 ALL block. The 3 subprocess tests cover the three
    BLOCK states that are easiest to reach via fixture-only.
  - R-class (frontmatter injection): smoke tests grep for the
    exact ASCII block_type lines that write_blocked emits — this
    catches any future regression that bypasses
    _reject_frontmatter_line_separators.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
ORCH = REPO_ROOT / "scripts" / "flow_orchestrator.py"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))
from flow_orchestrator import CrashRecoveryDispatcher  # noqa: E402
from flow_state_writer import (  # noqa: E402
    AutoPrepareLock,
    write_auto_prepare_lock,
    append_autonomy_event,
    EVENT_AUTO_ENGAGED,
    EVENT_MERGE_STARTED,
    EVENT_MERGE_APPLIED,
    EVENT_POST_MERGE_VERIFICATION_STARTED,
    EVENT_POST_MERGE_VERIFICATION_COMPLETED,
    EVENT_TASK_COMPLETED,
)
from progress_meta import read_progress_meta  # noqa: E402


# ----------------------------------------------------------------------
# Unit-level dispatcher tests — single-process, no subprocess.
# ----------------------------------------------------------------------
class TestCrashRecoveryDispatcher(unittest.TestCase):
    """Steps 19.1 / 19.3 / 19.5 / 19.7 / 19.9 unit cases — direct
    classify() on dispatcher with hand-seeded fixtures.
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.task_dir = Path(self.tmp) / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    # ------------------------------------------------------------------
    # State 0 — clean.
    # ------------------------------------------------------------------
    def test_state0_clean_proceed(self):
        """Step 19.1: no fixtures + no auto intent → clean / proceed."""
        d = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        )
        v = d.classify()
        self.assertEqual(v.state, "clean")
        self.assertEqual(v.action, "proceed")

    # ------------------------------------------------------------------
    # State 1 — pre-lock crash (legal silent fallback).
    # ------------------------------------------------------------------
    def test_state1_pre_lock_crash_fail_closed_interactive(self):
        """progress.md autonomy_mode=auto BUT no lock + no engaged →
        legal silent fallback per §7 line 312 (user never opted in).
        """
        (self.task_dir / "progress.md").write_text(
            "---\n"
            "autonomy_mode: auto\n"
            "contract_path: contract.json\n"
            "---\n"
        )
        d = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        )
        v = d.classify()
        self.assertEqual(v.state, "pre_lock_crash")
        self.assertEqual(v.action, "fail_closed_interactive")

    # ------------------------------------------------------------------
    # State 2 — lock+dead-pid blocks via R10.
    # ------------------------------------------------------------------
    def test_state2_lock_dead_pid_blocks(self):
        """Step 19.3: write a lock with a guaranteed-dead pid →
        T5's detect_auto_prepare_state returns interrupted_dead_pid →
        T19 routes to block with block_type=auto_prepare_interrupted.
        """
        lock = AutoPrepareLock(
            lock_version=1, slug="demo", run_id="r1", task_id="T0",
            contract_path="/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=2 ** 31 - 1,                       # guaranteed-dead pid
            host=socket.gethostname(),
            cwd=str(self.tmp),
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        write_auto_prepare_lock(self.task_dir, lock)
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertEqual(v.state, "auto_prepare_interrupted")
        self.assertEqual(v.action, "block")
        self.assertEqual(v.block_type, "auto_prepare_interrupted")
        for needed in (
            "resume_auto_from_prepare", "abort_task",
            "switch_to_interactive",
        ):
            self.assertIn(needed, v.choices)
        # blocked.md was written by the dispatcher (back-compat path
        # since notifier=None).
        self.assertTrue((self.task_dir / "blocked.md").is_file())

    # ------------------------------------------------------------------
    # State 3 — post-auto_engaged crash blocks.
    # ------------------------------------------------------------------
    def test_state3_post_auto_engaged_crash_blocks(self):
        """Step 19.5: Q7.2 + §6/§7 contradiction-fix — post-auto_engaged
        crash MUST block + user choice — NEVER silent fallback to
        interactive.
        """
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "2026-05-06T00:00:00Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": str(Path(self.tmp) / "wt"),
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertEqual(v.state, "auto_engaged_crashed")
        self.assertEqual(v.action, "block")
        for needed in (
            "resume_from_last_safe_state", "abort_task",
            "switch_to_interactive",
        ):
            self.assertIn(needed, v.choices)

    def test_state3_progress_md_autonomy_mode_unchanged(self):
        """The hard rule (§7 line 312): post-auto_engaged crash NEVER
        silently switches mode. progress.md autonomy_mode MUST be
        readable as `auto` after classify() returns the block verdict.
        """
        (self.task_dir / "progress.md").write_text(
            "---\n"
            "autonomy_mode: auto\n"
            "contract_path: contract.json\n"
            "---\n"
        )
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "2026-05-06T00:00:00Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": str(Path(self.tmp) / "wt"),
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        meta = read_progress_meta(self.task_dir / "progress.md")
        self.assertEqual(meta.autonomy_mode, "auto")  # NOT silently flipped

    def test_state3_aborted_decision_does_not_classify_as_crash(self):
        """If a v0.8.0 `aborted_*` decision exists for this run/task,
        the run already terminated — NOT a state 3 crash.
        """
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": "/tmp/wt",
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        # Simulate a v0.8.0 DecisionRecord with `decision: aborted_*`.
        # The dispatcher's _has_post_auto_engaged_crash filters this.
        with (self.task_dir / "decisions.jsonl").open(
            "a", encoding="utf-8",
        ) as f:
            f.write(json.dumps({
                "decision": "aborted_user_request",
                "run_id": "r1", "task_id": "T0",
            }) + "\n")
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertNotEqual(v.state, "auto_engaged_crashed")

    # ------------------------------------------------------------------
    # State 4 — mid-merge dispatcher routing (consumes T14).
    # ------------------------------------------------------------------
    def test_state4_mid_merge_routes_to_block(self):
        """Step 19.7: merge_started + no merge_applied → mid_merge_crash
        with R3 reconcile choices.
        """
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w", "worktree_path": "/tmp/wt",
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        append_autonomy_event(self.task_dir, EVENT_MERGE_STARTED, {
            "event_id": "e2", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w", "worktree_path": "/tmp/wt",
            "integration_target": "master",
            "target_commit_pre_merge": "deadbeef",
        })
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertEqual(v.state, "mid_merge_crash")
        for c in (
            "replay_merge_from_diff_hash", "abort_and_revert_partial",
            "switch_to_interactive",
        ):
            self.assertIn(c, v.choices)

    # ------------------------------------------------------------------
    # State 5 — verification-worktree orphan (Y4).
    # ------------------------------------------------------------------
    def test_state5_verification_orphan_blocks(self):
        """Step 19.9: live verify worktree path AND no completion event
        → block_type=verification_worktree_orphaned.
        """
        verify_path = (
            Path(self.tmp) / ".claude" / "worktrees" / "verify"
            / "r1+t0+deadbee"
        )
        verify_path.mkdir(parents=True)
        append_autonomy_event(self.task_dir, EVENT_MERGE_APPLIED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "target_commit_post_merge": "deadbeef" + "0" * 32,
            "merge_strategy": "--ff-only",
        })
        # T15 task_completed event proves the merge path passed gate 8;
        # without it state 4 (mid_gate8_crash) would also fire. Adding
        # it here forces the dispatcher to evaluate state 5 in
        # isolation.
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e1b", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED,
            {
                "event_id": "e2", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+deadbee",
                "verification_worktree_path": str(verify_path),
                "target_commit_post_merge": "deadbeef" + "0" * 32,
            },
        )
        # No verification_completed → orphan.
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertEqual(v.state, "verification_worktree_orphaned")
        for c in (
            "rerun_post_merge_verify", "accept_merge_anyway",
            "revert_merge", "switch_to_interactive",
        ):
            self.assertIn(c, v.choices)

    # ------------------------------------------------------------------
    # T19 review round 1 [Y2] — orphan_lock_post_engaged consume + warn.
    # ------------------------------------------------------------------
    def test_state_orphan_lock_consumed_and_proceeds(self):
        """[Y2] T5 ``orphan_lock_post_engaged`` is the synthetic state
        for (lock present AND auto_engaged event also present) — prior
        run reached engagement but exited without unlocking. Per T5 §8.1
        contract: consume + WARN, NEVER block. The dispatcher must
        unlink the stale lock and fall through to subsequent state
        detection — here the journal also has ``task_completed``, so
        state-3 (post-engaged crash) does NOT fire and we route to
        clean / proceed.

        Prior to the fix the lock survived the run (silent fall-through
        with no unlink), and the next invocation would re-trigger the
        same state, etc.
        """
        # Seed the lock — pid value is irrelevant (the orphan branch
        # in detect_auto_prepare_state fires on lock+engaged co-presence
        # alone, before any pid/host/contract probing).
        lock = AutoPrepareLock(
            lock_version=1, slug="demo", run_id="r1", task_id="T0",
            contract_path="/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=2 ** 31 - 1,
            host=socket.gethostname(),
            cwd=str(self.tmp),
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        write_auto_prepare_lock(self.task_dir, lock)
        # Emit auto_engaged so detect_auto_prepare_state classifies
        # (lock_present and engaged) → orphan_lock_post_engaged.
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "2026-05-06T00:00:00Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": str(Path(self.tmp) / "wt"),
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        # task_completed terminal event — proves the run wrapped up
        # cleanly past engagement; without it state-3 (post-engaged
        # crash) would also fire and mask the orphan-lock branch.
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e2", "ts": "2026-05-06T00:00:01Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })

        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()

        # T5 §8.1 contract: orphan_lock is a "warning" state, not a
        # block. Recovery proceeds; the only side effect is the lock
        # gets unlinked + a stderr WARN.
        self.assertEqual(v.state, "clean")
        self.assertEqual(v.action, "proceed")
        # Lock consumed — file removed from disk.
        self.assertFalse(
            (self.task_dir / "auto_prepare.lock").exists(),
            "stale lock must be unlinked by _consume_stale_lock",
        )

    def test_state5_verification_completed_is_clean(self):
        """If post_merge_verification_completed fired, state 5 does
        NOT trigger even if the verify worktree path still exists on
        disk (operator may have left it for inspection).
        """
        verify_path = (
            Path(self.tmp) / ".claude" / "worktrees" / "verify"
            / "r1+t0+deadbee"
        )
        verify_path.mkdir(parents=True)
        append_autonomy_event(self.task_dir, EVENT_MERGE_APPLIED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "target_commit_post_merge": "deadbeef" + "0" * 32,
            "merge_strategy": "--ff-only",
        })
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED,
            {
                "event_id": "e2", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+deadbee",
                "verification_worktree_path": str(verify_path),
                "target_commit_post_merge": "deadbeef" + "0" * 32,
            },
        )
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_COMPLETED,
            {
                "event_id": "e3", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+deadbee",
                "status": "passed", "criteria_results": [],
            },
        )
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e4", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        # No state 5; could be clean or terminal (not our concern here).
        self.assertNotEqual(v.state, "verification_worktree_orphaned")

    # ------------------------------------------------------------------
    # [P2 codex round-1] state 5 — verification orphan id-pairing.
    # ------------------------------------------------------------------
    def test_state5_orphan_when_prior_completed_exists(self):
        """[P2] codex round-1: previous logic was
        ``if not started or completed: return None`` — meaning ANY
        completed event in the journal masked an orphaned LATER verify
        pass. Fixture: A→started, A→completed, B→started (no B→completed)
        with verify path B alive on disk → state 5 must fire.
        """
        from flow_orchestrator import CrashRecoveryDispatcher  # noqa
        verify_path_b = (
            Path(self.tmp) / ".claude" / "worktrees" / "verify"
            / "r1+t0+wt_b"
        )
        verify_path_b.mkdir(parents=True)
        append_autonomy_event(self.task_dir, EVENT_MERGE_APPLIED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "target_commit_post_merge": "deadbeef" + "0" * 32,
            "merge_strategy": "--ff-only",
        })
        # First verify pass — completed cleanly. (e.g. operator re-ran.)
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED, {
                "event_id": "e2", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+wt_a",
                "verification_worktree_path": str(self.tmp) + "/wt_a",
                "target_commit_post_merge": "deadbeef" + "0" * 32,
            },
        )
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_COMPLETED, {
                "event_id": "e3", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+wt_a",
                "status": "passed", "criteria_results": [],
            },
        )
        # Second verify pass — started, no matching completed. The B
        # worktree directory is alive on disk (orphan condition).
        append_autonomy_event(
            self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED, {
                "event_id": "e4", "ts": "t",
                "slug": "demo", "run_id": "r1", "task_id": "T0",
                "verification_worktree_id": "r1+t0+wt_b",
                "verification_worktree_path": str(verify_path_b),
                "target_commit_post_merge": "deadbeef" + "0" * 32,
            },
        )
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertEqual(v.state, "verification_worktree_orphaned")
        self.assertEqual(v.action, "block")

    def test_state5_clean_when_latest_paired(self):
        """Counter-test: A→started+completed, B→started+completed —
        both paired. State 5 must NOT fire even though there are two
        started events in the journal.
        """
        from flow_orchestrator import CrashRecoveryDispatcher  # noqa
        append_autonomy_event(self.task_dir, EVENT_MERGE_APPLIED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w",
            "target_commit_post_merge": "deadbeef" + "0" * 32,
            "merge_strategy": "--ff-only",
        })
        for letter, eid_s, eid_c in (
            ("a", "e2", "e3"), ("b", "e4", "e5"),
        ):
            wid = f"r1+t0+wt_{letter}"
            append_autonomy_event(
                self.task_dir, EVENT_POST_MERGE_VERIFICATION_STARTED, {
                    "event_id": eid_s, "ts": "t",
                    "slug": "demo", "run_id": "r1", "task_id": "T0",
                    "verification_worktree_id": wid,
                    "verification_worktree_path": str(self.tmp) + f"/wt_{letter}",
                    "target_commit_post_merge": "deadbeef" + "0" * 32,
                },
            )
            append_autonomy_event(
                self.task_dir, EVENT_POST_MERGE_VERIFICATION_COMPLETED, {
                    "event_id": eid_c, "ts": "t",
                    "slug": "demo", "run_id": "r1", "task_id": "T0",
                    "verification_worktree_id": wid,
                    "status": "passed", "criteria_results": [],
                },
            )
        v = CrashRecoveryDispatcher(
            task_dir=self.task_dir, slug="demo",
            run_id="r1", task_id="T0",
            current_contract_hash="a" * 64,
            repo_root=Path(self.tmp),
        ).classify()
        self.assertNotEqual(v.state, "verification_worktree_orphaned")

    # ------------------------------------------------------------------
    # [P2 codex round-1] _consume_stale_lock fail-closed on OSError.
    # ------------------------------------------------------------------
    def test_consume_stale_lock_permission_error_blocks(self):
        """[P2] codex round-1: PermissionError on unlink → caller MUST
        fail-closed to state-2 block instead of silently proceeding.
        Otherwise stale lock survives indefinitely + every rerun
        silently bypasses real interrupted_* signals.
        """
        from unittest import mock
        from flow_orchestrator import CrashRecoveryDispatcher  # noqa
        # Seed lock + auto_engaged → orphan_lock_post_engaged route.
        lock = AutoPrepareLock(
            lock_version=1, slug="demo", run_id="r1", task_id="T0",
            contract_path="/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=2 ** 31 - 1,
            host=socket.gethostname(),
            cwd=str(self.tmp),
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        write_auto_prepare_lock(self.task_dir, lock)
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "2026-05-06T00:00:00Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": str(Path(self.tmp) / "wt"),
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })

        # Mock Path.unlink to raise PermissionError; classify MUST
        # block on state-2 (auto_prepare_interrupted) rather than
        # silently fall through.
        original_unlink = Path.unlink

        def fake_unlink(self, *a, **kw):
            if self.name == "auto_prepare.lock":
                raise PermissionError("EACCES")
            return original_unlink(self, *a, **kw)

        with mock.patch.object(Path, "unlink", fake_unlink):
            v = CrashRecoveryDispatcher(
                task_dir=self.task_dir, slug="demo",
                run_id="r1", task_id="T0",
                current_contract_hash="a" * 64,
                repo_root=Path(self.tmp),
            ).classify()
        self.assertEqual(v.state, "auto_prepare_interrupted")
        self.assertEqual(v.action, "block")
        # The lock is STILL on disk — we couldn't remove it. That's
        # the entire reason we're blocking (otherwise the next run
        # would re-warn forever).
        self.assertTrue(
            (self.task_dir / "auto_prepare.lock").exists(),
            "lock should remain on disk when unlink fails",
        )

    def test_consume_stale_lock_file_not_found_proceeds(self):
        """[P2] codex round-1: FileNotFoundError on unlink is the
        race-safe path — another process / earlier classify() pass
        already removed it. Treat as consumed; classify proceeds.
        """
        from unittest import mock
        from flow_orchestrator import CrashRecoveryDispatcher  # noqa
        lock = AutoPrepareLock(
            lock_version=1, slug="demo", run_id="r1", task_id="T0",
            contract_path="/c.json", contract_hash="a" * 64,
            contract_schema_version=1,
            created_at="2026-05-06T00:00:00Z",
            pid=2 ** 31 - 1,
            host=socket.gethostname(),
            cwd=str(self.tmp),
            target_branch="master",
            intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
        )
        write_auto_prepare_lock(self.task_dir, lock)
        append_autonomy_event(self.task_dir, EVENT_AUTO_ENGAGED, {
            "event_id": "e1", "ts": "2026-05-06T00:00:00Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": str(Path(self.tmp) / "wt"),
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "/c.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        })
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e2", "ts": "2026-05-06T00:00:01Z",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        original_unlink = Path.unlink

        def fake_unlink(self, *a, **kw):
            if self.name == "auto_prepare.lock":
                raise FileNotFoundError(f"{self} already gone")
            return original_unlink(self, *a, **kw)

        with mock.patch.object(Path, "unlink", fake_unlink):
            v = CrashRecoveryDispatcher(
                task_dir=self.task_dir, slug="demo",
                run_id="r1", task_id="T0",
                current_contract_hash="a" * 64,
                repo_root=Path(self.tmp),
            ).classify()
        # FileNotFoundError = race-safe consume → fall through to
        # subsequent state checks; task_completed terminal makes
        # state 3 not fire → clean.
        self.assertEqual(v.state, "clean")
        self.assertEqual(v.action, "proceed")


# ----------------------------------------------------------------------
# [P1 codex round-1] _task_already_completed + skip-on-rerun.
# ----------------------------------------------------------------------
class TestAlreadyCompletedSkip(unittest.TestCase):
    """Per-task completion gate is the resume-correctness boundary.
    CrashRecoveryDispatcher only classifies CRASH states; a clean
    task_completed history for the current run_id must SKIP redispatch
    (otherwise rerunning ``--auto-execute`` re-merges already-merged
    work).
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.task_dir = Path(self.tmp) / ".flow" / "tasks" / "demo"
        self.task_dir.mkdir(parents=True)

    def test_helper_returns_true_for_matching_pair(self):
        """[P1] (run_id, task_id) match + task_completed event → True."""
        from flow_orchestrator import _task_already_completed
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w", "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        self.assertTrue(_task_already_completed(
            self.task_dir, run_id="r1", task_id="T0",
        ))

    def test_helper_returns_false_for_different_run_id(self):
        """[P1] M-class scope: task_completed for run_id=r1 MUST NOT
        mark run_id=r2 as completed.
        """
        from flow_orchestrator import _task_already_completed
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w", "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        self.assertFalse(_task_already_completed(
            self.task_dir, run_id="r2", task_id="T0",
        ))

    def test_helper_returns_false_for_different_task_id(self):
        """[P1] M-class scope: task_completed for T0 MUST NOT mark T1
        as completed.
        """
        from flow_orchestrator import _task_already_completed
        append_autonomy_event(self.task_dir, EVENT_TASK_COMPLETED, {
            "event_id": "e1", "ts": "t",
            "slug": "demo", "run_id": "r1", "task_id": "T0",
            "worktree_id": "w", "final_diff_hash": "f" * 64,
            "target_commit_post_merge": "deadbeef" + "0" * 32,
        })
        self.assertFalse(_task_already_completed(
            self.task_dir, run_id="r1", task_id="T1",
        ))

    def test_helper_returns_false_for_no_journal(self):
        """[P1] Missing journal → fail-open False (let dispatcher
        classify state-1/state-2/state-3 on the real recovery).
        """
        from flow_orchestrator import _task_already_completed
        self.assertFalse(_task_already_completed(
            self.task_dir, run_id="r1", task_id="T0",
        ))

    def test_helper_returns_false_for_post_merge_verify_failed(self):
        """[P1] D6 status guard: post_merge_verify_failed is NOT a
        success-completion event; it routes to state-3 / aborted_*
        elsewhere, NOT to skip.
        """
        from flow_orchestrator import _task_already_completed
        # Hand-write the event line (avoids T6's required-field schema
        # — we only care that the helper differentiates event names).
        (self.task_dir / "decisions.jsonl").write_text(json.dumps({
            "event": "post_merge_verify_failed",
            "run_id": "r1", "task_id": "T0",
        }) + "\n")
        self.assertFalse(_task_already_completed(
            self.task_dir, run_id="r1", task_id="T0",
        ))

    def test_helper_skips_malformed_jsonl(self):
        """[P1] D5 typed-except: a malformed line on disk is forensic
        litter, not a recovery signal. The helper skips it without
        propagating json.JSONDecodeError.
        """
        from flow_orchestrator import _task_already_completed
        # Pre-seed with a malformed line.
        (self.task_dir / "decisions.jsonl").write_text(
            "{not json\n"
            + json.dumps({
                "event": EVENT_TASK_COMPLETED,
                "run_id": "r1", "task_id": "T0",
            }) + "\n"
        )
        self.assertTrue(_task_already_completed(
            self.task_dir, run_id="r1", task_id="T0",
        ))


# ----------------------------------------------------------------------
# Subprocess tests — full orchestrator entry path. Step 19.12.
# ----------------------------------------------------------------------
def _seed_task(slug_dir: Path, *, autonomy_mode: str = "auto") -> None:
    """Write the minimal fixture for an --auto-execute invocation:
    contract.json + progress.md frontmatter + an empty manifest list
    (the recovery dispatcher runs BEFORE the manifest loop dispatches).
    """
    slug_dir.mkdir(parents=True, exist_ok=True)
    (slug_dir / "contract.json").write_text(json.dumps({
        "contract_schema_version": 1,
        "autonomy_mode": autonomy_mode,
        "created_at": "2026-05-06T00:00:00Z",
    }))
    (slug_dir / "progress.md").write_text(
        f"---\n"
        f"autonomy_mode: {autonomy_mode}\n"
        f"contract_path: contract.json\n"
        f"---\n"
        f"\n"
        f"### Tasks\n\n"
        f"```yaml\n"
        f"tasks:\n"
        f"  - id: T0\n"
        f"    writes: ['scoped/foo.py']\n"
        f"```\n"
    )


def _run_orchestrator(
    slug: str, *, repo_root: Path,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env.pop("FLOW_AUTONOMY_PARENT_PID", None)        # ensure top-level
    return subprocess.run(
        [sys.executable, str(ORCH), "--auto-execute", slug],
        cwd=str(repo_root), env=env,
        capture_output=True, text=True, timeout=30,
    )


class TestPostAutoEngagedCrashBlocksEndToEnd(unittest.TestCase):
    """ship-required (§7 line 312): orchestrator MUST NEVER silently
    flip auto → interactive after a post-auto_engaged crash. All 3
    subprocess scenarios assert: returncode == 3, blocked.md exists,
    progress.md autonomy_mode unchanged.
    """

    def setUp(self):
        self.repo_root = Path(tempfile.mkdtemp(prefix="flow-test-"))
        self.addCleanup(
            lambda: shutil.rmtree(self.repo_root, ignore_errors=True),
        )
        subprocess.run(
            ["git", "init", "-q", str(self.repo_root)], check=True,
        )
        # Minimal repo state: one commit on master so integration_target
        # exists for the dispatcher's git-state checks.
        (self.repo_root / "README.md").write_text("# fixture\n")
        subprocess.run(
            ["git", "-C", str(self.repo_root), "add", "README.md"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_root), "commit",
             "-q", "-m", "init"],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        self.slug = "demo"
        self.slug_dir = self.repo_root / ".flow" / "tasks" / self.slug
        _seed_task(self.slug_dir)

    def test_orchestrator_subprocess_blocks_on_post_engaged_crash(self):
        # Seed the post-auto_engaged crash state: auto_engaged event
        # written, no terminal event.
        (self.slug_dir / "decisions.jsonl").write_text(json.dumps({
            "event": "auto_engaged", "event_id": "e1",
            "ts": "2026-05-06T00:00:00Z",
            "slug": self.slug, "run_id": "r1", "task_id": "T0",
            "worktree_id": "demo+t0+abcdefg",
            "worktree_path": "/tmp/wt",
            "original_base_commit": "a" * 40,
            "current_base_commit": "a" * 40,
            "lifecycle_state": "active", "checkpoint_id": None,
            "contract_path": "contract.json", "contract_hash": "a" * 64,
            "contract_schema_version": 1,
        }) + "\n")
        r = _run_orchestrator(self.slug, repo_root=self.repo_root)
        self.assertEqual(
            r.returncode, 3,
            msg=(
                f"expected exit 3 (block); got {r.returncode}\n"
                f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
            ),
        )
        blocked = (self.slug_dir / "blocked.md").read_text()
        self.assertIn("auto_engaged_crashed", blocked)
        self.assertIn("resume_from_last_safe_state", blocked)
        # The hard rule: autonomy_mode MUST NOT be silently flipped.
        progress = (self.slug_dir / "progress.md").read_text()
        self.assertIn("autonomy_mode: auto", progress)

    def test_orchestrator_subprocess_blocks_on_mid_merge_crash(self):
        # R3: merge_started written, no merge_applied. T14's
        # detect_mid_merge_crash + T19's CrashRecoveryDispatcher route
        # to atomic_merge_crashed block.
        events = [
            {"event": "auto_engaged", "event_id": "e1", "ts": "t",
             "slug": self.slug, "run_id": "r1", "task_id": "T0",
             "worktree_id": "demo+t0+abcdefg",
             "worktree_path": "/tmp/wt",
             "original_base_commit": "a" * 40,
             "current_base_commit": "a" * 40,
             "lifecycle_state": "active", "checkpoint_id": None,
             "contract_path": "contract.json",
             "contract_hash": "a" * 64,
             "contract_schema_version": 1},
            {"event": "merge_started", "event_id": "e2", "ts": "t",
             "slug": self.slug, "run_id": "r1", "task_id": "T0",
             "worktree_id": "demo+t0+abcdefg",
             "worktree_path": "/tmp/wt",
             "integration_target": "master",
             "target_commit_pre_merge": "deadbeef"},
        ]
        (self.slug_dir / "decisions.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
        )
        r = _run_orchestrator(self.slug, repo_root=self.repo_root)
        self.assertEqual(
            r.returncode, 3, msg=f"stderr={r.stderr!r}",
        )
        blocked = (self.slug_dir / "blocked.md").read_text()
        self.assertIn("atomic_merge_crashed", blocked)
        self.assertIn("replay_merge_from_diff_hash", blocked)
        self.assertIn(
            "autonomy_mode: auto",
            (self.slug_dir / "progress.md").read_text(),
        )

    def test_orchestrator_subprocess_blocks_on_auto_prepare_lock_crash(self):
        """R10 + T5 §8.1: auto_prepare.lock present + contract_hash
        mismatch (T5 detection order: contract-mismatch is checked
        BEFORE pid-liveness per flow_state_writer.py:1012-1014)
        → ``interrupted_contract_changed`` → block with
        ``auto_prepare_interrupted``. Both ``interrupted_*`` paths
        share the same block_type, so the assertion remains tight.
        T19 review round 1 [M1]: docstring updated to reflect actual
        T5 detection order — the prior text claimed "dead pid" but
        the orchestrator runs with current_contract_hash="a"*64
        (default for an unseeded contract.json) which never matches
        the lock's "a"*64 hash. Pure dead-pid path is exercised by
        ``test_orchestrator_subprocess_blocks_on_dead_pid_lock_crash``.
        """
        (self.slug_dir / "auto_prepare.lock").write_text(json.dumps({
            "lock_version": 1, "slug": self.slug,
            "run_id": "r1", "task_id": "T0",
            "contract_path": "contract.json",
            "contract_hash": "a" * 64,
            "contract_schema_version": 1,
            "created_at": "2026-05-06T00:00:00Z",
            "pid": 2 ** 31 - 1,                 # guaranteed-dead pid
            "host": socket.gethostname(),
            "cwd": str(self.slug_dir),
            "target_branch": "master",
            "intended_first_task_dispatch_at": "2026-05-06T00:00:01Z",
        }))
        r = _run_orchestrator(self.slug, repo_root=self.repo_root)
        self.assertEqual(
            r.returncode, 3, msg=f"stderr={r.stderr!r}",
        )
        blocked = (self.slug_dir / "blocked.md").read_text()
        self.assertIn("auto_prepare_interrupted", blocked)
        self.assertIn("resume_auto_from_prepare", blocked)
        self.assertIn(
            "autonomy_mode: auto",
            (self.slug_dir / "progress.md").read_text(),
        )

    def test_orchestrator_subprocess_blocks_on_dead_pid_lock_crash(self):
        """T19 review round 1 [M1]: pure dead-pid path coverage.
        Compute the real ``sha256(contract.json)`` and seed the lock
        with it so T5's contract-mismatch check passes; detection then
        falls through to host check (matches via ``socket.gethostname``)
        and finally pid liveness, where ``2**31 - 1`` is guaranteed
        dead. State: ``interrupted_dead_pid`` → same block_type
        ``auto_prepare_interrupted``. Both T5 routes are now
        independently asserted at the subprocess boundary.
        """
        import hashlib
        contract_path = self.slug_dir / "contract.json"
        real_hash = hashlib.sha256(contract_path.read_bytes()).hexdigest()
        (self.slug_dir / "auto_prepare.lock").write_text(json.dumps({
            "lock_version": 1, "slug": self.slug,
            "run_id": "r1", "task_id": "T0",
            "contract_path": "contract.json",
            "contract_hash": real_hash,         # match → bypass mismatch
            "contract_schema_version": 1,
            "created_at": "2026-05-06T00:00:00Z",
            "pid": 2 ** 31 - 1,                 # guaranteed-dead pid
            "host": socket.gethostname(),
            "cwd": str(self.slug_dir),
            "target_branch": "master",
            "intended_first_task_dispatch_at": "2026-05-06T00:00:01Z",
        }))
        r = _run_orchestrator(self.slug, repo_root=self.repo_root)
        self.assertEqual(
            r.returncode, 3, msg=f"stderr={r.stderr!r}",
        )
        blocked = (self.slug_dir / "blocked.md").read_text()
        self.assertIn("auto_prepare_interrupted", blocked)
        self.assertIn("resume_auto_from_prepare", blocked)
        self.assertIn(
            "autonomy_mode: auto",
            (self.slug_dir / "progress.md").read_text(),
        )


class TestAlreadyCompletedSkipSubprocess(unittest.TestCase):
    """[P1] codex round-1: rerun against a journal with task_completed
    must SKIP redispatch instead of re-merging. This is the smoking-gun
    integration test — without the helper, the manifest loop calls
    auto_dispatch_task again and rewrites blocked.md / re-creates the
    worktree.
    """

    def setUp(self):
        self.repo_root = Path(tempfile.mkdtemp(prefix="flow-test-"))
        self.addCleanup(
            lambda: shutil.rmtree(self.repo_root, ignore_errors=True),
        )
        subprocess.run(
            ["git", "init", "-q", str(self.repo_root)], check=True,
        )
        (self.repo_root / "README.md").write_text("# fixture\n")
        subprocess.run(
            ["git", "-C", str(self.repo_root), "add", "README.md"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.repo_root), "commit",
             "-q", "-m", "init"],
            check=True,
            env={
                **os.environ,
                "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
            },
        )
        self.slug = "demo"
        self.slug_dir = self.repo_root / ".flow" / "tasks" / self.slug
        _seed_task(self.slug_dir)

    def test_completed_task_skipped_on_rerun(self):
        """Seed auto_engaged + post_merge_verification_completed +
        task_completed for run_id=r1 task_id=T0; rerun --auto-execute;
        expect INFO skip message + return 0 (all manifests done).
        """
        # Build a clean terminal sequence so _resolve_or_create_run_id
        # picks up r1, then _task_already_completed returns True for T0.
        events = [
            {
                "event": "auto_engaged", "event_id": "e1",
                "ts": "2026-05-06T00:00:00Z",
                "slug": self.slug, "run_id": "r1", "task_id": "T0",
                "worktree_id": "demo+t0+abcdefg",
                "worktree_path": "/tmp/wt",
                "original_base_commit": "a" * 40,
                "current_base_commit": "a" * 40,
                "lifecycle_state": "active", "checkpoint_id": None,
                "contract_path": "contract.json",
                "contract_hash": "a" * 64,
                "contract_schema_version": 1,
            },
            {
                "event": "task_completed", "event_id": "e2",
                "ts": "2026-05-06T00:00:01Z",
                "slug": self.slug, "run_id": "r1", "task_id": "T0",
                "worktree_id": "demo+t0+abcdefg",
                "final_diff_hash": "f" * 64,
                "target_commit_post_merge": "deadbeef" + "0" * 32,
            },
        ]
        (self.slug_dir / "decisions.jsonl").write_text(
            "\n".join(json.dumps(e) for e in events) + "\n",
        )
        r = _run_orchestrator(self.slug, repo_root=self.repo_root)
        # Expected: skip → all manifests done → return 0; stderr
        # carries the INFO line so operators can see what happened.
        self.assertEqual(
            r.returncode, 0,
            msg=(
                f"expected exit 0 (skip + done); got {r.returncode}\n"
                f"stdout={r.stdout!r}\nstderr={r.stderr!r}"
            ),
        )
        self.assertIn("already completed for run r1", r.stderr)
        # No new dispatch → no new auto_engaged event (only the seeded
        # one). If skip failed and dispatch ran, we'd see auto_engaged
        # from the rerun appended to the journal.
        records = [
            json.loads(ln)
            for ln in (
                self.slug_dir / "decisions.jsonl"
            ).read_text().splitlines() if ln
        ]
        engaged_events = [
            r for r in records if r.get("event") == "auto_engaged"
        ]
        self.assertEqual(
            len(engaged_events), 1,
            f"redispatch happened — found {len(engaged_events)} "
            f"auto_engaged events (expected 1 from seed only)",
        )


if __name__ == "__main__":
    unittest.main()
