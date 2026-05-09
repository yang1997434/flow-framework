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
    quoted via ``shlex.quote`` before format() — exposed as the
    ``{worktree_quoted}`` placeholder
  * P2 (codex round-2): preserve ``{worktree}`` raw semantics so
    operator templates that already wrap in shell quotes (e.g.
    ``--worktree "{worktree}"``) keep working; opt-in safety via
    ``{worktree_quoted}``

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

    # ── F4 (codex round-1) + P2 (codex round-2): worktree placeholders ─
    # Round-1 silently swapped ``{worktree}`` -> shlex.quote(...), which
    # broke any template that already used outer shell quotes (the inner
    # single quotes were preserved literally). Round-2 P2 fix splits raw
    # vs quoted into two named placeholders so existing templates keep
    # working and the safe form is opt-in.
    def test_worktree_quoted_placeholder_is_shlex_quoted(self):
        """R-class: ``{worktree_quoted}`` must shlex.quote() the path so
        a template containing spaces does NOT cause argv splitting under
        shell=True."""
        spaced_dir = self.tmp / "has space" / "wt"
        spaced_dir.mkdir(parents=True)
        marker = self.tmp / "spaces-out"
        # Template echoes worktree_quoted to a file; if not quoted,
        # "has space" would split into two argv tokens and the captured
        # value would be partial / wrong.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{worktree_quoted}} > {marker.as_posix()}"
        )
        ctx = _Ctx(spaced_dir, slug="demo", task_id="T0")
        from flow_subagent_dispatch import invoke
        invoke(ctx)
        self.assertTrue(marker.is_file())
        out = marker.read_text(encoding="utf-8").strip()
        # Single-line, full path preserved (echo collapses quoting but
        # passes the whole path as one arg — exactly the property we want).
        self.assertEqual(out, str(spaced_dir))

    def test_worktree_quoted_neutralizes_metachars(self):
        """R-class: ``{worktree_quoted}`` must neutralize shell metachars
        (``;``, ``$()``, ``&&``). We create a REAL directory whose name
        contains ``;`` — without quoting, the shell would split into two
        commands. With quoting, echo prints the literal path.

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
            f"echo {{worktree_quoted}} > {marker.as_posix()}"
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

    # ── P2 (codex round-2): {worktree} raw semantics preserved ──────
    def test_worktree_placeholder_is_raw(self):
        """``{worktree}`` must interpolate the RAW worktree path
        (no shlex.quote() injected). Documented operator contract:
        backward-compatible — operators that already wrap in shell
        quotes keep working without double-quoting."""
        marker = self.tmp / "raw-out"
        # Path with NO spaces / metachars so an unquoted echo is safe;
        # this isolates the "is the placeholder raw?" assertion from
        # the quoting-required-for-spaces case.
        plain_dir = self.tmp / "plain_wt"
        plain_dir.mkdir()
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{worktree}} > {marker.as_posix()}"
        )
        ctx = _Ctx(plain_dir, slug="demo", task_id="T0")
        from flow_subagent_dispatch import invoke
        invoke(ctx)
        self.assertTrue(marker.is_file())
        out = marker.read_text(encoding="utf-8").strip()
        # The raw path is echoed verbatim; NO single quotes injected.
        self.assertEqual(out, str(plain_dir))
        self.assertNotIn(
            "'", out,
            "{worktree} must be RAW — shlex.quote() injection would "
            "leak literal single quotes (codex round-2 P2 regression)",
        )

    def test_template_using_outer_quotes_with_raw_placeholder_works(self):
        """P2 backward-compat: an operator template that wraps
        ``{worktree}`` in shell double-quotes (e.g.
        ``--worktree "{worktree}"``) must work with a path containing
        spaces — exactly because ``{worktree}`` is raw and the operator
        controls quoting. If round-1's shlex.quote() injection had
        survived, the inner single-quotes would have leaked into the
        output."""
        spaced_dir = self.tmp / "has space" / "outer-quoted"
        spaced_dir.mkdir(parents=True)
        marker = self.tmp / "outer-quoted-out"
        # Template uses outer double quotes around {worktree} — operator
        # idiom for "I know my path may have spaces, I'm handling
        # quoting myself".
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f'echo "{{worktree}}" > {marker.as_posix()}'
        )
        ctx = _Ctx(spaced_dir, slug="demo", task_id="T0")
        from flow_subagent_dispatch import invoke
        invoke(ctx)
        self.assertTrue(marker.is_file())
        out = marker.read_text(encoding="utf-8").strip()
        # Single line, the FULL path with the space preserved, NO
        # spurious single-quote chars (which would have appeared if
        # shlex.quote() had been silently injected into {worktree}).
        self.assertEqual(out, str(spaced_dir))
        self.assertNotIn("'", out)


# ── v0.8.3 P0.2: prompt_prefix file-based transport ──────────────────
#
# These tests pin the wire-up of the K-class sentinel prohibition (and
# any future ``prompt_prefix`` content) from
# ``build_implementer_prompt`` through the dispatch shim into the
# subagent prompt. The transport is a file under
# ``<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt``
# (NOT inside the worktree — see PRD AC §1 for the manifest_violation
# avoidance rationale). Operator templates reference the file via the
# new ``{prompt_prefix_file}`` placeholder; fail-closed when the
# placeholder is missing AND the prefix is non-empty.

class TestPromptPrefixWireUp(unittest.TestCase):
    """v0.8.3 P0.2 — prompt_prefix file-based transport."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="flow-p02-shim-"))
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        # Construct a fake repo_root + worktree layout matching the
        # production convention (`<repo_root>/.claude/worktrees/<id>/`).
        # The shim derives `repo_root = worktree_path.parents[2]`.
        self.repo_root = self.tmp / "repo"
        wt_parent = self.repo_root / ".claude" / "worktrees"
        wt_parent.mkdir(parents=True)
        self.worktree = wt_parent / "demo+t0+abc1234"
        self.worktree.mkdir()
        self.ctx = _Ctx(
            worktree_path=self.worktree, slug="demo", task_id="T0",
        )
        self._orig_env = os.environ.get("FLOW_SUBAGENT_DISPATCH_CMD")
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        sys.modules.pop("flow_subagent_dispatch", None)

    def tearDown(self):
        os.environ.pop("FLOW_SUBAGENT_DISPATCH_CMD", None)
        if self._orig_env is not None:
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = self._orig_env

    # ── AC §1: prefix file lands at the right path (NOT in worktree) ──
    def test_invoke_writes_prefix_file_at_repo_root_runtime(self):
        marker = self.tmp / "out"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {marker.as_posix()}"
        )
        from flow_subagent_dispatch import invoke
        invoke(
            self.ctx,
            prompt_prefix="HELLO_PREFIX_BODY",
            round_num=1,
        )
        expected = (
            self.repo_root / ".flow" / ".runtime"
            / "demo+T0+r1" / "dispatch_prefix.txt"
        )
        self.assertTrue(
            expected.is_file(),
            f"prefix file not at {expected}; tree contents: "
            f"{list(self.repo_root.rglob('*'))}",
        )
        # Critically NOT inside the worktree (would trigger
        # manifest_violation row 4).
        for p in self.worktree.rglob("*"):
            self.assertNotEqual(p.name, "dispatch_prefix.txt")
        # And `cat` round-trip wrote the same body.
        self.assertEqual(
            marker.read_text(encoding="utf-8"), "HELLO_PREFIX_BODY",
        )

    # ── AC §2: substitution actually replaces the placeholder ────────
    def test_invoke_substitutes_prefix_file_placeholder(self):
        marker = self.tmp / "subst-out"
        # The template uses a no-op shell command + echo of the placeholder
        # so we can inspect the substituted value without depending on
        # `cat` reading from disk (which would mask substitution bugs
        # behind the file actually existing).
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"echo {{prompt_prefix_file}} > {marker.as_posix()}"
        )
        from flow_subagent_dispatch import invoke
        invoke(self.ctx, prompt_prefix="X", round_num=1)
        out = marker.read_text(encoding="utf-8").strip()
        # Path is shlex.quote()-wrapped; on a path with no metachars
        # the wrapping is a no-op (the string equals the raw path).
        self.assertTrue(
            out.endswith("dispatch_prefix.txt"),
            f"substituted value did not end with dispatch_prefix.txt: {out!r}",
        )
        # And it's an absolute path containing /.flow/.runtime/.
        self.assertIn("/.flow/.runtime/", out)
        # And it points at the file we wrote.
        path = Path(out.strip("'\""))
        self.assertTrue(path.is_file())

    # ── AC §3: fail-closed on missing placeholder + non-empty prefix ──
    def test_invoke_raises_when_prefix_nonempty_template_lacks_placeholder(self):
        from flow_subagent_dispatch import invoke

        # Sub-assertion 1: literally absent.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true"
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        self.assertIn("prompt_prefix_file", str(cm.exception))

        # Sub-assertion 2: appears as substring in a comment-style
        # context (not a real format field). string.Formatter().parse()
        # does NOT see this as a field.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "echo hi  # placeholder once was {{prompt_prefix_file}}"
        )
        with self.assertRaises(RuntimeError):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

        # Sub-assertion 3: doubled braces escape — `{{prompt_prefix_file}}`
        # produces literal `{prompt_prefix_file}` after format(); it is
        # NOT a format field.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "echo {{prompt_prefix_file}}"
        )
        with self.assertRaises(RuntimeError):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

        # Sub-assertion 4: a template whose name resembles but is not
        # the placeholder must NOT satisfy the check.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "echo {prompt_prefix_filex}"
        )
        with self.assertRaises((RuntimeError, KeyError)):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

    # ── R2 P0: shell-comment fail-closed extension (codex caught) ────
    # Formatter().parse() sees `# {prompt_prefix_file}` as a real format
    # field — but the subprocess shell treats `#...` as a line comment
    # and never reads the placeholder. We add a regex pre-check that
    # rejects this exact form (single-line and multi-line "# on first
    # line" variants).
    def test_invoke_raises_on_shell_comment_placeholder(self):
        from flow_subagent_dispatch import invoke

        # Single line: `true # {prompt_prefix_file}` — `#` consumes the
        # rest of the line under shell parsing.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true # {prompt_prefix_file}"
        )
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        self.assertIn("comment", str(cm.exception).lower())

        # Multi-line: comment on first line, real command on second
        # (still a silent drop because the placeholder line never
        # executes).
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "cmd # {prompt_prefix_file}\nother --slug {slug}"
        )
        with self.assertRaises(RuntimeError):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

        # Tab before `#` should also count.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true\t#\t{prompt_prefix_file}"
        )
        with self.assertRaises(RuntimeError):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

    def test_invoke_known_bypass_string_literal_subprocess(self):
        """Operator-responsibility scope: a placeholder embedded inside
        a subprocess string literal (e.g. ``python -c 'x="{prompt_prefix_file}"'``)
        passes our fail-closed check. The Formatter sees a real field;
        the heuristic doesn't recognize a shell comment; the shell
        substitutes the path into the inner string. The subprocess
        never `cat`s it. This is a documented bypass — the pitfall +
        SKILL.md recommend the canonical ``$(cat {prompt_prefix_file})``
        form. We pin it here so future readers know the scope.
        """
        marker = self.tmp / "string-literal-out"
        # The python -c form keeps the placeholder as an inert string;
        # we just write SOMETHING to the marker so the subprocess returns 0.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"python3 -c 'x = \"{{prompt_prefix_file}}\"; "
            f"open(\"{marker.as_posix()}\", \"w\").write(\"ok\")'"
        )
        from flow_subagent_dispatch import invoke
        # MUST NOT raise — bypass is the operator's responsibility.
        invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        self.assertTrue(marker.is_file())

    # ── R3 P0: bare-form enforcement closes Formatter conv/spec bypass ──
    # Codex R2 caught: ``Formatter().parse()`` returns the same field
    # name for ``{prompt_prefix_file}``, ``{prompt_prefix_file!s}``, and
    # ``{prompt_prefix_file:>10}``. Our shell-comment scanner matches
    # the literal token only, so ``true # {prompt_prefix_file!s}``
    # would pass the field check, evade the comment scanner, and be
    # silently dropped at runtime. Fix: reject any non-bare form. There
    # is no legitimate reason to apply conversion or format-spec to a
    # quoted file path — collapsing to one canonical spelling keeps the
    # literal-token shell-comment scanner sound.
    def test_invoke_raises_on_format_conversion_form(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file!s}"
        from flow_subagent_dispatch import invoke
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        self.assertIn("bare", str(cm.exception).lower())
        # Also `!r` and `!a`.
        for conv in ("r", "a"):
            os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
                f"true {{prompt_prefix_file!{conv}}}"
            )
            with self.assertRaises(RuntimeError):
                invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

    def test_invoke_raises_on_format_spec_form(self):
        from flow_subagent_dispatch import invoke
        # Empty spec: `{prompt_prefix_file:}`.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file:}"
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        self.assertIn("bare", str(cm.exception).lower())
        # Non-empty spec: `{prompt_prefix_file:>10}`.
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true {prompt_prefix_file:>10}"
        )
        with self.assertRaises(RuntimeError):
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)

    def test_invoke_raises_on_shell_comment_with_conversion_form(self):
        """The combination bypass codex R2 caught:
        ``# {prompt_prefix_file!s}`` must be rejected. The bare-form
        gate fires BEFORE the shell-comment scanner reaches the
        (now-impossible) variant token, so the literal-only scanner
        stays sound."""
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true # {prompt_prefix_file!s}"
        )
        from flow_subagent_dispatch import invoke
        with self.assertRaises(RuntimeError) as cm:
            invoke(self.ctx, prompt_prefix="non-empty", round_num=1)
        # Bare-form gate fires first; structural fix beats heuristic.
        self.assertIn("bare", str(cm.exception).lower())

    # ── R2 P1#2: empty task_id with non-empty prefix fails closed ────
    def test_invoke_raises_on_empty_task_id_with_prefix(self):
        """Without a task_id the runtime dir collapses to
        ``<repo>/.flow/.runtime/<slug>++r1/`` (or worse, a fallback
        placeholder), which makes per-task evidence collide. When the
        caller asks for prefix transport without supplying task_id
        we MUST fail closed."""
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true {prompt_prefix_file}"
        )
        from flow_subagent_dispatch import invoke
        # ctx.task_id="" + no kwarg → empty task_id at runtime.
        ctx = _Ctx(self.worktree, slug="demo", task_id="")
        with self.assertRaises(RuntimeError) as cm:
            invoke(ctx, prompt_prefix="non-empty", round_num=1)
        self.assertIn("task_id", str(cm.exception))
        # Empty prefix path stays backwards-compatible (no task_id required).
        invoke(ctx, prompt_prefix="", round_num=1)

    # ── AC §4: unknown kwargs raise (kills silent-drop class) ────────
    def test_invoke_raises_on_unknown_kwargs(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true"
        from flow_subagent_dispatch import invoke
        with self.assertRaises(TypeError):
            invoke(self.ctx, future_kwarg_we_dont_know="oops")

    # ── AC §5: type-validate prompt_prefix before any side effect ────
    def test_invoke_raises_on_non_str_prefix(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            "true {prompt_prefix_file}"
        )
        from flow_subagent_dispatch import invoke
        for bad in (None, b"bytes-not-str", 42, ["list"], {"d": 1}):
            with self.assertRaises(TypeError):
                invoke(self.ctx, prompt_prefix=bad, round_num=1)
        # And no runtime dir was created (side-effect-free on type fail).
        runtime_dir = self.repo_root / ".flow" / ".runtime"
        self.assertFalse(runtime_dir.exists())

    # ── AC §6: backwards compat — empty prefix → no file, no fail ────
    def test_invoke_no_file_when_prefix_empty(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true"
        from flow_subagent_dispatch import invoke
        invoke(self.ctx, prompt_prefix="", round_num=1)
        runtime_dir = self.repo_root / ".flow" / ".runtime"
        self.assertFalse(
            runtime_dir.exists(),
            "empty prefix must not create the runtime dir",
        )
        # And default (no kwarg passed) is also empty.
        invoke(self.ctx)
        self.assertFalse(runtime_dir.exists())

    # ── AC §7: round_num discriminator is part of the path ──────────
    def test_invoke_round_discriminator_in_path(self):
        marker1 = self.tmp / "m1"
        marker2 = self.tmp / "m2"
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {marker1.as_posix()}"
        )
        from flow_subagent_dispatch import invoke
        invoke(self.ctx, prompt_prefix="round-one", round_num=1)
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = (
            f"cat {{prompt_prefix_file}} > {marker2.as_posix()}"
        )
        invoke(self.ctx, prompt_prefix="round-two", round_num=2)
        p1 = (self.repo_root / ".flow" / ".runtime"
              / "demo+T0+r1" / "dispatch_prefix.txt")
        p2 = (self.repo_root / ".flow" / ".runtime"
              / "demo+T0+r2" / "dispatch_prefix.txt")
        self.assertTrue(p1.is_file())
        self.assertTrue(p2.is_file())
        self.assertNotEqual(p1, p2)
        self.assertEqual(p1.read_text(encoding="utf-8"), "round-one")
        self.assertEqual(p2.read_text(encoding="utf-8"), "round-two")

    # ── AC §8: byte-for-byte fidelity (codex R2 AC delta #1) ─────────
    def test_invoke_prefix_file_byte_for_byte(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file}"
        from flow_subagent_dispatch import invoke
        # Multi-line, includes special chars + a Chinese char + quotes.
        body = (
            "line one\n"
            "line two with 中文 mixed in\n"
            "trailing-quote: 'single' and \"double\"\n"
            "no_trailing_newline_here"
        )
        invoke(self.ctx, prompt_prefix=body, round_num=1)
        path = (self.repo_root / ".flow" / ".runtime"
                / "demo+T0+r1" / "dispatch_prefix.txt")
        on_disk = path.read_bytes()
        # No BOM.
        self.assertFalse(on_disk.startswith(b"\xef\xbb\xbf"))
        # No CRLF — UTF-8 encoded LF only.
        self.assertNotIn(b"\r\n", on_disk)
        # Exact bytes equal UTF-8 of input (no trailing newline added).
        self.assertEqual(on_disk, body.encode("utf-8"))

    # ── AC §9: path-typo guard (codex R2 AC delta #2) ────────────────
    def test_invoke_path_contains_dot_runtime(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file}"
        from flow_subagent_dispatch import invoke
        invoke(self.ctx, prompt_prefix="x", round_num=1)
        # Walk runtime dir; assert path on disk lives under ``.flow/.runtime``.
        candidate = next(
            (
                p for p in self.repo_root.rglob("dispatch_prefix.txt")
                if p.is_file()
            ),
            None,
        )
        self.assertIsNotNone(candidate)
        self.assertIn("/.flow/.runtime/", str(candidate))

    # ── AC §10: layout assertion (codex R2 P1#1) ─────────────────────
    def test_invoke_raises_on_unexpected_worktree_layout(self):
        os.environ["FLOW_SUBAGENT_DISPATCH_CMD"] = "true {prompt_prefix_file}"
        from flow_subagent_dispatch import invoke

        # Case 1: <repo>/.claude/wt/<id>/ — `wt` instead of `worktrees`.
        bad_repo = self.tmp / "bad1"
        bad_wt = bad_repo / ".claude" / "wt" / "demo+t0+abc"
        bad_wt.mkdir(parents=True)
        ctx = _Ctx(bad_wt, slug="demo", task_id="T0")
        with self.assertRaises(RuntimeError) as cm:
            invoke(ctx, prompt_prefix="x", round_num=1)
        self.assertIn("layout", str(cm.exception).lower())

        # Case 2: <repo>/.claude/worktrees/verify/<id>/ — extra nesting.
        bad_repo2 = self.tmp / "bad2"
        bad_wt2 = bad_repo2 / ".claude" / "worktrees" / "verify" / "demo+t0+abc"
        bad_wt2.mkdir(parents=True)
        ctx2 = _Ctx(bad_wt2, slug="demo", task_id="T0")
        with self.assertRaises(RuntimeError):
            invoke(ctx2, prompt_prefix="x", round_num=1)


if __name__ == "__main__":
    unittest.main()
