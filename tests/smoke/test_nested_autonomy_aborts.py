"""T21 — nested-autonomy mechanical guard via FLOW_AUTONOMY_PARENT_PID.

Subprocess-driven end-to-end tests that exercise the env-var-presence
guard at `flow orchestrator --auto-execute` entry point (§7 S5).

Three scenarios:
  1. Env var set + slug exists → exit 4 (`aborted_nested`) and a
     decisions.jsonl record with `decision == "aborted_nested"`.
  2. Env var set + slug missing → exit 4 (security-positive default;
     no info leak about slug existence). stderr names the env var.
  3. No env var → guard does not fire. Without contract.json the
     orchestrator falls back to interactive (exit 0); we assert
     stderr does NOT contain `aborted_nested`.

Plus a unit test covering env propagation through `auto_dispatch_task`:
  4. dispatch_fn receives `subagent_env` kwarg with
     FLOW_AUTONOMY_PARENT_PID=str(os.getpid()).

Pitfall defenses:
  F (env var fail-closed): non-empty env var → abort regardless of
    slug existence.
  S (wire-up gap): the guard MUST run inside flow_orchestrator.main(),
    not inside `_cmd_auto_execute` — these subprocess tests pin that
    by spawning real `python flow.py orchestrator --auto-execute`.
  K (no plausible-justify): we don't try to decode parent PID as int,
    or walk the process tree — env var presence IS the check.
  R (parent_pid str safe in stderr): test assertions read stderr text
    only; the env var value is treated as opaque string.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent

# Re-import the guard helpers so the unit test (Step 21.3) can drive
# them in-process.  Subprocess tests do NOT import — they exercise the
# real CLI entry point by spawning python.
sys.path.insert(0, str(REPO_ROOT / "scripts"))
sys.path.insert(0, str(REPO_ROOT / "scripts" / "common"))
import flow_orchestrator as fo  # type: ignore  # noqa: E402
from flow_orchestrator import (  # type: ignore  # noqa: E402
    AUTONOMY_PARENT_PID_ENV,
    auto_dispatch_task,
    TaskManifest,
)
from flow_contract import Contract  # type: ignore  # noqa: E402


def _run_flow(args, cwd: Path, env: dict | None = None):
    return subprocess.run(
        [sys.executable, str(REPO_ROOT / "scripts" / "flow.py"), *args],
        cwd=str(cwd),
        capture_output=True,
        text=True,
        env=env if env is not None else os.environ.copy(),
    )


def _setup_minimal_slug(tmp: Path, slug: str = "demo") -> Path:
    """Create .flow/tasks/<slug>/ with contract.json + progress.md so
    that build_plan succeeds. The contract uses autonomy_mode=auto and
    a no-op acceptance criterion so dispatch reaches the env-guard
    BEFORE any heavy machinery.
    """
    task_dir = tmp / ".flow" / "tasks" / slug
    task_dir.mkdir(parents=True)
    (task_dir / "contract.json").write_text(json.dumps({
        "contract_schema_version": 1,
        "autonomy_mode": "auto",
        "created_at": "2026-05-05T00:00:00Z",
        "scope": {"allowed": ["src/**"]},
        "acceptance_criteria": [
            {"description": "u", "type": "unit", "command": "true"},
        ],
    }))
    (task_dir / "progress.md").write_text(
        "---\n"
        "contract_path: contract.json\n"
        "contract_schema_version: 1\n"
        "autonomy_mode: auto\n"
        "---\n\n"
        "# progress.md\n\n"
        "## Plan\n\nDemo plan.\n\n"
        "### Tasks\n\n```yaml\n"
        "tasks:\n  - id: T1\n    writes: ['src/a.py']\n"
        "```\n"
    )
    return task_dir


class TestNestedAutonomySubprocess(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True),
        )

    def test_env_var_set_with_slug_aborts_with_exit_4(self):
        task_dir = _setup_minimal_slug(self.tmp, "demo")
        env = os.environ.copy()
        env[AUTONOMY_PARENT_PID_ENV] = str(os.getpid())
        r = _run_flow(
            ["orchestrator", "--auto-execute", "demo"],
            cwd=self.tmp, env=env,
        )
        self.assertEqual(
            r.returncode, 4,
            msg=f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        self.assertIn("aborted_nested", r.stderr.lower())
        # The env var name must surface so operators can diagnose.
        self.assertIn(AUTONOMY_PARENT_PID_ENV, r.stderr)
        # Decision record landed on disk.
        dec_path = task_dir / "decisions.jsonl"
        self.assertTrue(dec_path.is_file(), msg=r.stderr)
        records = [
            json.loads(ln) for ln in
            dec_path.read_text().splitlines() if ln.strip()
        ]
        nested = [
            r for r in records if r.get("decision") == "aborted_nested"
        ]
        self.assertEqual(
            len(nested), 1,
            msg=f"expected exactly one aborted_nested record, got {records}",
        )
        rec = nested[0]
        self.assertEqual(rec["task"], "demo")
        self.assertEqual(rec["phase"], 2)
        # parent_pid surfaces in `reason` for forensic recovery.
        self.assertIn(str(os.getpid()), rec["reason"])

    def test_env_var_set_with_missing_slug_still_aborts_exit_4(self):
        """Security-positive default (plan 7670-7677): even when the
        slug doesn't exist, the guard returns exit 4 rather than
        leaking a 'not found' message. The reason: a nested attempt is
        still a nested attempt; we MUST NOT advertise slug existence
        to a sub-process attacker.
        """
        env = os.environ.copy()
        env[AUTONOMY_PARENT_PID_ENV] = str(os.getpid())
        r = _run_flow(
            ["orchestrator", "--auto-execute", "no-such-slug"],
            cwd=self.tmp, env=env,
        )
        self.assertEqual(
            r.returncode, 4,
            msg=f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        # The env var name surfaces; the absent slug is named in the
        # error but no decisions.jsonl is created (no task_dir to
        # write into).
        self.assertIn(AUTONOMY_PARENT_PID_ENV, r.stderr)
        self.assertIn("nested autonomy", r.stderr.lower())

    def test_no_env_var_proceeds_to_interactive_fallback(self):
        """No FLOW_AUTONOMY_PARENT_PID set → guard does not fire.
        Without contract.json the orchestrator falls back to
        interactive mode (exit 0). We assert that the nested-abort
        message is absent — the only signal that the guard is wired
        but inert.
        """
        env = os.environ.copy()
        env.pop(AUTONOMY_PARENT_PID_ENV, None)
        # Slug exists but no contract.json → build_plan returns a
        # plan with `contract is None` and the orchestrator emits
        # `interactive fallback` on stderr (exit 0).
        slug_dir = self.tmp / ".flow" / "tasks" / "demo"
        slug_dir.mkdir(parents=True)
        (slug_dir / "progress.md").write_text(
            "# progress.md\n\n## Plan\n\nx\n"
        )
        r = _run_flow(
            ["orchestrator", "--auto-execute", "demo"],
            cwd=self.tmp, env=env,
        )
        self.assertEqual(
            r.returncode, 0,
            msg=f"stdout={r.stdout!r} stderr={r.stderr!r}",
        )
        self.assertNotIn("aborted_nested", r.stderr.lower())


class TestAutoDispatchPropagatesParentPid(unittest.TestCase):
    """Step 21.3 / 21.4: auto_dispatch_task must compose subagent_env
    that carries FLOW_AUTONOMY_PARENT_PID=<own pid> and pass it to
    dispatch_fn as a kwarg. The subagent's flow orchestrator
    --auto-execute attempt thus mechanically aborts via the same
    guard tested above.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(
            lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True),
        )
        # Reuse the same minimal repo bootstrap that
        # test_orchestrator_worktree.py uses.
        import subprocess as sp
        sp.run(["git", "init", "-q", "-b", "master", str(self.tmp)], check=True)
        sp.run(
            ["git", "-C", str(self.tmp), "config", "user.email", "t@t"],
            check=True,
        )
        sp.run(
            ["git", "-C", str(self.tmp), "config", "user.name", "T"],
            check=True,
        )
        (self.tmp / "README.md").write_text("# r\n")
        sp.run(["git", "-C", str(self.tmp), "add", "."], check=True)
        sp.run(
            ["git", "-C", str(self.tmp),
             "commit", "-q", "-m", "init"],
            check=True,
        )
        # Minimal slug dir (auto_dispatch_task expects task_dir to exist).
        (self.tmp / ".flow" / "tasks" / "demo").mkdir(parents=True)
        # Contract instance — used directly, no JSON round-trip.
        self.contract = Contract(
            contract_schema_version=1,
            autonomy_mode="auto",
            created_at="2026-05-05T00:00:00Z",
            scope_allowed=["src/**"],
            scope_forbidden=[],
        )
        # Empty manifest — dispatch_fn is a no-op so verification passes.
        self.manifest = TaskManifest(
            id="T1",
            writes_declared=[],
            allowed_writes=[],
            out_of_scope=[],
            forbidden_hits=[],
            shared_hits=[],
        )

    def test_auto_dispatch_passes_parent_pid_in_subagent_env(self):
        captured: dict = {}

        def fake_dispatch(ctx, **kw):
            captured["ctx"] = ctx
            captured["subagent_env"] = kw.get("subagent_env")

        outcome = auto_dispatch_task(
            slug="demo",
            task_idx=0,
            repo_root=self.tmp,
            dispatch_fn=fake_dispatch,
            contract=self.contract,
            manifest=self.manifest,
            run_id="run-prop",
            contract_path=self.tmp / "contract.json",
            contract_hash="cafebabe" * 8,
        )
        self.assertEqual(outcome.status, "ok")
        self.assertIsNotNone(captured.get("subagent_env"))
        env = captured["subagent_env"]
        self.assertIn(AUTONOMY_PARENT_PID_ENV, env)
        self.assertEqual(
            env[AUTONOMY_PARENT_PID_ENV], str(os.getpid()),
        )
        # Sanity: env is a copy, not the live os.environ — orchestrator
        # never mutates parent process state.
        self.assertIsNot(env, os.environ)


if __name__ == "__main__":
    unittest.main()
