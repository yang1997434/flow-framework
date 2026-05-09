"""v0.8.1 dispatch shim. Bridges orchestrator (Python) to subagent
invocation. Resolution order: env var -> capability binding ->
RuntimeError.

The shim is intentionally thin - the capability binding (Step 22.4)
points at the real Claude CLI invocation; tests substitute via the
``FLOW_SUBAGENT_DISPATCH_CMD`` env var (see test_subagent_dispatch_shim.py).

Wire-up (S-class defended):
    scripts/flow_orchestrator.py::_invoke_subagent_dispatch ->
        importlib.import_module("flow_subagent_dispatch") ->
            .invoke(ctx, subagent_env=subagent_env, task_id=manifest.id,
                         prompt_prefix=..., round_num=...)

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

Template placeholders (codex round-2 P2 — preserve quoted templates):
    {slug}             — task slug, validated [A-Za-z0-9._+\\-]
    {task_id}          — task id, validated [A-Za-z0-9._+\\-]
    {worktree}         — RAW worktree path. Backward-compatible with
                         operator templates that already wrap the
                         placeholder in shell quotes (e.g.
                         ``--worktree "{worktree}"``). Use this when
                         your template controls quoting itself.
    {worktree_quoted}  — shlex.quote()-wrapped worktree path. RECOMMENDED
                         default for shell=True templates without their
                         own quoting (e.g.
                         ``--worktree {worktree_quoted}``).
    {prompt_prefix_file}
                       — (v0.8.3 P0.2) absolute path to a UTF-8 file
                         containing the K-class sentinel prohibition
                         (and any future ``prompt_prefix`` content the
                         orchestrator wants the subagent to read). The
                         path is ALREADY ``shlex.quote()``-wrapped — do
                         NOT add additional shell quoting in your
                         template. The file lives at
                         ``<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt``
                         (outside the worktree, gitignored at
                         ``.gitignore:21``). The operator template MUST
                         actually pipe the file body into the prompt
                         — merely mentioning the placeholder is
                         insufficient. Recommended idiom (the minimum
                         that wires the K-class guard through to the
                         subagent prompt)::

                             claude -p "$(cat {prompt_prefix_file})

                             flow:flow-phase2-execute --slug {slug} --task {task_id} --worktree {worktree_quoted}"

                         Fail-closed: if a non-empty ``prompt_prefix``
                         is supplied AND the template does not
                         reference ``{prompt_prefix_file}`` as a real
                         format field (commented-out, escaped via
                         ``{{...}}``, or appearing inside a string
                         literal does NOT count), ``invoke()`` raises
                         ``RuntimeError`` BEFORE running the subagent.

    History: round-1 silently swapped {worktree} -> shlex.quote(...),
    which broke any template that already used outer quotes — the inner
    single quotes injected by shlex.quote() were preserved literally and
    the subagent received ``'/path with space'`` (with quote chars). The
    fix splits raw vs quoted into two named placeholders so existing
    templates keep working and the safe form is opt-in.

    v0.8.3 P0.2 history: ``invoke()`` previously accepted ``**_kw`` and
    silently dropped any kwarg the template did not consume. The
    orchestrator's K-class sentinel prohibition was being passed via
    ``prompt_prefix=`` since v0.8.2 T4 / v0.8.3 P0.1 but never reached
    the dispatched subagent. The fix removes ``**_kw`` so unknown
    kwargs raise ``TypeError`` (kills the silent-drop class) AND adds
    the ``{prompt_prefix_file}`` placeholder + fail-closed assertion so
    operator templates that forget to wire the prefix file get a loud
    error rather than a silently-skipped guard.
"""
from __future__ import annotations

import json
import os
import re
import shlex
import string
import subprocess
import sys
from pathlib import Path
from typing import Any


