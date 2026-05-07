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


class TestDoctorEmptySnapshotNoFalsePositive(unittest.TestCase):
    """[Codex round-1 P2] In v0.8.1 doctor-only mode the snapshot is
    always empty (orchestrator wire-up deferred to v0.8.2). With the
    pre-fix semantics, every active task with a present prd.md /
    package.json / lockfile would be reported STALE because empty
    snapshot meant "everything added". Post-fix: triggers 2/3/4 skip
    explicitly with a `skipped` detail; trigger 1 (base_branch) +
    trigger 5 (baseline_fail) still run because they don't depend on
    snapshot.
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

    def test_active_task_with_prd_lockfile_dep_does_not_false_positive(
        self,
    ) -> None:
        """The pre-fix repro: a task with prd.md + package.json +
        package-lock.json on disk reported STALE every doctor run.
        Post-fix: task is "clean" (or at least: not flagged as stale
        by triggers 2/3/4)."""
        # Create the realistic active-task surface that triggers
        # 2/3/4 would all see as "newly added" with empty snapshot.
        self.contract_path.write_text(json.dumps({
            "integration_target": "master",
            "baseline_command": "",
        }))
        (self.task_dir / "prd.md").write_text("# spec\n")
        (self.tmp / "package.json").write_text(
            '{"name": "x", "dependencies": {"foo": "^1.0.0"}}'
        )
        (self.tmp / "package-lock.json").write_text('{"v": 1}')

        result = self._run_doctor()
        out = result.stdout + result.stderr
        # Doctor reaches summary (no crash).
        self.assertIn("Contract integrity", out)
        # The active task must NOT be flagged STALE based on
        # triggers 2/3/4 alone.
        self.assertNotIn("demo: STALE", out)
        # Confirm staleness section actually ran for this slug.
        self.assertIn("demo", out)


class TestDoctorRClassEscape(unittest.TestCase):
    """[Codex round-1 P2 R-class] Disk-derived strings (slug,
    worktree dir name, integration_target) printed in the staleness
    section must be ANSI/control-char stripped before terminal output.
    A malicious slug or worktree dir name with `\\x1b[31m` would
    otherwise recolor the doctor output mid-stream.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _run_doctor(self) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "flow_doctor.py")],
            cwd=str(self.tmp), capture_output=True, text=True,
        )

    @staticmethod
    def _staleness_section(out: str) -> str:
        """Extract just the '>> Staleness ...' section so we can assert
        about the user-injected fields without colliding with doctor's
        own ANSI colors used elsewhere in the output."""
        start = out.find(">> Staleness")
        if start < 0:
            return ""
        nxt = out.find("\n>> ", start + 1)
        return out[start: nxt if nxt > 0 else len(out)]

    @staticmethod
    def _strip_doctor_ansi(text: str) -> str:
        """Remove the well-known ANSI codes the doctor itself emits
        (GREEN/YELLOW/RED/DIM/RESET — see flow_doctor.py top-level
        constants). Anything left is either user-injected or a bug
        in the sanitizer."""
        import re as _re
        # Match `\x1b[<digits>m` — the SGR sequences flow_doctor uses.
        return _re.sub(r"\x1b\[[0-9;]*m", "", text)

    def test_doctor_strips_ansi_in_slug(self) -> None:
        """A slug containing `\\x1b[31m` must NOT appear unescaped in
        the staleness section. We craft a slug with a control byte,
        place a contract.json (intentionally corrupt to force the
        `top-level is not a dict` warn print path), and verify the
        raw escape sequence does not leak through that section."""
        # Slug with embedded ESC-CSI sequence + BEL.
        slug = "demo\x1b[31mEVIL\x07"
        slug_dir = self.tmp / ".flow" / "tasks" / slug
        slug_dir.mkdir(parents=True)
        # init git repo at tmp so doctor finds it.
        subprocess.run(
            ["git", "init", "-q", "-b", "master", str(self.tmp)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.email", "x@y"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.name", "x"],
            check=True,
        )
        (self.tmp / "VERSION").write_text("0.0.0\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
            check=True,
        )
        # Corrupt contract → forces the slug into a warn() print path.
        (slug_dir / "contract.json").write_text("[1, 2, 3]")
        # Matching worktree dir so check_staleness reaches the contract.
        wt = self.tmp / ".claude" / "worktrees" / f"{slug}+t1+abcdef0"
        wt.mkdir(parents=True)

        result = self._run_doctor()
        out = result.stdout + result.stderr
        # Strip doctor's own legitimate SGR codes; what remains in the
        # staleness section must NOT contain user-injected control
        # bytes from the slug.
        section = self._staleness_section(out)
        cleaned = self._strip_doctor_ansi(section)
        self.assertNotIn("\x1b", cleaned)
        self.assertNotIn("\x07", cleaned)
        # But the printable portion of the slug must still appear so
        # the operator can identify the offender.
        self.assertIn("EVIL", cleaned)

    def test_doctor_strips_control_in_worktree_name(self) -> None:
        """A worktree directory name with embedded BEL/ESC must NOT
        leak into the `worktree: ...` detail line of the staleness
        section. We assert against the staleness-scoped section so
        doctor's own legitimate ANSI color codes elsewhere don't
        spuriously match."""
        # Setup tmp git repo + valid contract for slug "demo".
        slug = "demo"
        subprocess.run(
            ["git", "init", "-q", "-b", "master", str(self.tmp)], check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.email", "x@y"],
            check=True,
        )
        subprocess.run(
            ["git", "-C", str(self.tmp), "config", "user.name", "x"],
            check=True,
        )
        (self.tmp / "VERSION").write_text("0.0.0\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True)
        subprocess.run(
            ["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
            check=True,
        )
        slug_dir = self.tmp / ".flow" / "tasks" / slug
        slug_dir.mkdir(parents=True)
        (slug_dir / "contract.json").write_text(json.dumps({
            "integration_target": "master",
            "baseline_command": "",
        }))
        # Worktree dir with embedded ESC + BEL in the name.
        wt_name = f"{slug}+t1+abc\x1b[32mEVIL\x07def"
        wt = self.tmp / ".claude" / "worktrees" / wt_name
        wt.mkdir(parents=True)

        result = self._run_doctor()
        out = result.stdout + result.stderr
        section = self._staleness_section(out)
        # Strip the doctor's own SGR codes first, then assert no raw
        # control bytes remain. Anything left would be user-injected.
        cleaned = self._strip_doctor_ansi(section)
        # Locate the worktree detail line in the cleaned section.
        wt_detail_lines = [
            ln for ln in cleaned.splitlines() if "worktree:" in ln
        ]
        self.assertTrue(wt_detail_lines, "worktree detail line missing")
        joined = "\n".join(wt_detail_lines)
        # No bare ESC, no BEL, no other control bytes from the
        # user-controlled wt name should pass through.
        self.assertNotIn("\x1b", joined)
        self.assertNotIn("\x07", joined)
        # But the printable portion is preserved.
        self.assertIn("EVIL", joined)


if __name__ == "__main__":
    unittest.main()
