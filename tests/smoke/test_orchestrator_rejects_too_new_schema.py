"""T2 (v0.8.1): orchestrator must hard-reject contracts whose
contract_schema_version exceeds the runtime's known ceiling.

Codex round-3 R11: v0.8.0 `flow_orchestrator.py` calls `parse_contract()`
without checking the schema-version ceiling. A contract with
`contract_schema_version: 999` would be parsed successfully (parser only
floors at >=1) and dispatched against, even though the runtime can't
understand the new fields. v0.8.1 must fail-closed in BOTH the
`--auto-execute` and `--dry-run` paths so we never silently parse a
future-version contract whose semantics we don't know.

This is the smoke companion to the validator-level pin in
`test_contract.py::test_validate_too_new_schema_hard_error`.
"""
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
FLOW = REPO_ROOT / "scripts" / "flow.py"


class TestOrchestratorRejectsTooNewSchema(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        slug_dir = Path(self.tmp) / ".flow" / "tasks" / "demo"
        slug_dir.mkdir(parents=True)
        (slug_dir / "progress.md").write_text(
            "---\nautonomy_mode: auto\ncontract_path: contract.json\n---\n\n"
            "## Tasks\n\n- [ ] T1 do thing — files: scripts/foo.py\n"
        )
        (slug_dir / "contract.json").write_text(json.dumps({
            "contract_schema_version": 999,  # too new — runtime knows up to 1
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
        }))
        self.cwd = self.tmp

    def test_auto_execute_rejects_too_new_schema(self):
        r = subprocess.run(
            [sys.executable, str(FLOW), "orchestrator", "--auto-execute", "demo"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        # Hard-reject, exit non-zero, message names the version mismatch.
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("contract_schema_version", r.stderr.lower())
        self.assertIn("999", r.stderr)

    def test_dry_run_also_rejects_too_new(self):
        # Dry-run must also fail-closed (no silent fallback to interactive
        # — caller would think the v=999 contract was understood).
        r = subprocess.run(
            [sys.executable, str(FLOW), "orchestrator", "--dry-run", "demo"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("contract_schema_version", r.stderr.lower())
        self.assertIn("999", r.stderr)


if __name__ == "__main__":
    unittest.main()