ENV_VAR = "FLOW_SUBAGENT_DISPATCH_CMD"
# G/S-class (codex round-1 F3): resolve relative to THIS module's install
# location, NOT relative to cwd. Production callers (`flow orchestrator
# --auto-execute`) chdir into a user project root that has only ``.flow/``
# under it — the framework's own ``claude/capabilities/defaults.json``
# lives next to ``scripts/`` in the framework install. Using cwd-relative
# resolution made the capability fallback unreachable in production and
# turned every dispatch into the explicit RuntimeError, even when the
# (now-removed) dispatch_cmd default was in place.
CAPABILITY_FILE = (
    Path(__file__).resolve().parent.parent
    / "claude" / "capabilities" / "defaults.json"
)

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

    Template placeholders: ``{slug}``, ``{task_id}`` (validated identifiers),
    ``{worktree}`` / ``{worktree_quoted}`` (raw vs ``shlex.quote()``-wrapped),
    and ``{prompt_prefix_file}`` (v0.8.3 P0.2 — absolute path to a
    ``shlex.quote()``-wrapped UTF-8 file under
    ``<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/``). The
    operator template MUST cat the file body into the actual prompt
    sent to the subagent — merely mentioning the placeholder does NOT
    deliver the K-class sentinel prohibition. Recommended template:

        claude -p "$(cat {prompt_prefix_file})

        flow:flow-phase2-execute --slug {slug} --task {task_id} --worktree {worktree_quoted}"

    The placeholder value is already shlex-wrapped so do NOT add
    additional shell quoting around ``{prompt_prefix_file}`` in your
    template.
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
        f"subagent dispatch not configured: export {ENV_VAR} env var "
        f"with the subagent invocation template. Template placeholders: "
        f"{{slug}}, {{task_id}} (validated alnum+._+-); {{worktree}} "
        f"(raw path, backward-compatible — wrap in your own shell quotes "
        f"if needed); {{worktree_quoted}} (shlex.quote()-wrapped, "
        f"RECOMMENDED for shell=True); and (v0.8.3 P0.2) "
        f"{{prompt_prefix_file}} (absolute path to a UTF-8 file holding "
        f"the K-class sentinel prohibition + reviewer feedback prefix; "
        f"the path is already shlex.quote()-wrapped, do NOT add shell "
        f"quoting). Recommended template (the K-class guard ONLY reaches "
        f"the subagent if you actually `cat` the file body into the "
        f"prompt — mere mention does not wire it through):\n"
        f"  claude -p \"$(cat {{prompt_prefix_file}})\n\n"
        f"  flow:flow-phase2-execute --slug {{slug}} --task {{task_id}} "
        f"--worktree {{worktree_quoted}}\"\n"
        f"v0.8.1 ships the dispatch shim infrastructure but the "
        f"capability default (autonomy_orchestrator.dispatch_cmd) is "
        f"intentionally absent — the SKILL handle is NOT a shell "
        f"command. Production wire-up of the Claude CLI invocation "
        f"template is v0.8.2 scope. v0.8.1 fails closed rather than "
        f"silently skipping dispatch."
    )


def _template_field_names(template: str) -> set[str]:
    """Return the set of real format-field names in ``template``.

    Uses ``string.Formatter().parse()`` so we are immune to substring
    false positives at the Python format layer (codex P0#3):
    commented-out tokens at the Python source level and doubled-brace
    escapes ``{{...}}`` do NOT count as format fields. Only positions
    that ``str.format()`` would actually substitute are reported.

    Caveat (codex R2 — scope-honest): a placeholder embedded inside a
    subprocess string literal (e.g. ``python -c 'x="{prompt_prefix_file}"'``)
    IS a real format field — the parser treats the inner quotes as
    literal characters, not as a Python string-literal context. The
    shim's ``invoke()`` substitutes a quoted path into that position,
    but the inner subprocess never ``cat``s the file, so the K-class
    guard is silently dropped at runtime. That mode is documented as a
    known operator-responsibility bypass in SKILL.md transport
    section + the ``dispatch-shim-silent-kw-drop`` pitfall — operators
    MUST follow the canonical ``$(cat {prompt_prefix_file})`` form.
    The fail-closed contract here covers (a) missing placeholder,
    (b) shell-comment placement (literal-token scanner downstream),
    and (c) non-bare format variants (R3 P0 gate); it does NOT
    promise an exhaustive shell-mistake catcher.
    """
    return {
        field
        for _literal, field, _spec, _conv in string.Formatter().parse(template)
        if field is not None and field != ""
    }


