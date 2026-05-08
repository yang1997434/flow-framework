"""T22 Step 22.0b — smoke for ``flow_subagent_dispatch.invoke()`` resolution
chain.

Covers:
  * env-var template -> shell command actually runs (touch a marker)
  * no env var + no capability config -> RuntimeError (fail closed)
  * subagent nonzero returncode is a soft warn, not an exception
  * R-class: slug/task_id with shell metacharacters raise ValueError
  * F1 (codex round-1): explicit ``task_id`` kwarg overrides ctx attribute
  * F2 (codex round-1): default capability config has NO ``dispatch_cmd``
    field (v0.8.1 ships infra only; production wire-up is operator-supplied)
  * F3 (codex round-1): CAPABILITY_FILE resolves via ``__file__`` (module
    path), not cwd — survives ``os.chdir`` to arbitrary tmp dirs
  * F4 (codex round-1): worktree path with spaces / metachars is shell-
    quoted via ``shlex.quote`` before format()

Out-of-scope (manual / v0.8.2): end-to-end orchestrator -> shim wiring
through a real Claude CLI invocation. The orchestrator-side import is
verified by the wire-up grep in the implementer report; this file
exercises the shim contract directly with a duck-typed ctx.
"""
from __future__ import annotations

import json
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


class _CtxNoTaskId:
    """Production-shape ctx (T22 codex round-1 F1): mirrors
    ``WorktreeContext`` which has NO ``task_id`` field. Used to verify
    the orchestrator's ``task_id=manifest.id`` kwarg propagates through
    the shim."""
    def __init__(self, worktree_path: Path, slug: str):
        self.worktree_path = worktree_path
        self.slug = slug


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

    # ── F1 (codex round-1): task_id kwarg overrides ctx attribute ────
    def test_task_id_kwarg_overrides_ctx(self):
        """Production WorktreeContext has NO task_id; orchestrator passes
        ``task_id=manifest.id`` as kwarg. Verify the kwarg is the
        authoritative source: ctx without task_id + kwarg=T7 → template
        renders with T7, not "" (which would interpolate ``--task ``)."""
        marker = self.tmp / "received-task"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{slug}}:{{task_id}} > {marker.as_posix()}"
        )
        prod_ctx = _CtxNoTaskId(self.tmp, slug="demo")
        from flow_subagent_dispatch import invoke
        invoke(prod_ctx, task_id="T7")
        self.assertTrue(marker.is_file())
        self.assertEqual(
            marker.read_text(encoding="utf-8").strip(),
            "demo:T7",
        )

    def test_task_id_kwarg_overrides_ctx_attribute_when_both_present(self):
        """If ctx has task_id="OLD" and kwarg has task_id="NEW", kwarg wins.
        This is the canonical wiring — orchestrator authority over ctx."""
        marker = self.tmp / "kwarg-wins"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{task_id}} > {marker.as_posix()}"
        )
        ctx = _Ctx(self.tmp, slug="demo", task_id="OLD")
        from flow_subagent_dispatch import invoke
        invoke(ctx, task_id="NEW")
        self.assertEqual(
            marker.read_text(encoding="utf-8").strip(),
            "NEW",
        )

    # ── F2 (codex round-1): default capability has no dispatch_cmd ───
    def test_default_capability_has_no_dispatch_cmd_field(self):
        """v0.8.1 must NOT ship a default ``dispatch_cmd`` because the
        SKILL handle ``flow:flow-phase2-execute --slug ...`` is not a
        shell command — running it under shell=True yields rc=127. The
        capability entry stays (autonomy_orchestrator is promoted to
        v0.8.1) but ``dispatch_cmd`` is operator-supplied via env var."""
        caps_path = REPO_ROOT / "claude" / "capabilities" / "defaults.json"
        caps = json.loads(caps_path.read_text(encoding="utf-8"))
        entry = caps.get("capabilities", {}).get("autonomy_orchestrator", {})
        self.assertIsInstance(
            entry, dict,
            "autonomy_orchestrator entry must still exist (promoted in v0.8.1)",
        )
        self.assertNotIn(
            "dispatch_cmd", entry,
            "v0.8.1 must NOT ship a default dispatch_cmd — production "
            "wire-up requires operator FLOW_SUBAGENT_DISPATCH_CMD env var "
            "(see codex round-1 F2). The SKILL handle is not a shell command.",
        )

    def test_no_env_var_raises_runtime_error_with_actionable_message(self):
        """F2 follow-up: with default ``dispatch_cmd`` absent and no env
        var, invoke() must raise RuntimeError pointing operators at
        FLOW_SUBAGENT_DISPATCH_CMD. (Replaces the implicit check that
        was effectively masked by the broken default.)"""
        from flow_subagent_dispatch import invoke
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx)
        msg = str(cm.exception)
        self.assertIn("FLOW_SUBAGENT_DISPATCH_CMD", msg)
        self.assertIn("v0.8.2", msg.lower() if "v0.8.2" in msg else msg)

    # ── F3 (codex round-1): CAPABILITY_FILE resolves via __file__ ────
    def test_capability_file_resolves_via_module_path(self):
        """Production callers chdir to user project root (which has only
        ``.flow/`` under it). The shim's CAPABILITY_FILE must still
        resolve to the framework's own ``claude/capabilities/defaults.json``
        — not a cwd-relative miss. We verify by chdir'ing to a tmp
        directory and confirming CAPABILITY_FILE.is_file() is True."""
        orig_cwd = os.getcwd()
        os.chdir(self.tmp)
        try:
            from flow_subagent_dispatch import CAPABILITY_FILE
            self.assertTrue(
                CAPABILITY_FILE.is_file(),
                f"CAPABILITY_FILE={CAPABILITY_FILE} must resolve via "
                f"__file__-based path, not cwd-relative; codex round-1 F3.",
            )
            # Sanity: the resolved path lives under REPO_ROOT.
            self.assertTrue(
                str(CAPABILITY_FILE).startswith(str(REPO_ROOT)),
                f"CAPABILITY_FILE must live under framework root "
                f"({REPO_ROOT}), got {CAPABILITY_FILE}",
            )
        finally:
            os.chdir(orig_cwd)

    # ── F4 (codex round-1): worktree path quoted via shlex.quote ─────
    def test_worktree_path_with_spaces_quoted(self):
        """R-class: a worktree path containing spaces must NOT cause
        argv splitting when interpolated into a shell=True template."""
        spaced_dir = self.tmp / "has space" / "wt"
        spaced_dir.mkdir(parents=True)
        marker = self.tmp / "spaces-out"
        # Template echoes worktree to a file; if not quoted, "has space"
        # would split into two argv tokens and the captured value would
        # be partial / wrong.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{worktree}} > {marker.as_posix()}"
        )
        ctx = _Ctx(spaced_dir, slug="demo", task_id="T0")
        from flow_subagent_dispatch import invoke
        invoke(ctx)
        self.assertTrue(marker.is_file())
        out = marker.read_text(encoding="utf-8").strip()
        # Single-line, full path preserved (echo collapses quoting but
        # passes the whole path as one arg — exactly the property we want).
        self.assertEqual(out, str(spaced_dir))

    def test_worktree_path_with_metachar_neutralized_by_quoting(self):
        """R-class: a worktree path containing shell metachars (``;``,
        ``$()``, ``&&``) must be quoted by ``shlex.quote`` so the
        metachar can't be parsed by the shell. We create a REAL
        directory whose name contains ``;`` — without quoting, the
        shell would split into two commands. With quoting, echo prints
        the literal path (including ``;``).

        Real ext4/btrfs filesystems do allow ``;`` in path components
        (only ``/`` and NUL are forbidden), so this is testable on a
        real disk; mkdir succeeds and the shim's ``cwd=worktree_path``
        chdir works.
        """
        evil_dir = self.tmp / "wt;evil$(whoami)&&true"
        evil_dir.mkdir()
        # Marker lives under self.tmp (NOT under evil_dir) so the path
        # we capture has no ambiguity with cwd.
        marker = self.tmp / "metachar-out"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{worktree}} > {marker.as_posix()}"
        )
        ctx = _Ctx(evil_dir, slug="demo", task_id="T0")
        from flow_subagent_dispatch import invoke
        invoke(ctx)
        # Verify echo received the entire path as a single argv element.
        # If quoting failed, the shell would have parsed ``;``/``&&`` as
        # command separators and ``$(whoami)`` as a sub-shell — output
        # would be partial / contain the username instead of the literal
        # ``$(whoami)``.
        out = marker.read_text(encoding="utf-8").strip()
        self.assertEqual(
            out, str(evil_dir),
            "shlex.quote(worktree) failed — metachars leaked into shell",
        )
        # Defensive sanity: literal metachars survived to echo's output.
        self.assertIn(";evil", out)
        self.assertIn("$(whoami)", out)
        self.assertIn("&&true", out)


if __name__ == "__main__":
    unittest.main()
