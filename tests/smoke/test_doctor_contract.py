import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class TestDoctorContractIntegrity(unittest.TestCase):
    """Doctor contract checks must walk up from cwd, NOT from the framework's
    install root. Tests build an isolated tmpdir with a `.flow/` and run
    doctor with cwd=tmp so the framework's own .flow/tasks/ never leaks in."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        # Mark the tmp as a Flow project root.
        (self.tmp / ".flow").mkdir()

    def test_doctor_passes_with_empty_tasks_dir(self):
        (self.tmp / ".flow" / "tasks").mkdir()
        result = subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "flow_doctor.py")],
            cwd=str(self.tmp), capture_output=True, text=True,
        )
        self.assertIn("Contract integrity", result.stdout)
        self.assertIn("OK", result.stdout)

    def test_doctor_flags_missing_contract_when_auto_mode(self):
        slug = self.tmp / ".flow" / "tasks" / "demo"
        slug.mkdir(parents=True)
        (slug / "progress.md").write_text(
            "---\nautonomy_mode: auto\n---\n# x\n"
        )
        result = subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "flow_doctor.py")],
            cwd=str(self.tmp), capture_output=True, text=True,
        )
        out = result.stdout + result.stderr
        self.assertIn("demo", out)
        self.assertIn("contract.json missing", out)


if __name__ == "__main__":
    unittest.main()