# ── R2 codex feedback: shell-comment fail-closed extension ───────────
# Formatter().parse() correctly catches Python-side escapes
# (``{{...}}``) but NOT shell-side ``# comment`` placement. Even when
# ``{prompt_prefix_file}`` is a real format field, if it sits AFTER an
# unquoted ``#`` on a shell line the subprocess will treat the ``#``
# as a comment and never read the value — silent-drop reborn at the
# shell layer. We add a per-line regex pre-check that rejects this
# exact form.
#
# Scope-honest: this catches the common foot-gun, NOT every shell-level
# misuse. In particular ``python -c 'x="{prompt_prefix_file}"'`` (the
# placeholder embedded inside an inner subprocess string literal)
# passes both checks — the field is real, no comment marker — but the
# subprocess never `cat`s the value. That mode is documented as a
# known operator-responsibility bypass in SKILL.md transport section
# and the ``dispatch-shim-silent-kw-drop`` pitfall.
_PROMPT_PREFIX_FILE_TOKEN = "{prompt_prefix_file}"
# Match `#` preceded by start-of-line or whitespace, with no quote
# characters between it and end-of-line-prefix. Crude but practical:
# operators who legitimately want a `#` inside quoted text BEFORE the
# placeholder can split the line.
_SHELL_COMMENT_BEFORE_TOKEN_RE = re.compile(r"(?:^|\s)#[^\"']*$")

# R3 P0 layer-2: catches ``{prompt_prefix_file<X>}`` for any non-bare
# ``<X>`` (colon, ``!``, etc.). ``Formatter().parse()`` cannot
# distinguish ``{x:}`` from ``{x}`` — both normalise to
# ``format_spec=''`` — so we scan the raw template for a non-`}`
# character immediately after the field name. Catches `{prompt_prefix_file:}`,
# `{prompt_prefix_file!s}`, `{prompt_prefix_file:>10}`, etc. The
# Formatter().parse() check still runs first to give a more specific
# error message for conversion / non-empty spec cases.
_NON_BARE_PROMPT_PREFIX_FILE_RE = re.compile(
    r"\{prompt_prefix_file[^}]"
)


def _template_uses_placeholder_in_executable_position(template: str) -> bool:
    """Return True iff every occurrence of ``{prompt_prefix_file}`` in
    ``template`` lies in a shell-executable position (NOT after an
    unquoted ``#`` comment marker on the same line).

    Returns False (i.e. fail-closed) if ANY occurrence sits behind a
    shell comment — even one bad placement means the K-class guard is
    silently dropped on that path.

    Note: this scanner matches the LITERAL token ``{prompt_prefix_file}``
    only. Variant Formatter forms like ``{prompt_prefix_file!s}`` and
    ``{prompt_prefix_file:>10}`` would evade it — that's why we also
    require the bare form via ``_assert_prompt_prefix_file_is_bare``
    (codex R3 P0). Together: bare-form gate collapses the variant
    family to one canonical spelling, so this literal scanner stays
    sound.
    """
    for line in template.splitlines():
        pos = line.find(_PROMPT_PREFIX_FILE_TOKEN)
        if pos == -1:
            continue
        prefix = line[:pos]
        if _SHELL_COMMENT_BEFORE_TOKEN_RE.search(prefix):
            return False
    return True


