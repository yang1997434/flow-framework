"""Smoke tests for scripts/common/exit_codes.py.

Pinned in v0.8.2.1 as part of the rc=2 -> rc=5 AFK park migration.
This module is the single source of truth for Flow exit codes; tests
guard the constant values, the canonical import style, and the
zero-side-effect property.
"""
from __future__ import annotations

import importlib
import sys
import unittest
from pathlib import Path

# Match the orchestrator's sys.path insertion order so 'common.exit_codes'
# resolves the same way in tests as in production.
_REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(_REPO_ROOT / "scripts"))


class TestExitCodeConstantsValues(unittest.TestCase):
    def test_six_constants_exact_values(self):
        from common.exit_codes import (  # noqa: E402  type: ignore
            PASS,
            GENERIC_FAIL,
            USAGE_ERROR,
            BLOCKED,
            NESTED_ABORT,
            PARKED_RECOVERABLE,
        )
        self.assertEqual(PASS, 0)
        self.assertEqual(GENERIC_FAIL, 1)
        self.assertEqual(USAGE_ERROR, 2)
        self.assertEqual(BLOCKED, 3)
        self.assertEqual(NESTED_ABORT, 4)
        self.assertEqual(PARKED_RECOVERABLE, 5)


class TestExitCodeImportStyle(unittest.TestCase):
    def test_canonical_import_works(self):
        # canonical: from common.exit_codes import ...
        from common.exit_codes import PARKED_RECOVERABLE  # noqa: E402  type: ignore
        self.assertEqual(PARKED_RECOVERABLE, 5)


class TestExitCodeNoSideEffect(unittest.TestCase):
    def test_module_reload_is_idempotent(self):
        # Module-form import (`import common.exit_codes as ec`) is
        # required to obtain a module reference for `importlib.reload`.
        # The canonical-prefix form here satisfies the import-style
        # AC, which forbids bare unqualified imports lacking the
        # `common.` prefix.
        import common.exit_codes as ec  # noqa: E402  type: ignore
        before_value = ec.PARKED_RECOVERABLE
        # Reload should not raise (no side effects beyond constant
        # rebinding) and the value must be preserved.
        importlib.reload(ec)
        self.assertEqual(ec.PARKED_RECOVERABLE, before_value)


if __name__ == "__main__":
    unittest.main()
