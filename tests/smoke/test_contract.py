import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_contract import parse_contract, validate_contract, CONTRACT_SCHEMA_VERSION, ContractError


class TestParseContract(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _write(self, payload):
        p = Path(self.tmp) / "contract.json"
        p.write_text(json.dumps(payload))
        return p

    def _base(self):
        return {
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
        }

    def test_minimal_valid_contract_parses(self):
        path = self._write(self._base())
        c = parse_contract(path)
        self.assertEqual(c.autonomy_mode, "interactive")
        self.assertEqual(c.contract_schema_version, CONTRACT_SCHEMA_VERSION)

    def test_scope_false_raises_contract_error(self):
        """scope: false is a falsy non-dict — must raise ContractError (fail-closed)."""
        payload = {**self._base(), "scope": False}
        path = self._write(payload)
        with self.assertRaises(ContractError) as ctx:
            parse_contract(path)
        self.assertIn("scope must be an object", str(ctx.exception))

    def test_scope_empty_string_raises_contract_error(self):
        """scope: '' is a falsy non-dict — must raise ContractError (fail-closed)."""
        payload = {**self._base(), "scope": ""}
        path = self._write(payload)
        with self.assertRaises(ContractError) as ctx:
            parse_contract(path)
        self.assertIn("scope must be an object", str(ctx.exception))

    def test_scope_zero_raises_contract_error(self):
        """scope: 0 is a falsy non-dict — must raise ContractError (fail-closed)."""
        payload = {**self._base(), "scope": 0}
        path = self._write(payload)
        with self.assertRaises(ContractError) as ctx:
            parse_contract(path)
        self.assertIn("scope must be an object", str(ctx.exception))

    def test_notification_false_raises_contract_error(self):
        """notification: false is a falsy non-dict — must raise ContractError (fail-closed)."""
        payload = {**self._base(), "notification": False}
        path = self._write(payload)
        with self.assertRaises(ContractError) as ctx:
            parse_contract(path)
        self.assertIn("notification must be an object", str(ctx.exception))

    def test_acceptance_criteria_not_list_raises_contract_error(self):
        """acceptance_criteria: false is a falsy non-list — must raise ContractError."""
        payload = {**self._base(), "acceptance_criteria": False}
        path = self._write(payload)
        with self.assertRaises(ContractError) as ctx:
            parse_contract(path)
        self.assertIn("acceptance_criteria must be an array", str(ctx.exception))

    # ------------------------------------------------------------------
    # T1 v0.8.1: schema additive fields + per-method timeout defaults +
    # idempotent override + post_merge_skip cross-field rule.
    # ------------------------------------------------------------------

    def test_max_codex_rounds_per_task_default(self):
        """Q2.2: missing field → default 3"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
        })
        c = parse_contract(path)
        self.assertEqual(c.budget["max_codex_rounds_per_task"], 3)

    def test_max_codex_rounds_per_task_explicit(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
            "budget": {"max_codex_rounds_per_task": 5},
        })
        c = parse_contract(path)
        self.assertEqual(c.budget["max_codex_rounds_per_task"], 5)

    def test_notification_throttle_min_default(self):
        """R9: missing → 5"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
        })
        c = parse_contract(path)
        self.assertEqual(c.notification["throttle_min"], 5)
        self.assertTrue(c.notification["tier2_enabled"])

    def test_notification_throttle_zero_means_no_throttle_not_disabled(self):
        """R9: 0 = fire every event; tier2_enabled separate switch"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
            "notification": {"throttle_min": 0},
        })
        c = parse_contract(path)
        self.assertEqual(c.notification["throttle_min"], 0)
        self.assertTrue(c.notification["tier2_enabled"])

    def test_notification_tier2_disabled(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
            "notification": {"tier2_enabled": False},
        })
        c = parse_contract(path)
        self.assertFalse(c.notification["tier2_enabled"])

    def test_idempotent_cmd_allowlist_default(self):
        """R8: missing → built-in allowlist"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-06T00:00:00Z",
        })
        c = parse_contract(path)
        self.assertIn("pytest", c.idempotent_cmd_allowlist)
        self.assertIn("mypy", c.idempotent_cmd_allowlist)
        self.assertIn("flow doctor", c.idempotent_cmd_allowlist)

    def test_acceptance_criterion_with_idempotent_object(self):
        """R8: per-criterion override needs rationale + timeout_sec + side_effect_class"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
            "acceptance_criteria": [{
                "description": "smoke pings stub",
                "type": "smoke",
                "method": "cmd",
                "command": "curl http://localhost/health",
                "timeout_sec": 30,
                "idempotent": {
                    "value": True,
                    "rationale": "GET against stub; no state mutation",
                    "timeout_sec": 30,
                    "side_effect_class": "read_only",
                },
                "post_merge_skip": False,
            }],
        })
        c = parse_contract(path)
        crit = c.acceptance_criteria[0]
        self.assertEqual(crit.method, "cmd")
        self.assertEqual(crit.timeout_sec, 30)
        self.assertTrue(crit.idempotent["value"])
        self.assertEqual(crit.idempotent["side_effect_class"], "read_only")

    def test_post_merge_skip_illegal_for_regression(self):
        """S3: regression type cannot have post_merge_skip=true unless contract.post_merge_regression_optional=true"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
            "acceptance_criteria": [{
                "description": "main suite",
                "type": "regression",
                "method": "cmd",
                "command": "bash tests/smoke/run.sh",
                "post_merge_skip": True,
            }],
        })
        with self.assertRaises(ContractError):
            parse_contract(path)

    def test_post_merge_regression_optional_unlocks_skip(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
            "post_merge_regression_optional": True,
            "acceptance_criteria": [{
                "description": "main suite",
                "type": "regression",
                "method": "cmd",
                "command": "bash tests/smoke/run.sh",
                "post_merge_skip": True,
            }],
        })
        c = parse_contract(path)
        self.assertTrue(c.acceptance_criteria[0].post_merge_skip)

    def test_criterion_default_timeout_by_method(self):
        """R7: defaults — file_exists/json_query=30s, cmd=600s, http=60s, e2e=1800s"""
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-06T00:00:00Z",
            "acceptance_criteria": [
                {"description": "f", "type": "smoke", "method": "file_exists", "path": "VERSION"},
                {"description": "c", "type": "unit", "method": "cmd", "command": "true"},
                {"description": "h", "type": "integration", "method": "http", "url": "http://localhost/"},
                {"description": "e", "type": "e2e", "method": "cmd", "command": "playwright test"},
            ],
        })
        c = parse_contract(path)
        self.assertEqual(c.acceptance_criteria[0].timeout_sec, 30)   # file_exists
        self.assertEqual(c.acceptance_criteria[1].timeout_sec, 600)  # cmd
        self.assertEqual(c.acceptance_criteria[2].timeout_sec, 60)   # http
        self.assertEqual(c.acceptance_criteria[3].timeout_sec, 1800) # e2e