def _assert_prompt_prefix_file_is_bare(template: str) -> None:
    """Reject any non-bare ``{prompt_prefix_file}`` form.

    Codex R3 P0: ``string.Formatter().parse()`` returns the same
    ``field_name`` for ``{prompt_prefix_file}``, ``{prompt_prefix_file!s}``,
    and ``{prompt_prefix_file:>10}``. Our literal-token shell-comment
    scanner matches the bare form only, so an operator template like
    ``true # {prompt_prefix_file!s}`` would pass the field-set check,
    evade the comment scanner, and be silently dropped at runtime by
    the shell.

    There is no legitimate reason to apply ``!conversion`` or
    ``:format_spec`` to a quoted file path. We collapse the variant
    family to one canonical spelling so downstream literal scanners
    stay sound.

    Implementation: ``Formatter().parse()`` catches conversion +
    non-empty format_spec, but ``{x:}`` (empty spec) is
    indistinguishable from ``{x}`` at parse() — Python normalises
    both to ``format_spec=''``. We additionally regex-scan the raw
    template for ``{prompt_prefix_file<anything-but-bare-close>}`` so
    the empty-spec form (``{prompt_prefix_file:}``) is also rejected.

    Raises ``RuntimeError`` on the first non-bare occurrence with an
    actionable error message.
    """
    # Layer 1: Formatter-detectable variants (conversion + non-empty
    # spec).
    for _literal, field_name, format_spec, conversion in string.Formatter().parse(template):
        if field_name != "prompt_prefix_file":
            continue
        if format_spec or conversion is not None:
            raise RuntimeError(
                "flow_subagent_dispatch.invoke: '{prompt_prefix_file}' "
                "must appear in BARE form — no '!conversion' or "
                "':format_spec' permitted "
                f"(got format_spec={format_spec!r}, "
                f"conversion={conversion!r}). Use exactly "
                "'{prompt_prefix_file}' so fail-closed checks "
                "(shell-comment scanner) can match the literal token. "
                "Variant forms like '{prompt_prefix_file!s}' or "
                "'{prompt_prefix_file:>10}' would evade the literal-"
                "token scanner and re-open the silent-drop class."
            )

    # Layer 2: empty-spec form ``{prompt_prefix_file:}``.
    # ``Formatter().parse()`` collapses ``{x:}`` -> ``format_spec=''``
    # so it's indistinguishable from ``{x}`` at parse-time. Detect via
    # raw-template regex: literal ``{prompt_prefix_file`` followed by
    # any character that is NOT ``}`` (i.e. a colon, conversion mark
    # ``!``, or anything else) before the closing brace.
    if _NON_BARE_PROMPT_PREFIX_FILE_RE.search(template):
        raise RuntimeError(
            "flow_subagent_dispatch.invoke: '{prompt_prefix_file}' "
            "must appear in BARE form — empty format-spec "
            "'{prompt_prefix_file:}' is also rejected because it "
            "evades the literal-token shell-comment scanner. Use "
            "exactly '{prompt_prefix_file}' (no trailing ':' or '!')."
        )


def _derive_repo_root(worktree_path: Path) -> Path:
    """Reverse-derive ``repo_root`` from the worktree path under the
    convention ``<repo_root>/.claude/worktrees/<id>/``.

    Layout assertion (codex R2 P1#1): if the worktree does not match
    that exact layout (e.g. ``<repo>/.claude/wt/<id>``,
    ``<repo>/.claude/worktrees/verify/<id>``, or anything else), raise
    ``RuntimeError`` with an actionable message. This prevents future
    misuse from silently routing the prefix file to the wrong
    repo_root.
    """
    parent = worktree_path.parent
    grandparent = parent.parent
    if parent.name != "worktrees" or grandparent.name != ".claude":
        raise RuntimeError(
            "flow_subagent_dispatch.invoke: worktree_path does not match "
            "the expected layout '<repo_root>/.claude/worktrees/<id>/'; "
            f"got {worktree_path!s}. The dispatch shim derives repo_root "
            "by reversing this convention to write the prompt_prefix file "
            "under <repo_root>/.flow/.runtime/. If a new worktree layout "
            "is required (verify worktrees, etc.), update this assertion "
            "AND the repo_root derivation in lockstep."
        )
    return grandparent.parent


