# Codex consult R1 — v0.8.3 P0.1 implementer redispatch ADR review

## Role

You are reviewing an ADR-lite (architecture decision record) for v0.8.3 P0.1 of the Flow AI coding framework. The codebase under review is at `/data/Claude/flow-framework/`. Your job is to give a **second opinion** on the proposed design — find blind spots, edge cases, hidden complexity, and adversarial scenarios I may have missed. NOT a code review (no implementation yet); a **plan review**.

## In-scope vs out-of-scope (read carefully — I'll otherwise burn budget on your speculation)

### In scope (review these)

- Correctness of the dispatch state-machine changes (Round 1 vs Round 2+ asymmetry, state mutation order)
- Whether bypassing `auto_dispatch_task` for Round 2+ misses any side-effects (locks, journal, contract validation, sentinel writes)
- Worktree id collision avoidance (round discriminator approach)
- `state.failed_rounds` vs `state.current_round_ctx` data model — is this the right shape?
- `_prod_review` facts-closure refresh strategy (reading state vs re-deriving facts via `derive_task_facts`)
- Winner-ctx propagation: `dispatch_with_retry -> _phase2_dispatch -> _cmd_auto_execute -> MergeRunner`
- Round-cap default 3 → 2 (cost vs convergence trade-off)
- Test strategy: unit fakes + 1 mini integration test covering AC1-AC4

### Out of scope (do NOT explore)

- Parallel speculation dispatch — this is split to v0.8.3 P0.7. Do NOT propose it as a "missing feature".
- Best-of-N round scoring or rubric — explicitly rejected.
- Manual user-driven winner selection — conflicts with autonomous orchestrator.
- Cross-task worktree reuse / pooling — not in scope.
- 18-class trigger redaction logic — already implemented via `redact_blindspot_index`, not changing.
- AFK / budget / hard-stop behavior — not changing in P0.1.
- Round 1 behavior changes — Round 1 still uses `auto_dispatch_task` (fresh path is Round 2+ only).
- Migrating to fully unified Round 1+2+ path — explicitly rejected to preserve v0.8.2 backward-compat.

## Threat model

**Primary goal**: when reviewer FAILS in Round 1, the next round must (1) actually run a real implementer subagent (not return empty deltas), (2) run in a fresh worktree from base (no Round 1 contamination), (3) carry the reviewer's redacted feedback in its prompt, (4) the merger eventually merges the round that PASSES, not Round 1's worktree.

**Failure modes I'm trying to prevent**:
- Reviewer feedback never reaches implementer (current bug)
- Round 2 worktree id collides with Round 1 (worktree_id derives from `<slug>+t{n}+{shortsha}` — same shortsha if integration_target unchanged)
- `auto_engaged` event fires twice (CrashRecoveryDispatcher misclassifies)
- MergeRunner merges wrong ctx (Round 1 instead of winner)
- `_prod_review` uses Round 1 facts when reviewing Round N's diff (closure stale)
- FAIL-round worktrees pile up forever (no cleanup)
- Hidden state-machine invariant broken (J-class: dual-counter loop must always make progress)

## ADR-lite (proposal under review)

### Context
v0.8.2 T18 left `_prod_impl` as a stub returning empty deltas. Phase 2 retry loop at prod path is a no-op for Round 2+. Reviewer feedback never enters implementation.

### Decision
**Fresh worktree per round + bypass `auto_dispatch_task` + extract helper + winner ctx explicit propagation**.

1. New helper `_dispatch_implementer_fresh_worktree(*, state, task_dir, contract, manifest, criteria, prompt_prefix, round_num) -> (WorktreeContext, dict)`:
   - Bypasses `auto_dispatch_task` (avoids 2nd `auto_engaged` event + worktree_id collision)
   - Calls `create_task_worktree(round_discriminator=round_num)` directly
   - Calls `_invoke_subagent_dispatch(ctx, ...)` with the prefix
   - Calls `derive_task_facts(ctx)` to refresh facts
   - Returns (new_ctx, deltas)
2. `_prod_impl(*, state, prompt_prefix, **_kw)`:
   - Round 1 (`state.dispatch_retry_rounds == 0`): keep returning `{}` (Round 1 already dispatched in `_cmd_auto_execute`)
   - Round 2+: call helper. Append prev `state.current_round_ctx` to `state.failed_rounds`. Set `state.current_round_ctx = new_ctx`. Set `state.current_round_facts = new_facts`. Return deltas.
