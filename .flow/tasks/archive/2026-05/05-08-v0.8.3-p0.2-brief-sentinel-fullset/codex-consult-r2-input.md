# Codex consult R2 — v0.8.3 P0.2 plan-pass re-review

## What changed since R1

PRD updated to address all R1 P0 + P1 + adversarial findings:

1. **R1 P0#1 (manifest_violation self-trigger)** — Resolved by relocating
   prefix file to `<repo_root>/.flow/.runtime/<slug>+<task_id>+r<round>/dispatch_prefix.txt`.
   `.gitignore:21` already contains `.flow/.runtime/` (verified). Worktree
   fact derivation only walks files inside the worktree, so a sibling
   `<repo_root>/.flow/...` path is invisible to it. Round-discriminator
   in path prevents R1/R2 collision.

2. **R1 P0#2 (`**_kw` silent-drop class still alive)** — Resolved by
   removing `**_kw` from `invoke()` signature. New signature:
   `invoke(ctx, *, subagent_env=None, task_id=None, prompt_prefix: str = "", round_num: int = 1) -> None`.
   Unknown kwargs now raise TypeError naturally (Python). New AC asserts this.

3. **R1 P0#3 (substring check too weak — `# {prompt_prefix_file}` etc.
   would pass)** — Resolved by switching fail-closed check to
   `string.Formatter().parse()`-derived field name set; substring match no
   longer used. New AC has 4 sub-assertions covering: comment, double-brace
   `{{...}}`, string-literal-only, and quoted/escaped variants.

4. **R1 P0#4 (type validation)** — Resolved: `prompt_prefix` non-str
   (None / bytes / int) raises TypeError before any side-effect. New AC.

5. **R1 P1#1 (`defaults.json:214` doc stale)** — Added explicit AC to
   update `claude/capabilities/defaults.json` placeholder list + example.

6. **R1 P1#2 (note that `{prompt_prefix_file}` is pre-quoted)** — Added
   explicit AC for `_resolve_cmd_template` docstring + RuntimeError text.

7. **R1 P1#3 (parallel-task isolation)** — Added unit
   `test_invoke_round_discriminator_in_path`; integration tests use
   slug+task_id+round-scoped paths inherently.

8. **R1 adversarial (operator template that mentions `{prompt_prefix_file}`
   but doesn't actually pipe content)** — Added doc warning in SKILL.md
   that operator template MUST `cat {prompt_prefix_file}` into the prompt;
   integration test asserts subprocess invocation rendered cmd contains
   the path AND the SKILL.md guidance is explicit. (Cannot mechanically
   enforce operator semantic correctness — only path appearance —
   acknowledged as "operator responsibility" with loud doc.)

## Re-review focus

Now that R1 P0/P1/adversarial are addressed, please verify:

A. **All P0/P1/adversarial truly addressed** — for each, point to the
   specific PRD section that fixes it; flag any incomplete fix.

B. **New silent-failure / wire-up gaps** introduced by the R1 fixes:
   - The `<repo_root>/.flow/.runtime/<...>/dispatch_prefix.txt` path
     derivation — does `worktree_path.parents[2]` always == repo_root
     for ALL worktree shapes? (Including verify worktrees at
     `<repo_root>/.claude/worktrees/verify/<run_id>+t<idx>+<sha[:7]>/`
     which would have `parents[3]` == repo_root, NOT parents[2].
     P0.2 dispatch path uses regular implementer worktrees, not verify;
     does this matter?)
   - The 5906-5914 prefix-build site in `_cmd_auto_execute` — runs
     BEFORE the CrashRecoveryDispatcher classify(); is that ordering
     safe (no side effects from prefix build before classify decides
     proceed/block)?
   - `string.Formatter().parse()` returns 4-tuples; `field` may be None
     (literal text) — the proposed predicate `if "prompt_prefix_file"
     not in fields` correctly excludes None. Confirm the parse cost is
     trivial for short templates (no DoS via huge template — template
     is operator-controlled, single-user, low priority).

C. **Test plan completeness for the new ACs**:
   - 9 tests total (5 unit + 2 integration + 2 added). Are there other
     missing categories given the R1 fixes (e.g., a test that verifies
     prefix file content matches `build_implementer_prompt` output
     byte-for-byte — no encoding loss / line-ending mangling / BOM)?

D. **Adversarial probe (R2)** — try once more: any *new* attack
   surface the file-based transport opens up, or any new silent-failure
   mode in the modified `_cmd_auto_execute` Round 1 wire?

## Verdict format (same as R1)

Same structure: GREEN / YELLOW / RED + P0 / P1 / adversarial / AC deltas.
~300 words max. Be ruthless on remaining gaps; don't soft-pedal.
