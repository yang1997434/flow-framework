---
name: dispatch-shim-silent-kw-drop
date: 2026-05-08
project: flow-framework
severity: high
status: active
trigger_paths:
  - scripts/flow_subagent_dispatch.py
  - scripts/flow_orchestrator.py
last_verified: 2026-05-08
---

# dispatch-shim-silent-kw-drop

## Symptom（看到什么）

A wire is "obviously" connected upstream (orchestrator builds the
prefix, passes it as a named kwarg) but the downstream consumer
(dispatched subagent prompt, downstream subprocess, downstream API
call) never sees it. Tests at the top of the chain pass (the
orchestrator function returned without raising) AND tests at the
bottom of the chain pass (the subagent ran), but no test exercises
the full chain.

Observed in v0.8.3 P0.2:

- `build_implementer_prompt` (v0.8.2 T4) prepends K-class sentinel
  prohibition to every first-pass code dispatch.
- `_phase2_dispatch::_prod_impl` calls
  `dispatch_fn(..., prompt_prefix=prefix)`.
- `_invoke_subagent_dispatch(ctx, **kw)` forwards kwargs.
- `flow_subagent_dispatch.invoke(ctx, *, ..., **_kw)` accepts kw but
  never reads `prompt_prefix`; the cmd template only knows
  `{slug,task_id,worktree,worktree_quoted}`.
- → `prompt_prefix` falls into `**_kw` and is **silently dropped**.
- Round 1 (`auto_dispatch_task`) doesn't even pass it.
- Net: the K-class guard is dead code in production for two releases.

## Root cause（为什么）

Two failure modes compounded:

1. **`**_kw` catch-all on the boundary function**. `invoke(...,
   **_kw)` silently accepts any unknown kwarg. The intent at write
   time was "future-proofing" — let the orchestrator pass new kwargs
   without breaking the shim. Effect was the opposite: any kwarg the
   shim doesn't explicitly handle is invisible to the downstream
   subprocess, with zero feedback to the caller.
2. **No fail-closed assertion** that the operator template actually
   consumes the new field. Even if a kwarg is named explicitly in
   the signature, an `str.format()` template can omit it and
   `template.format(prompt_prefix_file="...")` silently produces a
   string without the value. The KeyError direction is
   backward-compatible but the missing-from-template direction is
   silent.

## Fix（如何修）

- **REMOVE `**_kw`** from `invoke`. Replace with explicit `prompt_prefix:
  str = ""` + `round_num: int = 1`. Unknown kwargs now raise `TypeError`.
- **Validate the placeholder is a real format field** before using
  the value. Do NOT use substring matching (catches commented-out
  tokens, `{{...}}` escapes, string literals). Use
  `string.Formatter().parse()` to enumerate real fields:
  ```python
  fields = {f for _, f, _, _ in string.Formatter().parse(template) if f}
  if "prompt_prefix_file" not in fields:
      raise RuntimeError(...)
  ```
- **Add a shell-comment heuristic** as a second gate (codex R2 fix):
  `Formatter().parse()` passes when `{prompt_prefix_file}` is a real
  field but the field can still sit AFTER an unquoted `#` on a shell
  line — the subprocess treats the rest of the line as a comment and
  never reads the value. Per-line regex `(?:^|\s)#[^\"\']*$` rejects
  this exact form before substitution.
- **Enforce bare placeholder form** as a structural pre-gate (codex
  R3 P0 fix): `Formatter().parse()` returns the same `field_name`
  for `{prompt_prefix_file}`, `{prompt_prefix_file!s}`, and
  `{prompt_prefix_file:>10}` — all three pass the field-set check.
  Worse, the parser cannot even distinguish `{x:}` from `{x}` (both
  collapse to `format_spec=''`). The shell-comment scanner matches
  the literal token `{prompt_prefix_file}` only, so a template like
  `true # {prompt_prefix_file!s}` would: pass the field check, evade
  the comment scanner, get shell-commented at runtime → silent drop
  reborn. Fix two-layered: (a) `Formatter().parse()` walk enforces
  `format_spec == ''` and `conversion is None`; (b) raw-template
  regex `{prompt_prefix_file[^}]` catches the empty-spec `{x:}`
  variant that parse() can't see. Collapses the variant family to
  one canonical spelling so the literal-token scanner downstream
  stays sound.
- **Require `task_id` when prompt_prefix is non-empty**: without
  `task_id` the runtime dir collapses to `<slug>++r1/`, causing
  per-task evidence collisions. Fail closed: do NOT silently fall
  back to a placeholder identifier.
- **Type-validate inputs BEFORE side effects** (`isinstance(..., str)`).
- **Layout assertions** for any path-derivation reverse engineering
  (e.g. `repo_root = worktree_path.parents[2]` requires the worktree
  to actually live at `<repo_root>/.claude/worktrees/<id>/`).
- **End-to-end integration test** that proves the chain: spawn a
  real tmp git repo, drive the actual orchestrator entrypoint, and
  assert the downstream consumer received the value.

### Documented bypass scope (R2 honesty)

Fail-closed is a **partial backstop**, not an exhaustive contract.
The shell-comment heuristic catches the common foot-gun
(`# {prompt_prefix_file}`) but does NOT catch:

- **String-literal-inside-subprocess** —
  `python -c 'x="{prompt_prefix_file}"'` passes both the field check
  and the comment check, but the inner subprocess never `cat`s the
  file. Operators MUST follow the canonical
  `$(cat {prompt_prefix_file})` form documented in
  `claude/skills/flow/flow-phase2-execute/SKILL.md` transport
  section.

When the canonical operator template form is used, the K-class guard
is mechanically wired through. When operators improvise, they own the
risk — and the SKILL.md transport section warns them explicitly.

## Prevention（如何避免）

When adding a new kwarg to ANY shim or boundary function:

1. **No `**_kw` catch-all** unless you are deliberately implementing
   a forwarding generic (then assert the kwargs are passed downstream
   with a `set(kwargs) <= ALLOWED` check).
2. **If the kwarg flows into a substituted template / format string /
   API contract, add a fail-closed assertion** that the downstream
   really references it. Use the contract's own parser, not substring
   matching.
3. **Add an integration test that spans the entire chain**, not just
   per-layer unit tests. Per-layer tests with mocks at the boundary
   miss this exact class.
4. **Document the breaking change** in CHANGELOG with an explicit
   migration example for operators / downstream consumers.

## Trigger paths (where to grep when this recurs)

- `scripts/flow_subagent_dispatch.py` — `def invoke(...)`
- `scripts/flow_orchestrator.py` — every call site of `dispatch_fn`,
  `_invoke_subagent_dispatch`, and the `_prod_impl` adapter inside
  `_phase2_dispatch`.
- Any new dispatch shim: search for `**_kw`, `**kwargs`, or
  `**_kwargs` on a function whose downstream side is a string
  template or external CLI.

## Related

- v0.8.3 P0.0 — added K_CLASS_SENTINEL_PROHIBITION; relied on this
  wire being live (it wasn't).
- v0.8.3 P0.1 — added fresh-worktree-per-round redispatch; passes
  `prompt_prefix` via the same dead wire.
- v0.8.3 P0.2 — this fix + integration tests + fail-closed
  assertion. Pitfall captured here.
