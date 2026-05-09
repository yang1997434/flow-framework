# Codex consult R3 — v0.8.3 P0.2 plan-pass final gate

## R2 deltas applied

1. **R2 P0 (path typo `.flow/runtime/` → `.flow/.runtime/`)** — Fixed line 70
   of PRD (path-computation AC). Greppable: every PRD reference now uses
   `.flow/.runtime/`. New AC `test_invoke_path_contains_dot_runtime`
   asserts the rendered path contains the substring `/.flow/.runtime/`
   (regression guard).

2. **R2 P1#1 (verify-worktree layout misuse)** — Added explicit AC for
   `invoke()` layout assertion: `worktree_path.parent.name == "worktrees"
   AND worktree_path.parent.parent.name == ".claude"`; mismatch → raise.
   New unit `test_invoke_raises_on_unexpected_worktree_layout` exercises
   2 negative shapes (custom `wt/` dir + verify subdir).

3. **R2 P1#2 (build prefix only AFTER recovery decision)** — Tightened
   AC text: prefix build is "after `_task_already_completed` skip +
   `CrashRecoveryDispatcher.classify()` proceed, immediately before
   `auto_dispatch_task` call". Avoids side-effect in skip / fail-closed-
   interactive paths.

4. **R2 AC delta#1 (byte-for-byte content)** — Added unit
   `test_invoke_prefix_file_byte_for_byte`: file UTF-8 bytes ==
   `build_implementer_prompt(...)` output bytes; no BOM, no CRLF
   conversion, no trailing-newline mutation.

5. **R2 AC delta#2 (path typo regression)** — Covered by #1 above.

Test count: 9 → 12 (5 unit + 4 hardening + 1 path-typo guard + 2 integration).
Suite total target: 1002 PASS.

## Re-review focus

- Verify each R2 finding is closed by the listed PRD change (point to the
  actual AC text if claim is incomplete).
- Final GREEN-or-not call: any remaining structural issue requiring
  another round, or is this safe to dispatch to implementer?
- One last adversarial probe: anything you'd attack about this design
  that hasn't been raised yet?

## Verdict format

GREEN | YELLOW | RED, then a single short paragraph with reasoning.
If GREEN: explicitly say "ready to dispatch to implementer".
~150 words max. No new P0/P1 unless they truly block dispatch.
