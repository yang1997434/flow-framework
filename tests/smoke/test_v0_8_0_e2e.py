"""v0.8.0 e2e smoke: contract --init → --validate → orchestrator --dry-run."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _flow(args, cwd):
    return subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "flow.py"), *args],
        cwd=str(cwd), capture_output=True, text=True, env=os.environ.copy(),
    )


class TestV080EndToEnd(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.slug = "v080-demo"
        (self.tmp / ".flow" / "tasks" / self.slug).mkdir(parents=True)

    def test_full_lifecycle_init_validate_dry_run(self):
        # 1. init template
        r = _flow(["contract", "--init", self.slug], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        contract = self.tmp / ".flow" / "tasks" / self.slug / "contract.json"
        self.assertTrue(contract.is_file())

        # 2. validate template
        r = _flow(["contract", "--validate", self.slug], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("OK", r.stdout)

        # 3. write a minimal progress.md with tasks block + a real allowed glob
        progress = self.tmp / ".flow" / "tasks" / self.slug / "progress.md"
        progress.write_text(
            "---\n"
            "contract_path: contract.json\n"
            "contract_schema_version: 1\n"
            "autonomy_mode: interactive\n"
            "---\n\n"
            "# progress.md\n\n## Plan\n\nx\n\n"
            "### Tasks\n\n```yaml\n"
            "tasks:\n  - id: T1\n    writes: ['scoped/foo.py']\n"
            "```\n"
        )
        # Replace the template's placeholder allowed glob with a real one matching T1
        text = contract.read_text()
        text = text.replace('"<file glob>"', '"scoped/**"')
        contract.write_text(text)

        # 4. dry-run prints the plan
        r = _flow(["orchestrator", "--dry-run", self.slug], cwd=self.tmp)
        self.assertEqual(r.returncode, 0, msg=r.stderr)
        self.assertIn("T1", r.stdout)
        self.assertIn("autonomy_mode: interactive", r.stdout)

    def test_auto_mode_refused_with_clear_message(self):
        contract = self.tmp / ".flow" / "tasks" / self.slug / "contract.json"
        contract.write_text(json.dumps({
            "contract_schema_version": 1,
            "autonomy_mode": "auto",
            "created_at": "2026-05-05T00:00:00Z",
            "scope": {"allowed": ["src/**"]},
            "acceptance_criteria": [
                {"description": "u", "type": "unit", "command": "true"},
            ],
        }))
        r = _flow(["orchestrator", "--auto-execute", self.slug], cwd=self.tmp)
        self.assertEqual(r.returncode, 2, msg=r.stdout + r.stderr)
        self.assertIn("v0.8.0", r.stdout + r.stderr)
        self.assertIn("v0.8.1", r.stdout + r.stderr)


if __name__ == "__main__":
    unittest.main()
