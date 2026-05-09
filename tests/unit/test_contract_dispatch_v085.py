"""v0.8.5 — Contract.dispatch field unit tests.

Covers PRD §R5:
- Default both switches "on" when ``dispatch`` block absent
- Explicit ``"off"`` accepted for either switch independently
- Boolean shorthand (True/False) accepted
- Unknown value raises ContractError
- ``telemetry`` and ``feedback_enrichment`` independent (one off
  ≠ both off)
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_contract import parse_contract, ContractError  # noqa: E402  type: ignore


def _write_contract(td: Path, extra: dict) -> Path:
    base = {
        "contract_schema_version": 1,
        "autonomy_mode": "auto",
        "created_at": "2026-05-08T00:00:00Z",
    }
    base.update(extra)
    p = td / "contract.json"
    p.write_text(json.dumps(base), encoding="utf-8")
    return p


class DefaultsBothOn(unittest.TestCase):
    def test_dispatch_block_absent_defaults_both_on(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(Path(td), {})
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "on")
            self.assertEqual(c.dispatch["feedback_enrichment"], "on")

    def test_empty_dispatch_block_defaults_both_on(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(Path(td), {"dispatch": {}})
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "on")
            self.assertEqual(c.dispatch["feedback_enrichment"], "on")


class IndependentSwitches(unittest.TestCase):
    def test_telemetry_off_feedback_on(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"telemetry": "off"}},
            )
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "off")
            # Feedback default unchanged.
            self.assertEqual(c.dispatch["feedback_enrichment"], "on")

    def test_feedback_off_telemetry_on(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"feedback_enrichment": "off"}},
            )
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "on")
            self.assertEqual(c.dispatch["feedback_enrichment"], "off")


class BooleanShorthand(unittest.TestCase):
    def test_true_means_on(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"telemetry": True}},
            )
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "on")

    def test_false_means_off(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"telemetry": False}},
            )
            c = parse_contract(p)
            self.assertEqual(c.dispatch["telemetry"], "off")


class FailClosedOnInvalid(unittest.TestCase):
    def test_unknown_value_raises(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"telemetry": "yes"}},
            )
            with self.assertRaises(ContractError):
                parse_contract(p)

    def test_int_value_raises(self) -> None:
        with TemporaryDirectory() as td:
            p = _write_contract(
                Path(td),
                {"dispatch": {"telemetry": 1}},
            )
            with self.assertRaises(ContractError):
                parse_contract(p)


if __name__ == "__main__":
    unittest.main()
