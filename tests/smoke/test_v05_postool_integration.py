#!/usr/bin/env python3
"""Integration tests for v0.5 PostToolUse hooks (post-tool-bash + post-tool-edit).

Drives each hook as a subprocess with realistic stdin and asserts the JSON
output shape. Complements test_v05_postool_nudge.py (which covers the nudge
helper in isolation).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
HOOK_BASH = REPO_ROOT / "claude" / "hooks" / "post-tool-bash.py"
HOOK_EDIT = REPO_ROOT / "claude" / "hooks" / "post-tool-edit.py"


def _has_git() -> bool:
    return shutil.which("git") is not None


def _make_transcript(path: Path, target_bytes: int, model: str) -> None:
    """Build a JSONL transcript with model field + padded user content so
    file size meets the target. estimate_context_pct uses bytes/4 / limit,
    so for sonnet (200k tokens) we need bytes ≥ 400_000 to clear the 50%
    threshold (250 KB → 31%, 500 KB → 62%)."""
    head = json.dumps({"model": model, "type": "system"}, ensure_ascii=False) + "\n"
    pad_record = json.dumps({"role": "user", "content": "x" * 1024}, ensure_ascii=False) + "\n"
    with path.open("w", encoding="utf-8") as f:
        f.write(head)
        written = len(head)
        while written < target_bytes:
            f.write(pad_record)
            written += len(pad_record)


def _setup_project(tmp: Path) -> Path:
    """Create .flow + active task in tmp. Returns the task dir path."""
    flow = tmp / ".flow"
    task = flow / "tasks" / "01-01-demo"
    task.mkdir(parents=True)
    (task / "prd.md").write_text("# Demo\nstuff\n", encoding="utf-8")
    (task / "progress.md").write_text(
        "---\nphase: phase-2-execute\n---\n", encoding="utf-8"
    )
    (flow / ".current-task").write_text(
        str(task.relative_to(tmp)), encoding="utf-8"
    )
    return task


def _isolated_runtime_env(runtime_root: Path) -> dict:
    """Env that pins FLOW_HOME so hook nudge state stays in tempdir."""
    env = os.environ.copy()
    env["FLOW_HOME"] = str(runtime_root)
    # Detach heartbeat fast; tests don't care about autosave.
    return env


@unittest.skipUnless(_has_git(), "git not available")
class PostToolBashEmits(unittest.TestCase):
    """Subprocess-driven tests covering the 3 emit paths in post-tool-bash.py."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-pb-")).resolve()
        self.runtime = Path(tempfile.mkdtemp(prefix="flow-rt-")).resolve()
        # init git so the hook's git-commit branch can resolve a real HEAD
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
        })
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        self.task = _setup_project(self.tmp)
        self.transcript = self.tmp / "transcript.jsonl"
        # 500KB → 62.5% on sonnet 200k → above 50% threshold w/ high confidence
        _make_transcript(self.transcript, 500_000, "claude-sonnet-4-6")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def _run(self, command: str) -> dict | None:
        """Invoke post-tool-bash with the given Bash command + high-ctx
        transcript. Returns parsed stdout JSON (or None if no output)."""
        hook_input = {
            "cwd": str(self.tmp),
            "tool_input": {"command": command},
            "transcript_path": str(self.transcript),
        }
        result = subprocess.run(
            ["python3", str(HOOK_BASH)],
            input=json.dumps(hook_input),
            capture_output=True, text=True, timeout=15,
            env=_isolated_runtime_env(self.runtime),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        out = result.stdout.strip()
        return json.loads(out) if out else None

    def test_non_git_command_high_ctx_emits_nudge_only(self):
        out = self._run("ls -la")
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("flow-checkpoint-suggested", ctx)
        self.assertNotIn("flow-credential-warning", ctx)

    def test_git_commit_no_credentials_emits_nudge_only(self):
        # Make a commit so the hook's git-commit branch finds a HEAD subject.
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
        })
        (self.tmp / "x.txt").write_text("y\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "noop"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        out = self._run("git commit -m noop")
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("flow-checkpoint-suggested", ctx)
        self.assertNotIn("flow-credential-warning", ctx)

    def test_git_commit_no_match_no_credential_warning(self):
        # Without any credential-pattern match in .flow/, the hook fires the
        # nudge alone — no <flow-credential-warning> envelope.
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
        })
        (self.tmp / "bland.txt").write_text("nothing sensitive\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "+bland"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        out = self._run("git commit -m +bland")
        self.assertIsNotNone(out)
        ctx = out["hookSpecificOutput"]["additionalContext"]
        self.assertIn("flow-checkpoint-suggested", ctx)
        self.assertNotIn("flow-credential-warning", ctx)


@unittest.skipUnless(_has_git(), "git not available")
class PostToolHooksSymmetry(unittest.TestCase):
    """Bash and Edit hooks must emit equivalent additionalContext for
    equivalent state (i.e., the nudge text body)."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-sym-")).resolve()
        self.runtime = Path(tempfile.mkdtemp(prefix="flow-rt-")).resolve()
        subprocess.run(["git", "init", "-q", "-b", "main", str(self.tmp)], check=True)
        env = os.environ.copy()
        env.update({
            "GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
        })
        (self.tmp / "README").write_text("hi\n")
        subprocess.run(["git", "-C", str(self.tmp), "add", "."], check=True,
                       env=env, stdout=subprocess.DEVNULL)
        subprocess.run(["git", "-C", str(self.tmp), "commit", "-q", "-m", "init"],
                       check=True, env=env, stdout=subprocess.DEVNULL)
        _setup_project(self.tmp)
        self.transcript = self.tmp / "transcript.jsonl"
        _make_transcript(self.transcript, 500_000, "claude-sonnet-4-6")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)
        shutil.rmtree(self.runtime, ignore_errors=True)

    def _run_bash(self) -> dict | None:
        hook_input = {
            "cwd": str(self.tmp),
            "tool_input": {"command": "ls"},
            "transcript_path": str(self.transcript),
        }
        r = subprocess.run(
            ["python3", str(HOOK_BASH)],
            input=json.dumps(hook_input),
            capture_output=True, text=True, timeout=15,
            env=_isolated_runtime_env(self.runtime),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def _run_edit(self) -> dict | None:
        target = self.tmp / "scratch.txt"
        target.write_text("placeholder\n")
        hook_input = {
            "cwd": str(self.tmp),
            "tool_name": "Edit",
            "tool_input": {"file_path": str(target)},
            "transcript_path": str(self.transcript),
        }
        r = subprocess.run(
            ["python3", str(HOOK_EDIT)],
            input=json.dumps(hook_input),
            capture_output=True, text=True, timeout=15,
            env=_isolated_runtime_env(self.runtime),
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout) if r.stdout.strip() else None

    def test_both_hooks_fire_equivalent_nudge_text(self):
        # Run edit first so it gets the fresh window. Then run bash with the
        # same FLOW_HOME — but it'll be suppressed because nudge already
        # fired (per-window throttle). To compare bodies, use two different
        # FLOW_HOMEs by clearing state between calls.
        edit_out = self._run_edit()
        # Reset runtime so bash hook gets a fresh window — apples to apples.
        shutil.rmtree(self.runtime, ignore_errors=True)
        self.runtime.mkdir()
        bash_out = self._run_bash()

        self.assertIsNotNone(edit_out)
        self.assertIsNotNone(bash_out)
        edit_ctx = edit_out["hookSpecificOutput"]["additionalContext"]
        bash_ctx = bash_out["hookSpecificOutput"]["additionalContext"]
        # Both must carry the nudge marker.
        self.assertIn("flow-checkpoint-suggested", edit_ctx)
        self.assertIn("flow-checkpoint-suggested", bash_ctx)
        # The verbatim user-facing line must match in both.
        for ctx in (edit_ctx, bash_ctx):
            self.assertIn("/flow:pause", ctx)
            self.assertIn("/flow:resume", ctx)


if __name__ == "__main__":
    unittest.main(verbosity=2)
