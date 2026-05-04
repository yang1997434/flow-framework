#!/usr/bin/env python3
"""Smoke tests for v0.4 Lv1/Lv2/Lv3 autosave (subprojects #5 + #7).

Covers:
  Lv1  post-tool-bash git commit  -> idempotent append, mtime debounce
  Lv1  post-tool-edit batch flush -> 60s OR 10-edit threshold
  Lv3  distill cooldown           -> 5min suppression for non-explicit triggers
  Lv3  heartbeat trigger          -> 30min + 50 tool-call AND condition
  Lv3  distill enqueue            -> writes Sediment Notes marker only,
                                     never invokes an LLM directly

Run:
  python3 -m unittest tests.smoke.test_autosave -v
  bash tests/smoke/run.sh
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT / "scripts"))


def load_hook(name: str):
    """Load a hyphen-named hook module by file path."""
    file_map = {
        "post_tool_bash": REPO_ROOT / "claude" / "hooks" / "post-tool-bash.py",
        "post_tool_edit": REPO_ROOT / "claude" / "hooks" / "post-tool-edit.py",
        "stop": REPO_ROOT / "claude" / "hooks" / "stop.py",
    }
    spec = importlib.util.spec_from_file_location(name, file_map[name])
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def make_fake_repo(tmp: Path, slug: str = "01-01-demo") -> tuple[Path, Path]:
    """Create a fake project root with a .flow/ + active task. Returns (project_root, task_dir)."""
    (tmp / ".git").mkdir()
    flow = tmp / ".flow"
    tasks = flow / "tasks" / slug
    tasks.mkdir(parents=True)
    progress = tasks / "progress.md"
    progress.write_text(
        "# progress.md — demo\n\n"
        "## Plan\n\n"
        "<!-- TEMPLATE: placeholder -->\n\n"
        "## Sediment Notes\n\n"
        "<!-- TEMPLATE: placeholder -->\n",
        encoding="utf-8",
    )
    (flow / ".current-task").write_text(str(tasks), encoding="utf-8")
    return (tmp, tasks)


def isolated_flow_home(tmp: Path) -> Path:
    """Return a path under tmp suitable for use as $FLOW_HOME (isolates runtime)."""
    home = tmp / "flow-home"
    (home / ".runtime").mkdir(parents=True)
    return home


# ---------------------------------------------------------------------------
# Lv1 — git commit append (post-tool-bash)
# ---------------------------------------------------------------------------

class Lv1GitCommitAppend(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-lv1bash-"))
        self.project, self.task = make_fake_repo(self.tmp)
        self.flow_home = isolated_flow_home(self.tmp)
        self._old_env = dict(os.environ)
        os.environ["FLOW_HOME"] = str(self.flow_home)
        # Force fresh import so module picks up current env
        sys.modules.pop("post_tool_bash", None)
        self.mod = load_hook("post_tool_bash")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_append_writes_commit_under_commits_heading(self):
        ok = self.mod.append_commit_to_progress(self.task, "abc1234", "feat: add thing")
        self.assertTrue(ok)
        text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("## Commits", text)
        self.assertIn("`abc1234`", text)
        self.assertIn("feat: add thing", text)

    def test_idempotent_same_hash_skipped(self):
        first = self.mod.append_commit_to_progress(self.task, "abc1234", "feat: add thing")
        second = self.mod.append_commit_to_progress(self.task, "abc1234", "feat: add thing")
        self.assertTrue(first)
        self.assertFalse(second, "second append for same hash must be a no-op")
        text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertEqual(text.count("`abc1234`"), 1)

    def test_distinct_hashes_within_minute_both_recorded(self):
        # Different commit hashes within the same minute: both should be logged
        # (hash uniqueness is the source of truth, not minute-bucket).
        self.mod.append_commit_to_progress(self.task, "aaa0001", "first")
        self.mod.append_commit_to_progress(self.task, "bbb0002", "second")
        text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("`aaa0001`", text)
        self.assertIn("`bbb0002`", text)


# ---------------------------------------------------------------------------
# Lv1 — file-touch batch flush (post-tool-edit)
# ---------------------------------------------------------------------------

class Lv1FileTouchBatch(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-lv1edit-"))
        self.project, self.task = make_fake_repo(self.tmp)
        self.flow_home = isolated_flow_home(self.tmp)
        self._old_env = dict(os.environ)
        os.environ["FLOW_HOME"] = str(self.flow_home)
        sys.modules.pop("post_tool_edit", None)
        self.mod = load_hook("post_tool_edit")

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_should_flush_time_threshold(self):
        now = time.time()
        # Just-flushed, low count -> no flush
        self.assertFalse(self.mod.should_flush(
            {"last_flush_epoch": now, "unflushed_count": 1}, now
        ))
        # 60+ seconds since last flush -> flush
        self.assertTrue(self.mod.should_flush(
            {"last_flush_epoch": now - 61, "unflushed_count": 1}, now
        ))

    def test_should_flush_count_threshold(self):
        now = time.time()
        self.assertFalse(self.mod.should_flush(
            {"last_flush_epoch": now, "unflushed_count": 9}, now
        ))
        self.assertTrue(self.mod.should_flush(
            {"last_flush_epoch": now, "unflushed_count": 10}, now
        ))

    def test_collect_recent_files_dedupes(self):
        log = self.flow_home / ".runtime" / "demo.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        records = [
            {"ts": "2026-05-04T01:00:00", "tool": "Edit", "path": str(self.project / "a.py")},
            {"ts": "2026-05-04T01:00:01", "tool": "Edit", "path": str(self.project / "b.py")},
            {"ts": "2026-05-04T01:00:02", "tool": "Write", "path": str(self.project / "a.py")},
        ]
        log.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
        recent = self.mod.collect_recent_files(log, self.project, n=5)
        self.assertEqual(len(recent), 2, "duplicates across records must collapse")
        # Most recent unique edit ('a.py' at index 2) should come first
        self.assertEqual(recent[0], "a.py")

    def test_upsert_creates_files_touched_section(self):
        progress = self.task / "progress.md"
        block = "_test block_:\n\n- `foo.py`"
        ok = self.mod.upsert_files_section(progress, block)
        self.assertTrue(ok)
        text = progress.read_text(encoding="utf-8")
        self.assertIn("## Files Touched", text)
        self.assertIn("`foo.py`", text)

    def test_upsert_replaces_existing_body(self):
        progress = self.task / "progress.md"
        self.mod.upsert_files_section(progress, "old block")
        self.mod.upsert_files_section(progress, "new block")
        text = progress.read_text(encoding="utf-8")
        self.assertIn("new block", text)
        self.assertNotIn("old block", text, "second upsert must replace, not stack")


# ---------------------------------------------------------------------------
# Lv3 — distill cooldown + heartbeat
# ---------------------------------------------------------------------------

class Lv3DistillCooldown(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-lv3-"))
        self.project, self.task = make_fake_repo(self.tmp)
        self.flow_home = isolated_flow_home(self.tmp)
        self._old_env = dict(os.environ)
        os.environ["FLOW_HOME"] = str(self.flow_home)
        # Force fresh import to pick up FLOW_HOME
        for m in ("flow_autosave",):
            sys.modules.pop(m, None)
        import flow_autosave  # noqa: E402
        self.mod = flow_autosave

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_distill(self, trigger: str, cwd: Path | None = None) -> int:
        argv = [
            "flow_autosave.py", "distill",
            "--trigger", trigger,
            "--cwd", str(cwd or self.project),
        ]
        with mock.patch.object(sys, "argv", argv):
            try:
                self.mod.main()
            except SystemExit as e:
                return int(e.code or 0)
        return 0

    def test_first_distill_writes_marker_and_queue(self):
        self._run_distill("stop")
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("distill queued (trigger=stop)", progress_text)
        # Queue file populated
        queue = self.flow_home / ".runtime" / "distill-queue.jsonl"
        self.assertTrue(queue.is_file())
        rec = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(rec["trigger"], "stop")

    def test_cooldown_suppresses_within_5min(self):
        self._run_distill("stop")
        # Second stop trigger within 5 min should NOT add another marker
        self._run_distill("stop")
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertEqual(
            progress_text.count("distill queued (trigger=stop)"),
            1,
            "cooldown must suppress redundant non-explicit distills",
        )
        queue = self.flow_home / ".runtime" / "distill-queue.jsonl"
        records = [
            json.loads(ln) for ln in queue.read_text(encoding="utf-8").splitlines() if ln.strip()
        ]
        self.assertEqual(len(records), 1)

    def test_explicit_pause_bypasses_cooldown(self):
        self._run_distill("stop")
        self._run_distill("pause")  # explicit user trigger -> bypass
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("trigger=stop", progress_text)
        self.assertIn("trigger=pause", progress_text)

    def test_no_active_task_still_enqueues(self):
        # Remove pointer
        (self.project / ".flow" / ".current-task").unlink()
        rc = self._run_distill("stop")
        self.assertEqual(rc, 0)
        # No marker (no active task) — but queue still records
        queue = self.flow_home / ".runtime" / "distill-queue.jsonl"
        self.assertTrue(queue.is_file())
        rec = json.loads(queue.read_text(encoding="utf-8").splitlines()[0])
        self.assertIsNone(rec["task"])

    def test_distill_does_not_invoke_llm(self):
        """The orchestrator MUST NOT shell out to an LLM in a hook context.
        We assert by checking that no `claude`, `anthropic`, `codex`, or
        `gpt` command appears anywhere in the script path actually executed."""
        # We verify by introspection: the source file must not import any
        # known LLM SDK and must not subprocess any LLM CLI.
        autosave_src = (REPO_ROOT / "scripts" / "flow_autosave.py").read_text(encoding="utf-8")
        forbidden = ("import anthropic", "from anthropic", "import openai",
                     "subprocess.run([\"claude\"", "subprocess.run([\"codex\"",
                     "subprocess.Popen([\"claude\"", "subprocess.Popen([\"codex\"")
        for needle in forbidden:
            self.assertNotIn(
                needle, autosave_src,
                f"flow_autosave must not call LLM in-hook (found: {needle!r})",
            )


class Lv3Heartbeat(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-hb-"))
        self.project, self.task = make_fake_repo(self.tmp)
        self.flow_home = isolated_flow_home(self.tmp)
        self._old_env = dict(os.environ)
        os.environ["FLOW_HOME"] = str(self.flow_home)
        for m in ("flow_autosave",):
            sys.modules.pop(m, None)
        import flow_autosave  # noqa: E402
        self.mod = flow_autosave

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._old_env)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _run_heartbeat(self, increment: int = 1) -> int:
        argv = [
            "flow_autosave.py", "heartbeat",
            "--cwd", str(self.project),
            "--increment", str(increment),
        ]
        with mock.patch.object(sys, "argv", argv):
            try:
                self.mod.main()
            except SystemExit as e:
                return int(e.code or 0)
        return 0

    def test_heartbeat_below_thresholds_does_not_queue(self):
        # Simulate "last distill 1 minute ago" + 5 tool calls — neither met
        recent = datetime.now(timezone.utc).astimezone() - timedelta(minutes=1)
        self.mod.write_last_distill("manual", recent)
        self._run_heartbeat(increment=5)
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertNotIn("trigger=heartbeat", progress_text)

    def test_heartbeat_meets_both_thresholds_queues(self):
        # Last distill 35 min ago + tool count already at 60
        old = datetime.now(timezone.utc).astimezone() - timedelta(minutes=35)
        self.mod.write_last_distill("manual", old)
        self.mod.tool_count_path().write_text("59", encoding="utf-8")  # one more = 60
        self._run_heartbeat(increment=1)
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("trigger=heartbeat", progress_text)

    def test_heartbeat_only_time_met_does_not_queue(self):
        # 35 min ago BUT only 5 tool calls -> AND condition fails
        old = datetime.now(timezone.utc).astimezone() - timedelta(minutes=35)
        self.mod.write_last_distill("manual", old)
        self.mod.tool_count_path().write_text("4", encoding="utf-8")
        self._run_heartbeat(increment=1)
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertNotIn("trigger=heartbeat", progress_text)

    def test_heartbeat_only_count_met_does_not_queue(self):
        # 60 calls BUT last distill 1 min ago -> time condition fails
        recent = datetime.now(timezone.utc).astimezone() - timedelta(minutes=1)
        self.mod.write_last_distill("manual", recent)
        self.mod.tool_count_path().write_text("59", encoding="utf-8")
        self._run_heartbeat(increment=1)
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertNotIn("trigger=heartbeat", progress_text)


# ---------------------------------------------------------------------------
# Stop hook end-to-end (subprocess) — verifies it reaches autosave w/o errors
# ---------------------------------------------------------------------------

class StopHookE2E(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-stop-"))
        self.project, self.task = make_fake_repo(self.tmp)
        self.flow_home = isolated_flow_home(self.tmp)

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_stop_hook_invokes_autosave_distill(self):
        env = os.environ.copy()
        env["FLOW_HOME"] = str(self.flow_home)
        payload = json.dumps({"cwd": str(self.project)})
        result = subprocess.run(
            [sys.executable, str(REPO_ROOT / "claude" / "hooks" / "stop.py")],
            input=payload, capture_output=True, text=True, timeout=15, env=env,
        )
        self.assertEqual(result.returncode, 0, f"stderr: {result.stderr}")
        # Marker should now be in progress.md
        progress_text = (self.task / "progress.md").read_text(encoding="utf-8")
        self.assertIn("trigger=stop", progress_text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
