"""v0.8.1 dispatch shim. Bridges orchestrator (Python) to subagent
invocation. Resolution order: env var -> capability binding ->
RuntimeError.

The shim is intentionally thin - the capability binding (Step 22.4)
points at the real Claude CLI invocation; tests substitute via the
``FLOW_SUBAGENT_DISPATCH_CMD`` env var (see test_subagent_dispatch_shim.py).

Wire-up (S-class defended):
    scripts/flow_orchestrator.py::_invoke_subagent_dispatch ->
        importlib.import_module("flow_subagent_dispatch") ->
            .invoke(ctx, subagent_env=subagent_env)

The orchestrator passes the parent PID env (T21 / S5) via
``subagent_env``; this shim merges it into the subprocess environment so
the spawned subagent inherits ``FLOW_AUTONOMY_PARENT_PID`` and the
nested-autonomy mechanical guard fires correctly.

R-class hardening: ``slug`` and ``task_id`` are interpolated into a
``shell=True`` command string. Even though the env var/capability
template is operator-controlled (so this is operator -> operator, not
user -> operator), shell metacharacters in identifiers would let a
malformed manifest leak into a compound command. We validate the
character set up front and raise ``ValueError`` rather than risk
``rm -rf $HOME``-style surprises if a future contract spec relaxes the
slug regex.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ENV_VAR = "FLOW_SUBAGENT_DISPATCH_CMD"
CAPABILITY_FILE = Path("claude/capabilities/defaults.json")

# R-class: only allow chars that cannot terminate a shell token. Mirror the
# slug regex used by flow_contract / flow_doctor (alnum + dot + underscore +
# plus + dash). Empty strings are rejected because an empty {task_id}
# substitution would silently collapse to ``-task `` style fragments which
# are semantically meaningless.
_IDENT_RE = re.compile(r"^[A-Za-z0-9._+\-]+$")


def _validate_ident(name: str, value: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(
            f"flow_subagent_dispatch: {name} must be a non-empty string "
            f"(got {value!r}); refusing to interpolate into shell command"
        )
    if not _IDENT_RE.match(value):
        raise ValueError(
            f"flow_subagent_dispatch: {name}={value!r} contains characters "
            f"outside the safe identifier set [A-Za-z0-9._+\\-]; refusing "
            f"to interpolate into a shell=True command (R-class guard)"
        )


def _resolve_cmd_template() -> str:
    """Resolve the dispatch command template.

    Order:
      1. ``FLOW_SUBAGENT_DISPATCH_CMD`` env var (highest priority - tests
         + manual override + ops staging).
      2. ``claude/capabilities/defaults.json`` ->
         ``capabilities.autonomy_orchestrator.dispatch_cmd``.
      3. ``RuntimeError`` - fail closed. The orchestrator's
         ``_invoke_subagent_dispatch`` already raises a similar error if
         the import fails; here we cover the "module imported but
         neither config source supplies a template" case.
    """
    cmd = os.environ.get(ENV_VAR)
    if cmd:
        return cmd
    if CAPABILITY_FILE.is_file():
        try:
            caps = json.loads(CAPABILITY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            # G/H-class: fall through to the explicit RuntimeError below
            # rather than silently treating a corrupt capability file as
            # "no config". Test-time isolation uses chdir-to-tmp so this
            # path is normally unreachable in unit tests.
            caps = None
        if isinstance(caps, dict):
            entry = (
                caps.get("capabilities", {}).get("autonomy_orchestrator")
                if isinstance(caps.get("capabilities"), dict)
                else None
            )
            # Backward compat: some downstream consumers also accept a
            # top-level entry (older v0.8.0 stub layout). We accept either.
            if not entry and isinstance(caps.get("autonomy_orchestrator"), dict):
                entry = caps["autonomy_orchestrator"]
            if isinstance(entry, dict):
                cmd = entry.get("dispatch_cmd")
                if isinstance(cmd, str) and cmd:
                    return cmd
    raise RuntimeError(
        f"subagent dispatch not configured: set {ENV_VAR} env var "
        f"OR populate claude/capabilities/defaults.json::"
        f"capabilities.autonomy_orchestrator.dispatch_cmd. "
        f"v0.8.1 fails closed rather than silently skipping dispatch."
    )


def invoke(ctx: Any, *, subagent_env: dict | None = None, **_kw: Any) -> None:
    """Called by orchestrator's ``_invoke_subagent_dispatch``.

    ``ctx`` is duck-typed: ``ctx.worktree_path`` (Path or str),
    ``ctx.slug``, optional ``ctx.task_id``. The resolved command template
    receives those three as ``str.format()`` placeholders.

    The subagent runs as a subprocess so its crash doesn't take down the
    orchestrator. Nonzero return code is a *soft* signal - the orchestrator's
    manifest verify (T11) and gate runner (T12) catch the empty/broken
    diff and route appropriately. We only WARN here.
    """
    template = _resolve_cmd_template()

    slug = getattr(ctx, "slug", "")
    task_id = getattr(ctx, "task_id", "")
    worktree_path = str(getattr(ctx, "worktree_path", ""))

    _validate_ident("slug", slug)
    # task_id is empty in some test scaffolding - allow that, but if
    # non-empty validate the same way as slug.
    if task_id:
        _validate_ident("task_id", task_id)

    cmd_str = template.format(
        worktree=worktree_path,
        slug=slug,
        task_id=task_id,
    )

    # Merge in the orchestrator-supplied env (carries
    # FLOW_AUTONOMY_PARENT_PID per T21/S5). subagent_env=None falls back
    # to bare os.environ - tests that don't care about the parent-pid
    # guard stay simple.
    env = dict(os.environ)
    if subagent_env:
        env.update(subagent_env)

    proc = subprocess.run(
        cmd_str,
        shell=True,
        cwd=worktree_path or None,
        env=env,
        capture_output=False,
        text=True,
    )
    if proc.returncode != 0:
        print(
            f"WARN: subagent dispatch returned {proc.returncode} for "
            f"{slug}/{task_id or '?'} - orchestrator will derive facts "
            f"from worktree state and route per gates.",
            file=sys.stderr,
        )
