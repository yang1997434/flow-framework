"""Smoke tests for flow_wave_planner — Phase 1: parser."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_wave_planner import parse_plan_tasks, Task, PlanError  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
