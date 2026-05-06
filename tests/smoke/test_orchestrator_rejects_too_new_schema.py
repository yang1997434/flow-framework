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


# ----------------------------------------------------------------------
# Codex P1 bypass coverage (May 2026): the original post-parse_contract
# ceiling check was reachable ONLY when parse_contract succeeded. A v=999
# contract that ALSO carried future-incompatible field values
# (autonomy_mode/method/type unknown to v0.8.1) would raise ContractError,
# get caught by build_plan's except branch, set fallback_reason, and
# silently degrade to interactive — bypassing R11. Fix: pre-parse raw-JSON
# ceiling check in build_plan BEFORE parse_contract is called.
# ----------------------------------------------------------------------


class _BypassBase(unittest.TestCase):
    """Shared scaffolding for the bypass cases: writes a contract.json with
    a too-new schema_version PLUS a future-incompatible field, asserts the
    orchestrator still fails closed (does NOT degrade to interactive).
    """

    contract_payload: dict = {}

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        slug_dir = Path(self.tmp) / ".flow" / "tasks" / "demo"
        slug_dir.mkdir(parents=True)
        (slug_dir / "progress.md").write_text(
            "---\nautonomy_mode: auto\ncontract_path: contract.json\n---\n\n"
            "## Tasks\n\n- [ ] T1 do thing — files: scripts/foo.py\n"
        )
        (slug_dir / "contract.json").write_text(json.dumps(self.contract_payload))
        self.cwd = self.tmp

    def _run(self, mode: str):
        return subprocess.run(
            [sys.executable, str(FLOW), "orchestrator", f"--{mode}", "demo"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )

    def _assert_hard_reject(self, r):
        self.assertNotEqual(
            r.returncode, 0,
            f"expected hard reject; got rc=0\nstdout:\n{r.stdout}",
        )
        self.assertIn("contract_schema_version", r.stderr.lower())
        self.assertIn("999", r.stderr)
        # MUST NOT silently degrade — fallback_reason indicates interactive
        # degrade path was taken (which would mean we accepted a v999
        # contract by treating it as "no contract").
        self.assertNotIn("falling back to interactive", r.stdout.lower())
        self.assertNotIn("falling back to interactive", r.stderr.lower())


class TestBypassFutureAutonomyMode(_BypassBase):
    """v=999 + a future autonomy_mode value (e.g. `supervised` — invented
    in some hypothetical v2). v0.8.1's VALID_AUTONOMY_MODES is
    ('auto', 'interactive'); parse_contract would raise ContractError on
    this BEFORE reaching the post-parse ceiling check.
    """

    contract_payload = {
        "contract_schema_version": 999,
        "autonomy_mode": "supervised",  # not in VALID_AUTONOMY_MODES
        "created_at": "2026-05-06T00:00:00Z",
    }

    def test_auto_execute(self):
        self._assert_hard_reject(self._run("auto-execute"))

    def test_dry_run(self):
        self._assert_hard_reject(self._run("dry-run"))


class TestBypassFutureCriterionMethod(_BypassBase):
    """v=999 + a criterion with a future `method` (e.g. `smtp`).
    v0.8.1's VALID_CRITERION_METHODS is ('cmd', 'file_exists',
    'json_query', 'http'); parse_contract raises before the ceiling check.
    """

    contract_payload = {
        "contract_schema_version": 999,
        "autonomy_mode": "auto",
        "created_at": "2026-05-06T00:00:00Z",
        "acceptance_criteria": [
            {
                "description": "future method",
                "type": "smoke",
                "method": "smtp",  # not in VALID_CRITERION_METHODS
            },
        ],
    }

    def test_auto_execute(self):
        self._assert_hard_reject(self._run("auto-execute"))

    def test_dry_run(self):
        self._assert_hard_reject(self._run("dry-run"))


class TestBypassFutureCriterionType(_BypassBase):
    """v=999 + a criterion with a future `type` (e.g. `fuzz`).
    v0.8.1's VALID_CRITERION_TYPES does NOT include `fuzz`; parse_contract
    raises before the ceiling check.
    """

    contract_payload = {
        "contract_schema_version": 999,
        "autonomy_mode": "auto",
        "created_at": "2026-05-06T00:00:00Z",
        "acceptance_criteria": [
            {
                "description": "future type",
                "type": "fuzz",  # not in VALID_CRITERION_TYPES
                "method": "cmd",
                "command": "echo hi",
            },
        ],
    }

    def test_auto_execute(self):
        self._assert_hard_reject(self._run("auto-execute"))

    def test_dry_run(self):
        self._assert_hard_reject(self._run("dry-run"))


# ----------------------------------------------------------------------
# Positive controls: existing behaviors must NOT regress with the new
# pre-parse path in place.
# ----------------------------------------------------------------------


class TestControlV1WithBadAutonomyDegradesInteractive(unittest.TestCase):
    """v=1 + invalid autonomy_mode → still degrades to interactive
    (existing behavior). The pre-parse ceiling check must NOT trip on
    a current-version contract; only parse_contract's normal validation
    runs, ContractError is caught, fallback_reason is set.
    """

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
            "contract_schema_version": 1,        # current
            "autonomy_mode": "supervised",       # invalid for v1
            "created_at": "2026-05-06T00:00:00Z",
        }))
        self.cwd = self.tmp

    def test_dry_run_degrades(self):
        r = subprocess.run(
            [sys.executable, str(FLOW), "orchestrator", "--dry-run", "demo"],
            cwd=self.cwd,
            capture_output=True,
            text=True,
        )
        # Dry-run prints the plan including fallback_reason; degrades cleanly
        # — no SystemExit. (Existing v0.8.1 behavior.)
        self.assertEqual(
            r.returncode, 0,
            f"expected clean degrade for v=1 + bad mode; rc={r.returncode}\n"
            f"stdout:\n{r.stdout}\nstderr:\n{r.stderr}",
        )
        self.assertIn("interactive", r.stdout.lower())
        self.assertIn("contract parse failed", r.stdout.lower())


class TestControlV999WithAllValidFieldsStillRejects(unittest.TestCase):
    """v=999 + every field a current-version parser already accepts →
    still rejected. This is the original test scenario from setUp above
    (kept here separately as an explicit regression pin against the new
    pre-parse code path inadvertently letting it through).
    """

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
            "contract_schema_version": 999,
            "autonomy_mode": "auto",              # known
            "created_at": "2026-05-06T00:00:00Z",
            "acceptance_criteria": [               # all valid v0.8.1 fields
                {
                    "description": "smoke ok",
                    "type": "smoke",
                    "method": "cmd",
                    "command": "echo ok",
                },
            ],
        }))
        self.cwd = self.tmp

    def test_dry_run_rejects(self):
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
