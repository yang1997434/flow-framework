# Codex review R2 — v0.8.3 P0.2 implementer diff (post-R1-fix)

## What changed since R1

R1 verdict was YELLOW with 1 P0 + 2 P1. Implementer addressed all three:

1. **R1 P0 (shell-comment fail-closed gap)** — Added
   `_template_uses_placeholder_in_executable_position(template)` helper
   in `scripts/flow_subagent_dispatch.py` with regex
   `(?:^|\s)#[^\"']*$` to reject `# {prompt_prefix_file}` (single-line,
   multi-line first-line `#`, tab-prefixed). Wired as second gate after
   the `Formatter().parse()` check. New test
   `test_invoke_raises_on_shell_comment_placeholder` (3 sub-assertions).
   Added `test_invoke_known_bypass_string_literal_subprocess` to pin
   the documented out-of-scope bypass (`python -c 'x="..."'`-style)
   — operator-responsibility scope. SKILL.md + pitfall now scope-honest:
   list 3 caught cases (missing placeholder, shell-comment, missing
   task_id) + explicit doc of the documented bypass.

2. **R1 P1#1 (`_cmd_auto_execute` integration coverage)** — Added
   `TestCmdAutoExecutePrefixWireUp` class with 2 tests:
   `test_cmd_auto_execute_round1_builds_and_passes_prefix` (drives
   `_cmd_auto_execute` with monkeypatched `auto_dispatch_task` that
   forwards to real shim) and `test_cmd_auto_execute_skip_path_writes_no_prefix`
   (`_task_already_completed=True` → no prefix file written, pinning
   the codex R2 PRD-pass P1#2 ordering invariant).

3. **R1 P1#2 (empty `task_id` collision risk via `'NOTASK'` fallback)** —
   New gate: `if prompt_prefix and not task_id: raise RuntimeError`.
   `'NOTASK'` fallback removed from path interpolation. New test
   `test_invoke_raises_on_empty_task_id_with_prefix`.

## Diff stats

8 files, +1485/-12 (was +1033/-12 at R1; +452 from R2 fixes).

Key additions:
- `scripts/flow_subagent_dispatch.py` — +88 lines (helper + 2 gates +
  removed fallback)
- `tests/smoke/test_subagent_dispatch_shim.py` — +75 lines (3 new units)
- `tests/smoke/test_v083_p02_dispatch_wireup.py` — +216 lines
  (`TestCmdAutoExecutePrefixWireUp` class + scaffolding)
- `claude/skills/flow/flow-phase2-execute/SKILL.md` — +20 lines
  (scope honesty rewrite)
- `.flow/pitfalls/dispatch-shim-silent-kw-drop.md` — +28 lines
  ("Documented bypass scope (R2 honesty)" subsection)
- `CHANGELOG.md` — +25 lines (R2 sub-bullets + count revisions)

## Test count

977 (R1) + 5 (R2) = 982 PASS, 0 regressions. PRD R2 target was 981; we
overshot by 1 because the shell-comment test contains 3 sub-assertions
counted as 1 method.

## Re-review focus

A. **R1 findings truly closed?** — for each (P0 shell-comment, P1#1
   `_cmd_auto_execute` integration, P1#2 empty task_id) point to the
   specific code/test that closes it. Flag any incomplete fix.

B. **New silent-failure / wire-up gaps introduced by R2 fixes**:
   - The shell-comment regex `(?:^|\s)#[^\"']*$` — is it tight enough?
     Does it have false-positives (rejecting legitimate templates) or
     false-negatives (missing more comment styles)?
   - The `prompt_prefix and not task_id → raise` gate — is the
     symmetric case (prompt_prefix empty + task_id empty) correctly
     handled (no false-fail)?
   - Does the integration test `test_cmd_auto_execute_round1_builds_and_passes_prefix`
     actually exercise the real `_cmd_auto_execute` body, or does it
     shortcut around the build site (codex R1 P1#1 was specifically
     about not testing the real build site)?

C. **D-class swallowed exception sweep** (per project memory: codex
   review必抓 swallowed exception): any `try/except` in the new helpers
   that silently absorbs an error?

D. **B-class state-machine sweep**: any new path silently advances
   counters or skips them?

E. **Adversarial probe (R2 — last try)**: any creative attack still
   open? Any way an operator template can pass all gates but still
   defang the K-class delivery (other than the documented
   string-literal-inside-subprocess bypass already pinned)?

## Verdict format

GREEN | YELLOW | RED + concise reasoning. If GREEN: explicitly say
"approved — write `.review-passed.json` marker". ~250 words max.
Be ruthless on remaining gaps.

Run `git -C /data/Claude/flow-framework/.claude/worktrees/agent-a400e5af5e2336336 diff --cached`
to see the full R2 diff.
