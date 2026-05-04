#!/usr/bin/env python3
"""Smoke tests for v0.5 mechanical payload builder."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def _has_git() -> bool:
    return shutil.which("git") is not None


@unittest.skipUnless(_has_git(), "git not available")
class BuildMechanicalPayload(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-mech-"))
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        env = os.environ.copy()
        env.setdefault("GIT_AUTHOR_NAME", "test")
        env.setdefault("GIT_AUTHOR_EMAIL", "t@t")
        env.setdefault("GIT_COMMITTER_NAME", "test")
        env.setdefault("GIT_COMMITTER_EMAIL", "t@t")
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        self.task_dir = self.tmp / ".flow" / "tasks" / "01-01-test"
        self.task_dir.mkdir(parents=True)
        (self.task_dir / "progress.md").write_text(
            "---\nphase: phase-2-execute\nstatus: active\n---\n", encoding="utf-8"
        )

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_payload_has_required_top_level_fields(self):
        from common.mechanical import build_payload
        payload = build_payload(
            project_root=self.tmp,
            task_dir=self.task_dir,
            trigger="precompact",
            transcript_path=None,
        )
        for k in ("schema_version", "ts", "trigger", "task_slug", "phase", "git",
                  "files_touched_recent", "context_pct_estimated",
                  "transcript_path_size_bytes"):
            self.assertIn(k, payload)
        self.assertEqual(payload["trigger"], "precompact")
        self.assertEqual(payload["task_slug"], "01-01-test")
        self.assertEqual(payload["git"]["branch"], "main")
        self.assertTrue(payload["git"]["head"])
        self.assertIsInstance(payload["git"]["recent_commits"], list)
        self.assertGreaterEqual(len(payload["git"]["recent_commits"]), 1)
        self.assertEqual(payload["phase"], "phase-2-execute")

    def test_phase_extracted_from_progress_frontmatter(self):
        from common.mechanical import build_payload
        (self.task_dir / "progress.md").write_text(
            "---\nphase: phase-3-finish\n---\n", encoding="utf-8"
        )
        payload = build_payload(
            project_root=self.tmp, task_dir=self.task_dir,
            trigger="post-tool", transcript_path=None,
        )
        self.assertEqual(payload["phase"], "phase-3-finish")

    def test_no_progress_md_returns_phase_unknown(self):
        from common.mechanical import build_payload
        (self.task_dir / "progress.md").unlink()
        payload = build_payload(
            project_root=self.tmp, task_dir=self.task_dir,
            trigger="post-tool", transcript_path=None,
        )
        self.assertEqual(payload["phase"], "unknown")


if __name__ == "__main__":
    unittest.main(verbosity=2)
