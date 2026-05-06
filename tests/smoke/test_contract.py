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


if __name__ == "__main__":
    unittest.main()
