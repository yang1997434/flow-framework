"""Smoke tests for glob_overlap helper.

Tests cover the formal contract:
  - Two literal paths overlap iff identical
  - A glob and a path overlap iff the glob matches the path
  - Two globs overlap iff there exists at least one path matching both
  - Recursive ** matches nested paths
  - Non-recursive * matches only at one level
  - Bare '*' / '**' / '**/*' are flagged as overforbroad
  - Negation '!path' patterns are rejected
  - Repo-relative paths only (no leading /, no '..')
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from common.glob_overlap import globs_overlap, is_broad_glob, validate_glob, GlobError  # noqa: E402


class TestLiteralPaths(unittest.TestCase):
    def test_identical_literals_overlap(self):
        self.assertTrue(globs_overlap(["src/foo.py"], ["src/foo.py"]))

    def test_different_literals_do_not_overlap(self):
        self.assertFalse(globs_overlap(["src/foo.py"], ["src/bar.py"]))

    def test_disjoint_directories(self):
        self.assertFalse(globs_overlap(["src/auth/login.py"], ["src/api/handlers.py"]))


class TestGlobAndPath(unittest.TestCase):
    def test_glob_matches_path(self):
        self.assertTrue(globs_overlap(["src/auth/**"], ["src/auth/login.py"]))

    def test_glob_does_not_match_unrelated_path(self):
        self.assertFalse(globs_overlap(["src/auth/**"], ["src/api/handlers.py"]))

    def test_recursive_glob_matches_nested(self):
        self.assertTrue(globs_overlap(["**/package.json"], ["packages/foo/package.json"]))

    def test_non_recursive_glob_does_not_match_nested(self):
        # '*.lock' alone matches only repo root .lock files, NOT nested
        self.assertFalse(globs_overlap(["*.lock"], ["packages/foo/yarn.lock"]))


class TestTwoGlobs(unittest.TestCase):
    def test_overlapping_globs(self):
        self.assertTrue(globs_overlap(["src/**"], ["src/auth/**"]))

    def test_disjoint_glob_directories(self):
        self.assertFalse(globs_overlap(["src/auth/**"], ["src/api/**"]))

    def test_extension_glob_overlap(self):
        # both match src/auth/login.py
        self.assertTrue(globs_overlap(["src/auth/**"], ["**/*.py"]))


class TestBroadGlobDetection(unittest.TestCase):
    def test_bare_star_is_broad(self):
        self.assertTrue(is_broad_glob("*"))

    def test_bare_double_star_is_broad(self):
        self.assertTrue(is_broad_glob("**"))

    def test_double_star_slash_star_is_broad(self):
        self.assertTrue(is_broad_glob("**/*"))

    def test_specific_glob_not_broad(self):
        self.assertFalse(is_broad_glob("src/**/*.py"))

    def test_literal_path_not_broad(self):
        self.assertFalse(is_broad_glob("VERSION"))


class TestValidate(unittest.TestCase):
    def test_negation_rejected(self):
        with self.assertRaises(GlobError):
            validate_glob("!src/secret.py")

    def test_absolute_path_rejected(self):
        with self.assertRaises(GlobError):
            validate_glob("/etc/passwd")

    def test_parent_dir_rejected(self):
        with self.assertRaises(GlobError):
            validate_glob("../escape.py")

    def test_normal_glob_accepted(self):
        validate_glob("src/auth/**")  # no raise


if __name__ == "__main__":
    unittest.main()
