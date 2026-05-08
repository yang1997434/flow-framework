"""T22 Step 22.0b — smoke for ``flow_subagent_dispatch.invoke()`` resolution
chain.

Covers:
  * env-var template -> shell command actually runs (touch a marker)
  * no env var + no capability config -> RuntimeError (fail closed)
  * subagent nonzero returncode is a soft warn, not an exception
  * R-class: slug/task_id with shell metacharacters raise ValueError

Out-of-scope (manual / v0.8.2): end-to-end orchestrator -> shim wiring
through a real Claude CLI invocation. The orchestrator-side import is
verified by the wire-up grep in the implementer report; this file
exercises the shim contract directly with a duck-typed ctx.
"""
from __future__ import annotations

import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))


class _Ctx:
    def __init__(self, worktree_path: Path, slug: str, task_id: str):
        self.worktree_path = worktree_path
        self.slug = slug
        self.task_id = task_id


class TestSubagentDispatchShim(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-shim-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.ctx = _Ctx(
            worktree_path=self.tmp,
            slug="demo",
            task_id="T0",
        )
        # Make sure no stale env var leaks across tests.
        self._orig_env = os.environ.get("FLOW_SUBAGENT_DISPATCH_CMD")
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        # Force a fresh import each test so module-level state never
        # leaks (capability file caching etc.).
        sys.modules.pop("flow_subagent_dispatch", None)

    def tearDown(self):
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        if self._orig_env is not None:
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = self._orig_env

    def test_env_var_template_invoked(self):
        """FLOW_SUBAGENT_DISPATCH_CMD env var -> shell command runs."""
        marker = self.tmp / "dispatched"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"touch {marker.as_posix()}"
        )
        from flow_subagent_dispatch import invoke
        invoke(self.ctx)
        self.assertTrue(
            marker.is_file(),
            "shim must execute the env-var template",
        )

    def test_env_var_passes_slug_and_task_id_to_template(self):
        """Template placeholders {slug} {task_id} get substituted."""
        marker = self.tmp / "received"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{slug}}:{{task_id}} > {marker.as_posix()}"
        )
        from flow_subagent_dispatch import invoke
        invoke(self.ctx)
        self.assertTrue(marker.is_file())
        self.assertEqual(
            marker.read_text(encoding="utf-8").strip(),
            "demo:T0",
        )

    def test_subagent_env_propagated(self):
        """subagent_env kwarg overrides env vars in the spawned process."""
        marker = self.tmp / "envtest"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"sh -c 'echo $FLOW_AUTONOMY_PARENT_PID > {marker.as_posix()}'"
        )
        from flow_subagent_dispatch import invoke
        invoke(self.ctx, subagent_env={"FLOW_AUTONOMY_PARENT_PID": "12345"})
        self.assertTrue(marker.is_file())
        self.assertEqual(
            marker.read_text(encoding="utf-8").strip(),
            "12345",
        )

    def test_no_config_raises_runtime_error(self):
        """No env var + no capability file -> RuntimeError (fail closed)."""
        # cd to a tmp dir so the relative capability path doesn't resolve
        # to the real repo's defaults.json.
        orig_cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            from flow_subagent_dispatch import invoke
            with self.assertRaises(RuntimeError) as cm:
                invoke(self.ctx)
            self.assertIn("FLOW_SUBAGENT_DISPATCH_CMD", str(cm.exception))
        finally:
            os.chdir(orig_cwd)

    def test_subagent_nonzero_returncode_warns_not_raises(self):
        """Subagent failure is soft - orchestrator handles via gates."""
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "false"
        from flow_subagent_dispatch import invoke
        # Should NOT raise.
        invoke(self.ctx)

    def test_slug_with_shell_metachar_rejected(self):
        """R-class: slug containing shell metacharacters raises ValueError."""
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true"
        bad_ctx = _Ctx(self.tmp, slug="demo;rm -rf /", task_id="T0")
        from flow_subagent_dispatch import invoke
        with self.assertRaises(ValueError) as cm:
            invoke(bad_ctx)
        self.assertIn("slug", str(cm.exception))

    def test_task_id_with_shell_metachar_rejected(self):
        """R-class: task_id containing $() is rejected before format()."""
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true"
        bad_ctx = _Ctx(self.tmp, slug="demo", task_id="T0$(whoami)")
        from flow_subagent_dispatch import invoke
        with self.assertRaises(ValueError) as cm:
            invoke(bad_ctx)
        self.assertIn("task_id", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
