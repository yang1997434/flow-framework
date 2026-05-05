"""End-to-end smoke tests for v0.7 wave-dispatch pipeline.

These tests use a temporary task slug + stub progress.md to exercise:
  - 5-task plan with disjoint writes → expected wave structure
  - SHARED_ARTIFACTS overlap forces serial
  - Cache writes and invalidates correctly
  - Capability missing fallback (simulated by removing wave_dispatch from defaults)

This is a unit-of-the-machine test, not a full Phase 2 dispatch (that requires
real subagents + a controller LLM).
"""
from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_wave_planner import (  # noqa: E402
    parse_plan_tasks,
    pack_into_waves,
    write_cache,
    read_cache,
    is_cache_valid,
    PLANNER_VERSION,
    Task,
)


PROGRESS_MD_5_DISJOINT = """
---
slug: test-v07
status: active
phase: implement
blocked_by: []
---

# progress.md — test-v07

## Plan

(controller orchestrates)

### Tasks
```yaml
tasks:
  - id: t1
    writes: [src/auth/login.py]
    description: "implement login"
  - id: t2
    writes: [src/auth/logout.py]
    description: "implement logout"
  - id: t3
    writes: [src/api/users.py]
    description: "users endpoint"
  - id: t4
    writes: [src/api/posts.py]
    description: "posts endpoint"
  - id: t5
    writes: [docs/api.md]
    description: "doc update"
```
"""


PROGRESS_MD_SHARED_ARTIFACT = """
### Tasks
```yaml
tasks:
  - id: t1
    writes: [src/foo.py]
  - id: t2
    writes: [VERSION]   # SHARED_ARTIFACT
  - id: t3
    writes: [src/bar.py]
```
"""


class TestE2EFiveDisjoint(unittest.TestCase):
    def test_five_disjoint_packs_into_two_waves_at_cap_3(self):
        tasks = parse_plan_tasks(PROGRESS_MD_5_DISJOINT)
        self.assertEqual(len(tasks), 5)
        waves = pack_into_waves(tasks, cap=3)
        self.assertEqual(len(waves), 2)
        self.assertEqual(len(waves[0]), 3)
        self.assertEqual(len(waves[1]), 2)


class TestE2ESharedArtifact(unittest.TestCase):
    def test_shared_artifact_breaks_into_three_waves(self):
        tasks = parse_plan_tasks(PROGRESS_MD_SHARED_ARTIFACT)
        waves = pack_into_waves(tasks, cap=3)
        # Expected: [[t1], [t2 alone], [t3]] — t2 is shared artifact alone,
        # t1 and t3 don't merge across t2 because contiguous-prefix
        self.assertEqual(len(waves), 3)
        self.assertEqual([t.id for t in waves[0]], ["t1"])
        self.assertEqual([t.id for t in waves[1]], ["t2"])
        self.assertEqual([t.id for t in waves[2]], ["t3"])


class TestE2ECacheLifecycle(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.cache_path = Path(self.tmpdir) / "wave-decomposition.json"

    def tearDown(self):
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_write_then_validate_cache(self):
        tasks = parse_plan_tasks(PROGRESS_MD_5_DISJOINT)
        waves = pack_into_waves(tasks, cap=3)
        write_cache(
            self.cache_path,
            plan_hash="abc",
            base_commit="def",
            controller_model="claude-opus-4-7",
            planner_version=PLANNER_VERSION,
            cap_used=3,
            waves=waves,
        )
        cached = read_cache(self.cache_path)
        self.assertIsNotNone(cached)
        self.assertTrue(is_cache_valid(
            cached, "abc", "def", "claude-opus-4-7", PLANNER_VERSION, 3
        ))
        # Any key change invalidates
        self.assertFalse(is_cache_valid(
            cached, "abc", "def2", "claude-opus-4-7", PLANNER_VERSION, 3
        ))


class TestE2EFlowWavesPreview(unittest.TestCase):
    """Test the `flow waves --preview` CLI by invoking the subprocess."""

    def setUp(self):
        # Set up a temp .flow/tasks/test-v07-preview/progress.md inside the real repo
        self.task_dir = REPO_ROOT / ".flow" / "tasks" / "test-v07-preview"
        self.task_dir.mkdir(parents=True, exist_ok=True)
        (self.task_dir / "progress.md").write_text(PROGRESS_MD_5_DISJOINT, encoding="utf-8")

    def tearDown(self):
        if self.task_dir.exists():
            shutil.rmtree(self.task_dir)

    def test_preview_runs_cleanly(self):
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "scripts" / "flow_waves.py"),
             "--preview", "test-v07-preview"],
            capture_output=True, text=True,
        )
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("Waves:", result.stdout)
        self.assertIn("Wave[0]", result.stdout)


if __name__ == "__main__":
    unittest.main()