class TestContractInvalidCases(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _write(self, payload):
        p = Path(self.tmp) / "contract.json"
        p.write_text(json.dumps(payload))
        return p

    def _base(self, **over):
        d = {
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
        }
        d.update(over)
        return d

    def test_missing_required_field_fails_closed(self):
        path = self._write({"autonomy_mode": "interactive"})
        with self.assertRaises(ContractError) as cm:
            parse_contract(path)
        self.assertIn("contract_schema_version", str(cm.exception))

    def test_invalid_autonomy_mode_fails_closed(self):
        path = self._write(self._base(autonomy_mode="autoo"))
        with self.assertRaises(ContractError) as cm:
            parse_contract(path)
        self.assertIn("autonomy_mode", str(cm.exception))

    def test_unknown_field_accepted_with_warning_list(self):
        path = self._write(self._base(future_field_xyz=42))
        c = parse_contract(path)
        self.assertIn("future_field_xyz", c.unknown_fields)

    def test_invalid_acceptance_criterion_type_fails_closed(self):
        path = self._write(self._base(acceptance_criteria=[
            {"description": "x", "type": "telepathy", "command": "true"},
        ]))
        with self.assertRaises(ContractError):
            parse_contract(path)

    def test_invalid_afk_on_timeout_fails_closed(self):
        path = self._write(self._base(afk_on_timeout="maybe"))
        with self.assertRaises(ContractError):
            parse_contract(path)

    def test_not_a_json_object_fails_closed(self):
        p = Path(self.tmp) / "contract.json"
        p.write_text("[]")
        with self.assertRaises(ContractError):
            parse_contract(p)

    def test_invalid_json_fails_closed(self):
        p = Path(self.tmp) / "contract.json"
        p.write_text("{not json")
        with self.assertRaises(ContractError):
            parse_contract(p)

    def test_missing_file_fails_closed(self):
        with self.assertRaises(ContractError):
            parse_contract(Path(self.tmp) / "nope.json")


class TestValidateContract(unittest.TestCase):
    """validate_contract is the higher-level check used by the CLI:
    it parses + applies cross-field rules + version compatibility."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _write(self, payload):
        p = Path(self.tmp) / "contract.json"
        p.write_text(json.dumps(payload))
        return p

    def test_version_too_new_fails_closed(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION + 99,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
        })
        ok, errs = validate_contract(path)
        self.assertFalse(ok)
        self.assertTrue(any("schema_version" in e for e in errs))

    def test_auto_mode_without_acceptance_criteria_warns(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-05T00:00:00Z",
        })
        ok, errs = validate_contract(path)
        # v0.8.0: warn but don't fail (Phase 3 falls back to legacy gate when
        # acceptance_criteria empty). v0.8.1 will tighten this.
        self.assertTrue(ok)
        self.assertTrue(any(e.startswith("[warn]") for e in errs))

    def test_valid_full_contract(self):
        path = self._write({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-05T00:00:00Z",
            "staleness_ttl_days": 7,
            "scope": {"allowed": ["src/**"], "forbidden": [".env"]},
            "acceptance_criteria": [
                {"description": "unit", "type": "unit", "command": "pytest"},
            ],
        })
        ok, errs = validate_contract(path)
        self.assertTrue(ok, msg=f"errors: {errs}")


sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))
from progress_meta import read_progress_meta, ProgressMeta


class TestProgressMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))

    def _write(self, body):
        p = self.tmp / "progress.md"
        p.write_text(body)
        return p

    def test_no_frontmatter_returns_default_meta(self):
        path = self._write("# progress.md\n\n(no frontmatter)\n")
        m = read_progress_meta(path)
        self.assertEqual(m.autonomy_mode, "interactive")  # default
        self.assertIsNone(m.contract_path)

    def test_frontmatter_with_pointer_fields(self):
        path = self._write(
            "---\n"
            "contract_path: contract.json\n"
            "contract_schema_version: 1\n"
            "autonomy_mode: auto\n"
            "last_checkpoint: 2026-05-05T12:00:00Z\n"
            "---\n\n"
            "# progress.md\n"
        )
        m = read_progress_meta(path)
        self.assertEqual(m.contract_path, "contract.json")
        self.assertEqual(m.contract_schema_version, 1)
        self.assertEqual(m.autonomy_mode, "auto")

    def test_invalid_autonomy_mode_falls_back_to_interactive(self):
        path = self._write(
            "---\nautonomy_mode: maybe\n---\n# x\n"
        )
        m = read_progress_meta(path)
        self.assertEqual(m.autonomy_mode, "interactive")  # fail-closed

    def test_partial_frontmatter_other_fields_preserved(self):
        path = self._write(
            "---\nautonomy_mode: auto\nslug: foo\nphase: 2\n---\n# x\n"
        )
        m = read_progress_meta(path)
        self.assertEqual(m.autonomy_mode, "auto")
        self.assertIsNone(m.contract_path)  # not set


import os
import subprocess


class TestContractCLI(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp))
        self.slug = "test-cli-slug"
        self.task_dir = self.tmp / ".flow" / "tasks" / self.slug
        self.task_dir.mkdir(parents=True)

    def _run_flow(self, *args, cwd=None):
        cwd = cwd or self.tmp
        env = os.environ.copy()
        return subprocess.run(
            ["python3", str(REPO_ROOT / "scripts" / "flow.py"), *args],
            cwd=str(cwd), capture_output=True, text=True, env=env,
        )

    def _write_contract(self, payload):
        (self.task_dir / "contract.json").write_text(json.dumps(payload))

    def test_validate_passes_on_valid_contract(self):
        self._write_contract({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "interactive",
            "created_at": "2026-05-05T00:00:00Z",
        })
        result = self._run_flow("contract", "--validate", self.slug)
        self.assertEqual(result.returncode, 0, msg=result.stderr)
        self.assertIn("OK", result.stdout)

    def test_validate_fails_on_missing_field(self):
        self._write_contract({"autonomy_mode": "interactive"})
        result = self._run_flow("contract", "--validate", self.slug)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("contract_schema_version", result.stderr + result.stdout)

    def test_validate_missing_contract_file_fails_with_clear_error(self):
        result = self._run_flow("contract", "--validate", self.slug)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("not found", result.stderr + result.stdout)

    def test_validate_warns_on_auto_without_acceptance(self):
        self._write_contract({
            "contract_schema_version": CONTRACT_SCHEMA_VERSION,
            "autonomy_mode": "auto",
            "created_at": "2026-05-05T00:00:00Z",
        })
        result = self._run_flow("contract", "--validate", self.slug)
        self.assertEqual(result.returncode, 0)  # warning, not error
        self.assertIn("[warn]", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
