#!/usr/bin/env python3
"""Smoke tests for sub-project #3 — skill compatibility diff hook."""
from __future__ import annotations

import importlib
import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class KeywordExtraction(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_skill_diff", None)
        cls.mod = importlib.import_module("flow_skill_diff")

    def test_tokenize_lowercases_and_filters_short(self):
        out = self.mod.tokenize("Brainstorm IDEAS for a new feature")
        self.assertIn("brainstorm", out)
        self.assertIn("ideas", out)
        self.assertIn("feature", out)
        self.assertNotIn("for", out)  # 3-letter, filtered by len ≥ 4
        self.assertNotIn("a", out)    # too short
        self.assertNotIn("new", out)  # too short (3 chars)

    def test_tokenize_drops_stopwords(self):
        out = self.mod.tokenize("plugin description claude default skill")
        # All these are in STOPWORDS
        self.assertEqual(out, set())

    def test_overlap_coef_perfect_match(self):
        a = {"brainstorm", "ideation"}
        b = {"brainstorm", "ideation", "phase", "explore", "approaches"}
        # |A ∩ B| / min(|A|, |B|) = 2/2 = 1.0
        self.assertEqual(self.mod.overlap_coef(a, b), 1.0)

    def test_overlap_coef_no_match(self):
        self.assertEqual(self.mod.overlap_coef({"a"}, {"b"}), 0.0)

    def test_overlap_coef_empty(self):
        self.assertEqual(self.mod.overlap_coef(set(), {"a"}), 0.0)
        self.assertEqual(self.mod.overlap_coef({"a"}, set()), 0.0)


class DiffDetection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_skill_diff", None)
        cls.mod = importlib.import_module("flow_skill_diff")

    def test_new_plugin_detected(self):
        prev = {"a@m": "1.0"}
        curr = {"a@m": "1.0", "b@m": "2.0"}
        out = self.mod.detect_new_or_upgraded(curr, prev)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "b@m")
        self.assertEqual(out[0][2], "new")

    def test_upgrade_detected(self):
        prev = {"a@m": "1.0"}
        curr = {"a@m": "2.0"}
        out = self.mod.detect_new_or_upgraded(curr, prev)
        self.assertEqual(len(out), 1)
        self.assertIn("upgrade from 1.0", out[0][2])

    def test_unchanged_not_detected(self):
        same = {"a@m": "1.0"}
        out = self.mod.detect_new_or_upgraded(same, same)
        self.assertEqual(out, [])

    def test_removed_not_detected(self):
        # diff is one-directional: only NEW or UPGRADED, not REMOVED
        prev = {"a@m": "1.0", "b@m": "2.0"}
        curr = {"a@m": "1.0"}
        out = self.mod.detect_new_or_upgraded(curr, prev)
        self.assertEqual(out, [])


class CacheBehavior(unittest.TestCase):
    """Per-(spec, version) cache must avoid re-running expensive analysis."""

    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_skill_diff", None)
        cls.mod = importlib.import_module("flow_skill_diff")

    def test_cache_path_is_filesystem_safe(self):
        # @ and / must not appear in resulting filename
        p = self.mod.cache_path("plugin@marketplace/with-slash", "1.0.0")
        self.assertNotIn("@", p.name)
        self.assertNotIn("/", p.name)

    def test_cache_round_trip(self):
        # Construct a tmp REPO-like layout with a fake registry
        with tempfile.TemporaryDirectory(prefix="flow-skd-") as tmp:
            tmpdir = Path(tmp)
            cache_dir = tmpdir / "cache"
            cache_dir.mkdir()
            # Patch module's CACHE_DIR
            orig_cache_dir = self.mod.CACHE_DIR
            self.mod.CACHE_DIR = cache_dir
            try:
                # First write
                f = self.mod.cache_path("foo@bar", "1.0")
                f.write_text(json.dumps({"spec": "foo@bar", "version": "1.0", "candidates": []}), encoding="utf-8")
                self.assertTrue(f.is_file())
                data = json.loads(f.read_text(encoding="utf-8"))
                self.assertEqual(data["spec"], "foo@bar")
            finally:
                self.mod.CACHE_DIR = orig_cache_dir


class RenderPendingMd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sys.modules.pop("flow_skill_diff", None)
        cls.mod = importlib.import_module("flow_skill_diff")

    def test_no_candidates_section(self):
        out = self.mod.render_pending_md([
            {"spec": "x@y", "version": "1.0", "reason": "new", "candidates": []}
        ])
        self.assertIn("x@y", out)
        self.assertIn("No capability overlap", out)
        self.assertIn("flow skill-diff clear", out)

    def test_with_candidates_section(self):
        out = self.mod.render_pending_md([
            {
                "spec": "x@y", "version": "1.0", "reason": "upgrade from 0.9",
                "candidates": [
                    ("brainstorm", 0.85, "superpowers:brainstorming"),
                    ("tdd", 0.5, "superpowers:test-driven-development"),
                ],
            }
        ])
        self.assertIn("**brainstorm**", out)
        self.assertIn("0.85", out)
        self.assertIn("upgrade from 0.9", out)
        self.assertIn("Szymkiewicz", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
