import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _run_flow(args, cwd):
    return subprocess.run(
        ["python3", str(REPO_ROOT / "scripts" / "flow.py"), *args],
        cwd=str(cwd), capture_output=True, text=True, env=os.environ.copy(),
    )


def _setup_task(tmp: Path, slug: str, contract: dict, tasks_yaml: str):
    task_dir = tmp / ".flow" / "tasks" / slug
    task_dir.mkdir(parents=True)
    (task_dir / "contract.json").write_text(json.dumps(contract))
    (task_dir / "progress.md").write_text(
        "---\n"
        f"contract_path: contract.json\n"
        f"contract_schema_version: 1\n"
        f"autonomy_mode: {contract['autonomy_mode']}\n"
        "---\n\n"
        "# progress.md\n\n"
        "## Plan\n\nDemo plan.\n\n"
        "### Tasks\n\n```yaml\n"
        f"{tasks_yaml}"
        "```\n"
    )
    return task_dir


class TestOrchestratorDryRun(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def test_dryrun_prints_task_plan_for_interactive_mode(self):
        _setup_task(self.tmp, "demo", {
            "contract_schema_version": 1,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
            "scope": {"allowed": ["src/**"]},
        }, "tasks:\n  - id: T1\n    writes: ['src/a.py']\n  - id: T2\n    writes: ['src/b.py']\n")
        result = _run_flow(["orchestrator", "--dry-run", "demo"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("T1", result.stdout)
        self.assertIn("T2", result.stdout)
        self.assertIn("manifest", result.stdout.lower())

    def test_auto_dispatch_pre_lock_crash_falls_back_interactive(self):
        """T19 Step 19.11 replaced the v0.8.0 exit-2 stub with the
        end-to-end dispatch loop. The historical assertion
        (`returncode == 2` + "v0.8.0 disabled" message) is obsolete
        per the plan exit-code table (line 7180-7186): exit 2 is no
        longer reachable on the auto-execute path.

        Fixture state (autonomy_mode=auto in progress.md, no lock,
        no auto_engaged event for this run/task) is exactly state 1
        in T19's CrashRecoveryDispatcher: the user never opted in
        to THIS attempt (no boundary marker was ever written), so
        the dispatcher returns `fail_closed_interactive` and the
        orchestrator exits 0 with a stderr WARN. This is the LEGAL
        silent fallback per §7 line 312 (state 1 is the only state
        permitted to silently degrade to interactive).
        """
        _setup_task(self.tmp, "demo", {
            "contract_schema_version": 1,
            "autonomy_mode": "auto",
            "created_at": "2026-05-05T00:00:00Z",
            "scope": {"allowed": ["src/**"]},
            "acceptance_criteria": [
                {"description": "u", "type": "unit", "command": "true"},
            ],
        }, "tasks:\n  - id: T1\n    writes: ['src/a.py']\n")
        result = _run_flow(
            ["orchestrator", "--auto-execute", "demo"], cwd=self.tmp,
        )
        # State 1 returns exit 0 (legal silent fallback to
        # interactive — user never opted in).
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        # The historical v0.8.0 stub message is GONE.
        msg = result.stdout + result.stderr
        self.assertNotIn(
            "ERROR: v0.8.0 does not support autonomous dispatch", msg,
        )
        # The new fallback path emits a stderr WARN for operator
        # visibility (different from silent — operators can grep).
        self.assertIn("interactive fallback", result.stderr)

    def test_dryrun_missing_contract_falls_back_to_interactive(self):
        slug = "demo"
        d = self.tmp / ".flow" / "tasks" / slug
        d.mkdir(parents=True)
        (d / "progress.md").write_text("# progress.md\n\n## Plan\n\nx\n")
        result = _run_flow(["orchestrator", "--dry-run", slug], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("interactive", result.stdout.lower())
        self.assertNotIn("ERROR", result.stdout)

    def test_dryrun_manifest_intersects_scope_and_writes(self):
        _setup_task(self.tmp, "demo", {
            "contract_schema_version": 1,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
            "scope": {"allowed": ["src/**", "tests/**"], "forbidden": [".env"]},
        }, "tasks:\n  - id: T1\n    writes: ['src/a.py', 'docs/x.md']\n")
        result = _run_flow(["orchestrator", "--dry-run", "demo"], cwd=self.tmp)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("out_of_scope", result.stdout)
        self.assertIn("docs/x.md", result.stdout)


if __name__ == "__main__":
    unittest.main()
