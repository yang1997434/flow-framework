#!/usr/bin/env python3
"""Smoke tests for flow_doctor.check_capability_clis (v0.6.1, issue #10).

Verifies:
  - _is_dependency_available() handles skill-bundle dirs AND PATH binaries
  - check_capability_clis() doesn't crash on a clean run (no exceptions)
"""
from __future__ import annotations

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from flow_doctor import _is_dependency_available, check_capability_clis  # noqa: E402


class IsDependencyAvailable(unittest.TestCase):
    def test_returns_true_for_skill_bundle(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            (fake_home / ".claude" / "skills" / "fakebundle").mkdir(parents=True)
            with mock.patch.object(Path, "home", return_value=fake_home):
                self.assertTrue(_is_dependency_available("fakebundle"))

    def test_returns_true_for_path_binary(self):
        # `python3` is guaranteed on PATH in this test environment
        self.assertTrue(_is_dependency_available("python3"))

    def test_returns_false_for_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            with mock.patch.object(Path, "home", return_value=fake_home):
                # Use a name guaranteed not to exist on PATH or as a bundle
                self.assertFalse(_is_dependency_available("definitely-not-a-real-tool-xyz123"))


class CheckCapabilityClis(unittest.TestCase):
    def test_runs_without_exception(self):
        """The check must not crash even when dependencies are missing."""
        buf = io.StringIO()
        with redirect_stdout(buf):
            check_capability_clis()
        out = buf.getvalue()
        self.assertIn("Capability CLI requirements", out)

    def test_warns_when_dep_missing(self):
        """When a required dep is missing, output must contain the warning."""
        with tempfile.TemporaryDirectory() as tmp:
            fake_home = Path(tmp)
            # No ~/.claude/skills/gstack means gstack is "missing"
            # Need real flow_capability.load_registry — leave PATH alone so codex still resolves.
            with mock.patch.object(Path, "home", return_value=fake_home):
                buf = io.StringIO()
                with redirect_stdout(buf):
                    check_capability_clis()
                out = buf.getvalue()
        self.assertIn("Capability CLI requirements", out)
        # At least one capability requires_cli should now be flagged missing.
        # Without `~/.claude/skills/gstack/`, gstack-backed capabilities (≥10 entries)
        # are flagged. Output should mention "gstack" and a count > 0.
        self.assertIn("gstack", out)


if __name__ == "__main__":
    unittest.main()