3. `_prod_review(*, state, impl_deltas, **_kw)`:
   - Use `state.current_round_ctx` and `state.current_round_facts` (NOT outer-scope captured Round 1 ctx/facts)
   - Pass to `gate_runner.run_phase2(facts=state.current_round_facts, ...)`
4. `dispatch_with_retry`: on `outcome=="pass"`, set `state.winner_ctx = state.current_round_ctx`. Return additionally.
5. `_phase2_dispatch` signature: `-> tuple[int, WorktreeContext | None]`. Round 1 fallthrough: `winner_ctx = ctx_round1`. Otherwise winner_ctx from state.
6. `_cmd_auto_execute`: `rc, winner_ctx = _phase2_dispatch(...)`; `MergeRunner(ctx=winner_ctx, ...)`.
7. `dispatch_retry_rounds_cap` default 3 → 2 (cost-aware).
8. `create_task_worktree(round_num=N)`: append `+r{N}` to worktree_id when N>=2 (Round 1 keeps current naming for backward compat).

### Rejected
- Re-call `auto_dispatch_task` for Round 2+: causes worktree_id collision + duplicate `auto_engaged` events
- Worktree reuse (in-place): contamination risk (user pre-decided)
- Best-of-N scoring, parallel speculation: out of scope (split)

### Consequences
- ~3-5 day work; mandatory opus gate; tests need extension (969 → ~975)
- RetryDeps protocol slightly extended (state mutated more)
- v0.8.2 backward compat preserved (Round 1 unchanged)
- Prepares for v0.8.3 P0.7 parallel speculation

## Critical research findings (already-known constraints)

- `auto_dispatch_task` is NOT idempotent (re-calling crashes at `git worktree add` because branch exists)
- It emits `auto_engaged` event consumed by `CrashRecoveryDispatcher`
- `MergeRunner` only uses ctx fields `worktree_path`, `branch`, `integration_target` — fresh ctx works if those match
- `derive_task_facts` diffs `current_base_commit..HEAD` so fresh ctx with fresh base = correct diff
- Existing tests are full-fake (no real fs); 3 unit tests at `tests/test_phase2_retry_loop.py`, `tests/test_dual_counter_invariants.py`, `tests/test_e2e_v0_8_2_p0.py`

## What I want from you (output format)

Produce a structured response with these sections:

### A. Verdict
GREEN / YELLOW / RED. One sentence rationale.

### B. Critical issues (P0)
Things that would BREAK the design if shipped as-is. Reference exact step number / decision point.

### C. Substantive concerns (P1)
Things that work but are fragile / suboptimal / hidden cost.

### D. Edge cases & adversarial scenarios
What happens when:
- Round 2+ subagent invocation hangs / crashes mid-flight?
- `create_task_worktree(round_num=2)` fails (disk full, git lock)?
- Worktree creation succeeds but subagent dispatch fails — orphaned ctx, what does state look like?
- `state.failed_rounds` retention vs persistence across recovery (state survives crash; do FAIL ctx survive?)
- Round 1 ctx + winner_ctx are the SAME object (Round 1 PASS path) — is the wiring sound?

### E. State-machine invariants
Does the design preserve dual-counter J-class invariants? Specifically:
- "loop must always make progress" — both `dispatch_retry_rounds` and `codex_review_rounds` increment monotonically; this must continue to hold
- Reviewer outcome enum is closed (`pass | fail | rejected_with_rationale`) — we're not introducing a new outcome
- Budget enforcement still wins over review verdict

### F. Hidden side-effects of bypassing `auto_dispatch_task`
List EVERY observable side-effect of `auto_dispatch_task` (read its 9 steps + the 3 imports it touches). For each, classify: "must replicate in helper" vs "intentional skip" vs "Round 1-only, never Round 2+".

### G. Recommended adjustments (concrete)
Bulleted list of specific decision changes — phrased as "change X to Y because Z".

### H. Test coverage gaps
What scenarios SHOULD have tests but my proposed tests miss?

## Tone

Adversarial but constructive. Prefer specific line refs over vague concerns. Quote function names exactly. Don't suggest features outside in-scope. Don't redo my brainstorm. Maximum 1500 words.
