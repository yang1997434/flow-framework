# Codex review R3 — v0.8.3 P0.2 implementer diff (post-R2-fix)

## R2 deltas applied

R2 verdict was YELLOW with 1 P0 (`{prompt_prefix_file!s}` / `{prompt_prefix_file:}`
bypass) + 1 P1 (docstring inconsistency). Implementer addressed both:

1. **R2 P0 (Formatter conv/spec bypass)** — Added
   `_assert_prompt_prefix_file_is_bare(template)` in
   `scripts/flow_subagent_dispatch.py`. Two-layered:
   - **Layer 1**: `Formatter().parse()` walk rejects non-empty
     `format_spec` or non-None `conversion` for field
     `prompt_prefix_file` (catches `!s`, `!r`, `!a`, `:>10`).
   - **Layer 2** (implementer-added bonus, not in feedback): raw-template
     regex `\{prompt_prefix_file[^}]` catches the empty-spec
     `{prompt_prefix_file:}` form which `Formatter().parse()` cannot
     distinguish from bare `{prompt_prefix_file}` (Python normalises
     both to `format_spec=''`). This is a parse-API blind-spot codex
     R2 didn't surface but the implementer caught while implementing.
   - Wired BEFORE the missing-placeholder + shell-comment + task_id
     gates (structural pre-gate).
   - 3 new tests: `test_invoke_raises_on_format_conversion_form`,
     `test_invoke_raises_on_format_spec_form`,
     `test_invoke_raises_on_shell_comment_with_conversion_form`.

2. **R2 P1 (internal docstring lies)** — `_template_field_names`
   docstring rewritten to align with the README/SKILL/pitfall scope
   statement (no longer claims string literals "do NOT count").
   Inline comment in `invoke()` cleaned up similarly.

## Diff stats (R3)

8 files staged, 985 PASS 0 regressions (exact match for PRD target
982 + 3 new R3 tests). 20 total P0.2 tests (16 unit in
`TestPromptPrefixWireUp` + 4 integration).

## Re-review focus

A. **R2 P0 truly closed?** — verify both layers (Formatter walk +
   raw-template regex) cover every `{prompt_prefix_file<...>}` variant.
   Specifically:
   - `{prompt_prefix_file}` — bare, ALLOWED
   - `{prompt_prefix_file!s}` / `!r` / `!a` — REJECTED
   - `{prompt_prefix_file:}` — REJECTED (raw-regex layer)
   - `{prompt_prefix_file:>10}` / `{prompt_prefix_file:.5}` — REJECTED
   - `{ prompt_prefix_file }` (whitespace inside braces — does Python
     accept this? If yes, is it also rejected by the regex?)
   - `{prompt_prefix_file[0]}` (subscript) — does this even parse?

B. **R2 P1 closed?** — docstring no longer self-contradicts.

C. **Adversarial probe (FINAL try)**: any creative attack still open
   in the bare-form world?
   - Multiple occurrences of the placeholder in template — does the
     bare-form check apply to all, or short-circuit on first?
   - Template with `{prompt_prefix_file}` AND `{prompt_prefix_file:>10}`
     simultaneously (mixed) — must reject.
   - Nested format strings? `{ prompt_prefix_file }` with extra
     whitespace?

D. **D-class swallowed exception sweep**: the new
   `_assert_prompt_prefix_file_is_bare` — any try/except that
   silently absorbs?

E. **B-class state-machine sweep**: the bare-form gate runs BEFORE
   the file write — does it correctly bail out without partial side
   effects?

## Verdict format

GREEN | YELLOW | RED + concise reasoning. If GREEN: explicitly say
"approved — write `.review-passed.json` marker and merge". If still
YELLOW after this round, please be specific about whether the gap is
fundamental (rethink) or just another adversarial heuristic gap that
requires endless cat-and-mouse (in which case advise scoping the
contract differently). ~250 words max. Be ruthless on remaining gaps.

Diff visible via:
`git -C /data/Claude/flow-framework/.claude/worktrees/agent-a400e5af5e2336336 diff --cached`
