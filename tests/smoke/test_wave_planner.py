"""Smoke tests for flow_wave_planner — Phase 1: parser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_wave_planner import parse_plan_tasks, Task, PlanError, _parse_task_yaml  # noqa: E402


class TestParsePlanTasks(unittest.TestCase):
    def test_parse_minimal_yaml_block(self):
        progress_md = """
# progress.md

## Plan

(prose context)

### Tasks
```yaml
tasks:
  - id: t1
    writes: [src/auth/login.py]
  - id: t2
    writes: [src/api/handlers.py]
```
"""
        tasks = parse_plan_tasks(progress_md)
        self.assertEqual(len(tasks), 2)
        self.assertEqual(tasks[0].id, "t1")
        self.assertEqual(tasks[0].writes, ["src/auth/login.py"])
        self.assertEqual(tasks[1].id, "t2")

    def test_no_tasks_section_returns_empty(self):
        progress_md = """
## Plan

(single, main session implements)
"""
        tasks = parse_plan_tasks(progress_md)
        self.assertEqual(tasks, [])

    def test_missing_writes_field_kept(self):
        # writes is optional; missing → None (signals strict serial)
        progress_md = """
### Tasks
```yaml
tasks:
  - id: t1
    description: "do stuff"
```
"""
        tasks = parse_plan_tasks(progress_md)
        self.assertEqual(tasks[0].writes, None)

    def test_stray_top_level_key_after_tasks_does_not_corrupt(self):
        # Regression for issue #3: a new top-level key (e.g. `meta: foo`) after
        # the tasks list must reset the parser's active-task context, so any
        # subsequent indented `key: value` lines don't bleed into the previously
        # open task. The task wrapper uses selective field assignment, so the
        # observable contract is: t1 parses cleanly, exactly 1 task, id == "t1".
        progress_md = """
### Tasks
```yaml
tasks:
  - id: t1
meta: foo
  orphan_key: bar
```
"""
        tasks = parse_plan_tasks(progress_md)
        self.assertEqual(len(tasks), 1)
        self.assertEqual(tasks[0].id, "t1")
        # writes was never declared → strict-serial sentinel preserved
        self.assertIsNone(tasks[0].writes)
        # Drop down to the parser internals to confirm the orphan key did not
        # bleed into the inner task dict (Task() uses selective field
        # assignment, so the bleed is not observable via the public API alone).
        yaml_text = """
tasks:
  - id: t1
meta: foo
  orphan_key: bar
"""
        raw = _parse_task_yaml(yaml_text)
        self.assertNotIn("orphan_key", raw["tasks"][0])

    def test_invalid_yaml_raises(self):
        progress_md = """
### Tasks
```yaml
tasks:
  - id: [unclosed
```
"""
        with self.assertRaises(PlanError):
            parse_plan_tasks(progress_md)


class TestSharedArtifacts(unittest.TestCase):
    def test_load_shared_artifacts(self):
        from flow_wave_planner import load_shared_artifacts
        globs = load_shared_artifacts()
        self.assertIn("VERSION", globs)
        self.assertIn("**/package.json", globs)
        # at least 5 entries
        self.assertGreater(len(globs), 5)

    def test_task_writing_to_shared_artifact_flagged(self):
        from flow_wave_planner import wave_touches_shared
        task = Task(id="t1", writes=["VERSION"])
        self.assertTrue(wave_touches_shared([task]))

    def test_task_with_normal_writes_not_flagged(self):
        from flow_wave_planner import wave_touches_shared
        task = Task(id="t1", writes=["src/foo.py"])
        self.assertFalse(wave_touches_shared([task]))

    def test_nested_lockfile_flagged(self):
        from flow_wave_planner import wave_touches_shared
        task = Task(id="t1", writes=["packages/foo/package-lock.json"])
        # **/package-lock.json should match nested
        self.assertTrue(wave_touches_shared([task]))


if __name__ == "__main__":
    unittest.main()
