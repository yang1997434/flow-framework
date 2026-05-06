import json
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
from flow_contract import parse_contract, CONTRACT_SCHEMA_VERSION, ContractError


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


if __name__ == "__main__":
    unittest.main()