def invoke(
    ctx: Any,
    *,
    subagent_env: dict | None = None,
    task_id: str | None = None,
    prompt_prefix: str = "",
    round_num: int = 1,
) -> None:
    """Called by orchestrator's ``_invoke_subagent_dispatch``.

    ``ctx`` is duck-typed: ``ctx.worktree_path`` (Path or str) +
    ``ctx.slug``. ``task_id`` is taken from the explicit kwarg first
    (orchestrator passes ``manifest.id``); only as a last-resort fallback
    do we look at ``ctx.task_id`` — production WorktreeContext does NOT
    define ``task_id`` (it's only on the per-iteration manifest), so the
    kwarg path is the canonical wiring. Falling back to ``getattr`` keeps
    older test fixtures (which set ``ctx.task_id`` directly) working
    while production uses the explicit kwarg path.

    ``prompt_prefix`` (v0.8.3 P0.2): when non-empty, written to
    ``<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt``
    and surfaced to the operator template via the
    ``{prompt_prefix_file}`` placeholder (already ``shlex.quote()``-
    wrapped). The operator is responsible for piping the file body into
    the actual prompt sent to the subagent (e.g. via ``$(cat ...)``).
    Fail-closed: if ``prompt_prefix`` is non-empty AND the template
    does not reference ``{prompt_prefix_file}`` as a real format field,
    ``RuntimeError`` is raised BEFORE any subprocess runs (codex
    P0#3 — substring matching would mis-pass commented-out / escaped
    occurrences).

    Note: ``invoke`` no longer accepts ``**kwargs`` — codex P0#2: the
    catch-all kwarg trap was the silent-drop class that made
    ``prompt_prefix`` unreachable for two releases. Any unknown kwarg
    now raises ``TypeError`` so a future "added a new parameter but
    forgot to wire it" mistake fails loud.

    The subagent runs as a subprocess so its crash doesn't take down the
    orchestrator. Nonzero return code is a *soft* signal - the orchestrator's
    manifest verify (T11) and gate runner (T12) catch the empty/broken
    diff and route appropriately. We only WARN here.
    """
    # ── Type validate prompt_prefix BEFORE any side effect (codex P0#4) ─
    # Bytes / None / int / list / dict all fail here. We use isinstance
    # rather than `type(...) is str` so a future `_LiteralStr` subclass
    # would still pass, but bool would not (bool is_a int → already
    # rejected by the str check).
    if not isinstance(prompt_prefix, str):
        raise TypeError(
            f"flow_subagent_dispatch.invoke: prompt_prefix must be str; "
            f"got {type(prompt_prefix).__name__} ({prompt_prefix!r})"
        )
    if not isinstance(round_num, int) or isinstance(round_num, bool) or round_num < 1:
        raise ValueError(
            f"flow_subagent_dispatch.invoke: round_num must be a positive "
            f"int (>=1); got {round_num!r}"
        )

    template = _resolve_cmd_template()

    slug = getattr(ctx, "slug", "")
    # F1 wiring: orchestrator-supplied task_id is authoritative; fall back
    # to ctx attribute only for backward-compat with existing tests.
    if task_id is None:
        task_id = getattr(ctx, "task_id", "")
    worktree_path_str = str(getattr(ctx, "worktree_path", ""))

    _validate_ident("slug", slug)
    # task_id is empty in some test scaffolding - allow that, but if
    # non-empty validate the same way as slug.
    if task_id:
        _validate_ident("task_id", task_id)

    # ── R3 P0: bare-form enforcement (must run BEFORE other gates) ──
    # Reject `{prompt_prefix_file!s}` / `{prompt_prefix_file:>10}` so
    # the literal-token shell-comment scanner downstream stays sound
    # (variant forms would evade it). Run only when prompt_prefix is
    # non-empty: empty-prefix path may legitimately have a bare
    # placeholder that substitutes to "" (no enforcement needed
    # because no prompt_prefix file is even written).
    if prompt_prefix:
        _assert_prompt_prefix_file_is_bare(template)

    # ── Fail-closed placeholder check (codex P0#3) ──────────────────
    # Use string.Formatter().parse() so Python-side false positives —
    # commented-out tokens at the source level + doubled-brace escapes
    # `{{...}}` (which parse to literal `{...}` with field name None)
    # — do NOT register as real format fields. Variant forms like
    # `{prompt_prefix_file!s}` / `{prompt_prefix_file:>10}` are caught
    # earlier by `_assert_prompt_prefix_file_is_bare` (R3 P0 gate),
    # so by this point only the bare token can be present.
    # Scope-honest caveat: a placeholder embedded inside a subprocess
    # string literal (e.g. `python -c 'x="{prompt_prefix_file}"'`) IS
    # a real format field — the inner subprocess simply never `cat`s
    # it. Documented operator-responsibility bypass — see SKILL.md
    # transport section + the `dispatch-shim-silent-kw-drop` pitfall.
    fields = _template_field_names(template)
    if prompt_prefix and "prompt_prefix_file" not in fields:
        raise RuntimeError(
            "flow_subagent_dispatch.invoke: prompt_prefix is non-empty "
            "but the dispatch template does not reference "
            "{prompt_prefix_file} as a real format field. The K-class "
            "sentinel prohibition (and any reviewer feedback) would be "
            "silently dropped. Update FLOW_SUBAGENT_DISPATCH_CMD (or "
            "claude/capabilities/defaults.json::autonomy_orchestrator."
            "dispatch_cmd) to include {prompt_prefix_file} AND actually "
            "cat its body into the prompt, e.g.:\n"
            "  claude -p \"$(cat {prompt_prefix_file})\n\n"
            "  flow:flow-phase2-execute --slug {slug} --task {task_id} "
            "--worktree {worktree_quoted}\"\n"
            "Note: the placeholder value is already shlex.quote()-"
            "wrapped — do NOT add shell quoting around it."
        )

    # ── R2 codex: shell-comment placement gate ───────────────────────
    # Even if the field is real, a `# {prompt_prefix_file}` placement
    # makes the shell skip the line — silent-drop reborn at the shell
    # layer. Reject. Scope-honest: does NOT cover every shell-misuse
    # (string-literal-inside-subprocess is a known operator-bypass —
    # see SKILL.md transport section + dispatch-shim-silent-kw-drop
    # pitfall).
    if prompt_prefix and not _template_uses_placeholder_in_executable_position(
        template
    ):
        raise RuntimeError(
            "flow_subagent_dispatch.invoke: '{prompt_prefix_file}' "
            "appears to be inside a shell comment ('# ...') in the "
            "dispatch template. The shell will treat the rest of that "
            "line as a comment and never read the placeholder, silently "
            "dropping the K-class sentinel prohibition. Move the "
            "placeholder to an executable position, e.g.:\n"
            "  claude -p \"$(cat {prompt_prefix_file})\n\n"
            "  flow:flow-phase2-execute --slug {slug} --task {task_id} "
            "--worktree {worktree_quoted}\"\n"
            "Note: this fail-closed check covers the common shell-comment "
            "foot-gun. Embedding the placeholder inside an inner "
            "subprocess string literal (e.g. `python -c 'x=\"{prompt_prefix_file}\"'`) "
            "is an operator-responsibility bypass — see the transport "
            "section of claude/skills/flow/flow-phase2-execute/SKILL.md."
        )

    # ── R2 codex P1#2: task_id required when prefix is non-empty ────
    # Path discriminator collapses to <slug>++r1 without a task_id;
    # multiple tasks of the same slug would collide on the runtime
    # file. Production passes manifest.id so this is benign today,
    # but a future caller forgetting task_id while passing a prefix
    # would corrupt evidence silently.
    if prompt_prefix and not task_id:
        raise RuntimeError(
            "flow_subagent_dispatch.invoke: prompt_prefix supplied but "
            "task_id is missing — required for runtime dir uniqueness "
            "(<repo>/.flow/.runtime/<slug>+<task_id>+r<round>/). Pass "
            "task_id=manifest.id explicitly, or set ctx.task_id."
        )

    # ── Materialise the prompt_prefix file (only if non-empty) ──────
    prompt_prefix_file: Path | None = None
    if prompt_prefix:
        # Layout assertion + repo_root derivation. Skip when prompt_prefix
        # is empty so legacy fixtures with non-standard worktree layouts
        # (test fixtures using bare tmp dirs) keep working — the only
        # path that REQUIRES the layout is the one that writes the
        # prefix file. (G-class: targeted enforcement, not a global
        # invariant that breaks unrelated callers.)
        worktree_path_obj = Path(worktree_path_str)
        repo_root = _derive_repo_root(worktree_path_obj)
        # task_id is guaranteed non-empty here: the R2 P1#2 fail-closed
        # gate above raises RuntimeError if prompt_prefix is non-empty
        # and task_id is empty. NO fallback placeholder — collision
        # safety of the runtime dir relies on task_id discrimination.
        runtime_dir = (
            repo_root / ".flow" / ".runtime"
            / f"{slug}+{task_id}+r{round_num}"
        )
        runtime_dir.mkdir(parents=True, exist_ok=True)
        prompt_prefix_file = runtime_dir / "dispatch_prefix.txt"
        # UTF-8 byte-for-byte (codex R2 AC delta #1): NO BOM, NO CRLF
        # injection, NO trailing newline added. write_bytes ensures we
        # don't go through the platform line-ending translation that
        # ``write_text(..., newline=None)`` could apply.
        prompt_prefix_file.write_bytes(prompt_prefix.encode("utf-8"))

    # R-class hardening (codex round-1 F4 + round-2 P2): worktree path
    # is NOT covered by ``_validate_ident`` (path may legitimately
    # contain ``/`` and platform-specific chars). Two named placeholders
    # to preserve operator-supplied quoting semantics:
    #   {worktree}        — raw path; backward-compatible with templates
    #                       that already wrap in shell quotes
    #                       (e.g. ``--worktree "{worktree}"``).
    #   {worktree_quoted} — shlex.quote()-wrapped; RECOMMENDED default
    #                       for unquoted templates running under
    #                       shell=True.
    # Round-1 unconditionally substituted shlex.quote(...) into
    # {worktree}, which broke quoted templates: outer quotes preserved
    # the inner single-quotes literally and the subagent received a
    # bogus path with quote chars.
    #
    # v0.8.3 P0.2: {prompt_prefix_file} carries the absolute path to the
    # written prefix file (already shlex.quote()-wrapped so operators
    # don't double-quote). When prompt_prefix is empty we substitute an
    # empty string — but the fail-closed check above guarantees that
    # any template demanding the placeholder has a non-empty prefix to
    # accompany it (the inverse of the P0#3 invariant: if the template
    # references the placeholder but caller supplies "", the literal
    # empty path passed through is the operator's choice — they
    # explicitly opted into the format field).
    prompt_prefix_file_str = (
        shlex.quote(str(prompt_prefix_file))
        if prompt_prefix_file is not None
        else ""
    )
    cmd_str = template.format(
        slug=slug,
        task_id=task_id,
        worktree=worktree_path_str,
        worktree_quoted=shlex.quote(worktree_path_str),
        prompt_prefix_file=prompt_prefix_file_str,
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
        cwd=worktree_path_str or None,
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
