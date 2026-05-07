import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _init_repo_with_worktree(tmp: Path, slug: str) -> Path:
    """Bootstrap a Flow project root + matching worktree dir so
    `check_staleness` reaches the contract.json read path. Returns the
    task directory (`<tmp>/.flow/tasks/<slug>`).

    Without a worktree under `.claude/worktrees/<slug>+t*+<sha>`, the
    `if not candidates: continue` branch on flow_doctor.py:549 short-
    circuits before the contract read, masking the [85] regression.
    """
    subprocess.run(["git", "init", "-q", "-b", "master", str(tmp)], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.email", "x@y"], check=True,
    )
    subprocess.run(
        ["git", "-C", str(tmp), "config", "user.name", "x"], check=True,
    )
    (tmp / "VERSION").write_text("0.0.0\n")
    subprocess.run(["git", "-C", str(tmp), "add", "."], check=True)
    subprocess.run(
        ["git", "-C", str(tmp), "commit", "-q", "-m", "init"], check=True,
    )
    task_dir = tmp / ".flow" / "tasks" / slug
    task_dir.mkdir(parents=True)
    # Fake worktree dir matching `<slug>+t<n>+<shortsha>` pattern so
    # check_staleness doesn't skip via the `not candidates` branch.
    wt = tmp / ".claude" / "worktrees" / f"{slug}+t1+abcdef0"
    wt.mkdir(parents=True)
    return task_dir


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


class TestDoctorStalenessContractGuards(unittest.TestCase):
    """[85] code-review fix — `check_staleness` must guard every
    `contract.json` read with `isinstance(dict)` before chained `.get()`.
    Pre-fix repro: a corrupt or legacy contract crashed the entire
    doctor run mid-stream (AttributeError before `check_contract_integrity`
    + final summary). Post-fix: warn + skip + continue → doctor reaches
    the final summary.

    Each test runs the doctor as a subprocess against an isolated tmp
    project so a crash mid-run would surface as a non-zero exit + no
    "Contract integrity" section header. We assert both the absence of
    AttributeError AND that the doctor proceeded past `check_staleness`
    to later sections (proving we didn't merely swallow the exception).
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.task_dir = _init_repo_with_worktree(self.tmp, "demo")
        self.contract_path = self.task_dir / "contract.json"

    def _run_doctor(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "flow_doctor.py")],
            cwd=str(self.tmp), capture_output=True, text=True,
        )

    def _assert_doctor_did_not_crash(
        self, result: subprocess.CompletedProcess
    ) -> None:
        out = result.stdout + result.stderr
        # The fatal pre-fix symptom is AttributeError on `.get()` chain.
        self.assertNotIn("AttributeError", out)
        self.assertNotIn("Traceback", out)
        # Doctor must reach `check_contract_integrity()` after staleness.
        # If staleness crashed, this section header would be missing.
        self.assertIn("Contract integrity", out)

    def test_check_staleness_handles_non_dict_contract_json(self) -> None:
        """[85] Top-level non-dict (array literal). Pre-fix: crash on
        `contract_data.get("integration_target")` because list has no
        `.get()`. Post-fix: warn + skip."""
        self.contract_path.write_text("[1, 2, 3]")  # valid JSON, not dict
        result = self._run_doctor()
        self._assert_doctor_did_not_crash(result)
        out = result.stdout + result.stderr
        self.assertIn("demo", out)
        self.assertIn("top-level is not a dict", out)

    def test_check_staleness_handles_non_dict_baseline_field(self) -> None:
        """[85] `"baseline"` field as string. Pre-fix: crash on
        `contract_data.get("baseline", {}).get("command")` because the
        outer `.get("baseline")` returns the string, not the {} default,
        and strings have no `.get()`. Post-fix: legacy fallback skipped
        cleanly because `isinstance(legacy, dict)` is False."""
        self.contract_path.write_text(json.dumps({
            "integration_target": "master",
            "baseline": "some_string",  # non-dict — would have crashed
        }))
        result = self._run_doctor()
        self._assert_doctor_did_not_crash(result)
        # Doctor proceeds; staleness section ran for `demo` without crash.
        out = result.stdout + result.stderr
        self.assertIn("demo", out)

    def test_check_staleness_handles_corrupt_contract_json(self) -> None:
        """JSONDecodeError → warn + skip, not crash. Pre-existing
        `(OSError, json.JSONDecodeError)` typed except already handled
        this; test pins the behaviour against future regression."""
        self.contract_path.write_text("not valid json {")
        result = self._run_doctor()
        self._assert_doctor_did_not_crash(result)
        out = result.stdout + result.stderr
        self.assertIn("contract.json unreadable", out)

    def test_check_staleness_handles_legacy_baseline_dict_format(self) -> None:
        """Back-compat: old contract format with `baseline.command` (dict
        form) must still resolve `baseline_command`. Verifies the
        `if isinstance(legacy, dict): legacy.get("command")` fallback
        path. Doctor does not crash; demo slug is reported."""
        self.contract_path.write_text(json.dumps({
            "integration_target": "master",
            "baseline": {"command": "bash tests/smoke/run.sh"},
        }))
        result = self._run_doctor()
        self._assert_doctor_did_not_crash(result)
        out = result.stdout + result.stderr
        self.assertIn("demo", out)


if __name__ == "__main__":
    unittest.main()
