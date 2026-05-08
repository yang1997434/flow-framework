#!/usr/bin/env python3
"""Smoke tests for v0.5 context_estimator.

Note: tests that depend on a specific MODEL_LIMITS value MUST pin
FLOW_CONTEXT_LIMIT in the env (highest priority rung in
`_resolve_limit`), otherwise host machine settings.json env aliases
(e.g. ``ANTHROPIC_DEFAULT_SONNET_MODEL=...[1m]``) leak into the
estimator and change the answer. Added 2026-05-07 alongside the
1M-misdetection bugfix.
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class EstimatePct(unittest.TestCase):
    def test_returns_none_low_for_missing_file(self):
        from common.context_estimator import estimate_context_pct
        pct, conf = estimate_context_pct("/nonexistent/path/transcript.jsonl")
        self.assertIsNone(pct)
        self.assertEqual(conf, "low")

    def test_returns_none_low_for_unreadable(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            p.write_text("")
            pct, conf = estimate_context_pct(p)
            # Empty file → 0% but confidence low (no model detected)
            self.assertEqual(pct, 0)
            self.assertEqual(conf, "low")

    def test_known_size_with_default_model_limit(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            # Write 100KB of fake JSONL with a model field
            line = json.dumps({"model": "claude-sonnet-4-6", "x": "a" * 200}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            # Pin limit to 200k; otherwise host settings.json with
            # ANTHROPIC_DEFAULT_SONNET_MODEL=...[1m] would upgrade to 1M
            # and the assertion range below would not hold.
            with mock.patch.dict(os.environ,
                                 {"FLOW_CONTEXT_LIMIT": "200000"},
                                 clear=False):
                pct, conf = estimate_context_pct(p)
            # 100KB / 4 = 25k tokens; sonnet limit 200k → ~12-13%
            self.assertIsNotNone(pct)
            self.assertGreaterEqual(pct, 10)
            self.assertLessEqual(pct, 20)
            self.assertIn(conf, ("medium", "high"))

    def test_opus_1m_uses_1m_limit(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            line = json.dumps({"model": "claude-opus-4-7[1m]", "x": "a" * 200}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            pct, _conf = estimate_context_pct(p)
            # 100KB / 4 = 25k tokens; 1M limit → 2-3%
            self.assertIsNotNone(pct)
            self.assertLessEqual(pct, 5)

    def test_unknown_model_falls_back_to_200k(self):
        from common.context_estimator import estimate_context_pct
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "t.jsonl"
            line = json.dumps({"x": "no model field at all"}) + "\n"
            with p.open("w") as f:
                while p.stat().st_size < 100_000:
                    f.write(line)
            pct, conf = estimate_context_pct(p)
            self.assertEqual(conf, "low")  # no model → low confidence
            self.assertIsNotNone(pct)


if __name__ == "__main__":
    unittest.main(verbosity=2)
