"""T5 ship-required smoke — `auto_prepare.lock` crash detection contract.

Per design §7 ship-required test list. T5 lands the writer/detector half;
the orchestrator subprocess + `blocked.md` assertion is grown in T19's
recovery dispatcher (see `feat+v0.8.1-safety-stack` plan T19). This smoke
pins the detect-side contract so T19 has a stable foundation to build on.

# T19 will extend this with orchestrator subprocess + blocked.md assertion.
"""
import json
import os
import socket
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_state_writer import (
    AutoPrepareLock,
    write_auto_prepare_lock,
    detect_auto_prepare_state,
)


class TestAutoPrepareLockCrashDetected(unittest.TestCase):
    """T5 phase: writer/detector contract.

    T19 grows this with orchestrator subprocess + blocked.md assertion.
    """

    def test_dead_pid_lock_classified_as_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            lock = AutoPrepareLock(
                lock_version=1, slug="demo", run_id="r", task_id="T1",
                contract_path="/c.json", contract_hash="a" * 64,
                contract_schema_version=1,
                created_at="2026-05-06T00:00:00Z",
                pid=2**31 - 1,  # outside OS pid range
                host=socket.gethostname(), cwd=td, target_branch="master",
                intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
            )
            write_auto_prepare_lock(Path(td), lock)
            r = detect_auto_prepare_state(
                Path(td), run_id="r", task_id="T1",
                current_contract_hash="a" * 64,
            )
            self.assertEqual(r["state"], "interrupted_dead_pid")
            self.assertEqual(r["block_type"], "auto_prepare_interrupted")

    def test_contract_change_under_lock_classified_as_interrupted(self):
        with tempfile.TemporaryDirectory() as td:
            lock = AutoPrepareLock(
                lock_version=1, slug="demo", run_id="r", task_id="T1",
                contract_path="/c.json", contract_hash="old" * 22,
                contract_schema_version=1,
                created_at="2026-05-06T00:00:00Z",
                pid=os.getpid(), host="x", cwd=td,
                target_branch="master",
                intended_first_task_dispatch_at="2026-05-06T00:00:01Z",
            )
            write_auto_prepare_lock(Path(td), lock)
            r = detect_auto_prepare_state(
                Path(td), run_id="r", task_id="T1",
                current_contract_hash="new" * 22,  # mismatch
            )
            self.assertEqual(r["state"], "interrupted_contract_changed")
            self.assertEqual(r["block_type"], "auto_prepare_interrupted")


if __name__ == "__main__":
    unittest.main()
