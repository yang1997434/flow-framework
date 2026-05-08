# MergeRunner ctx Research — P0.1 fresh-per-round

**Date:** 2026-05-08
**Scope:** `flow_orchestrator.py` — MergeRunner, WorktreeContext, merge flow analysis

---

## Q1 — What does MergeRunner expect from `ctx`?

**Class:** `MergeRunner` (line 2294). Constructor (line 2313–2322):
```python
def __init__(self, *, ctx: WorktreeContext, contract, task_dir, run_id, task_id):
    self.ctx = ctx
```

**Fields consumed by `merge_task` (line 2328) and `_continue_merge` (line 2391):**

| Field | Where used | Purpose |
|-------|-----------|---------|
| `ctx.slug` | L2349, L2663 | `decisions.jsonl` event payloads |
| `ctx.worktree_id` | L2351, L2665 | event payloads (forensic) |
| `ctx.worktree_path` | L2352, L2367, L2489, L2548, L2595 | git commands run `git -C ctx.worktree_path`; Check #1/#2/#3 |
| `ctx.original_base_commit` | L2353, L2370 | checkpoint body (informational only) |
| `ctx.current_base_commit` | L2354, L2371 | checkpoint body (informational only) |
| `ctx.integration_target` | L2449, L2667 | R9 HEAD assertion: `head_ref != ctx.integration_target` blocks |
| `ctx.branch` | L2626, L2640 | Check #3: `actual_branch != ctx.branch` blocks |

**`_derive_repo_root` (line 2768):** derives repo root as `ctx.worktree_path.parents[2]`.
The assumption is `worktree_path = <repo_root>/.claude/worktrees/<worktree_id>/`.
A fresh ctx from a new worktree that follows the same path convention satisfies this.

**Conclusion:** MergeRunner does NOT require ctx to be the same object that ran `auto_dispatch_task`. It only needs a WorktreeContext pointing at the WINNER round's worktree — with correct `worktree_path`, `branch`, and `integration_target`.

---

## Q2 — WorktreeContext fields

**Dataclass** (line 310–327), 11 fields:

```python
@dataclass
class WorktreeContext:
    slug: str
    task_idx: int                      # zero-based — the t<n> in branch name
    worktree_id: str                   # = `<slug>+t<n>+<shortsha>`
    worktree_path: Path
    branch: str                        # same as worktree_id
    integration_target: str            # parent branch (e.g., master)
    original_base_commit: str          # full sha at creation (immutable)
    current_base_commit: str           # full sha after any rebase (S6)
    base_shortsha: str                 # 7-char short of original
    lifecycle_state: str               # active|merging|merged|aborted|blocked
    created_at: str                    # ISO 8601
```

**Fields that differ if ctx is freshly created vs. inherited:**

- `worktree_id` / `branch` / `worktree_path` / `base_shortsha`: will encode the NEW
  shortsha of the integration_target at Round N creation time (different from Round 1
  if integration_target advanced). This is **correct** behavior for fresh-per-round.
- `original_base_commit` / `current_base_commit`: point at the Round N fork point.
  This is intentional — `derive_task_facts` diffs `current_base_commit..HEAD` (line 461–468),
  so a fresh ctx diffs only Round N's changes.
- `task_idx`: must stay the same (0-based index into plan.manifests). Do NOT
  accidentally increment for retry rounds — the branch naming `<slug>+t<n>+<shortsha>`
  carries this and must remain stable for CrashRecovery classification.
- `created_at`: different timestamp. Safe — only used in `auto_engaged` event.
- `lifecycle_state`: should be `"active"` at creation. MergeRunner records `"merging"`
  in the event payload but reads it from the passed-in ctx, not from a live store.
  If a fresh ctx is created after Round 1 dispatch, `auto_engaged` (line 830) records
  the ROUND 1 ctx's `lifecycle_state` in decisions.jsonl — Round N ctx lifecycle events
  will have a different `worktree_id`, which is fine as long as the consumer
  correlates by `worktree_id`, not by chronological ordering alone.

**Potentially breaking if wrong:**
- `branch` must exactly match the symbolic HEAD in the winner worktree (Check #3, L2640).
  A fresh ctx must have `branch = worktree_id = new_slug+t{task_idx}+{new_shortsha}`.
- `worktree_path.parents[2]` must resolve to repo_root (L2776). WORKTREE_ROOT is
  `.claude/worktrees/` (relative) so path depth is fixed — safe for fresh worktrees.

---

## Q3 — Does the R3 merge flow depend on Round 1 state vs Round N?

**Step 6 merges the SHA from `facts.target_commit_pre_merge` (L2679)**, NOT from
`ctx.branch` ref directly. This SHA comes from `derive_task_facts(ctx)` (L2033 refresh
in GateRunner, or original L867).

**`derive_task_facts` (line 445–479)** diffs `ctx.current_base_commit..HEAD`. With
fresh-per-round, `current_base_commit` = integration_target at Round N worktree
creation → diff is ONLY Round N's committed changes. This is correct.

**No diff-baseline "ORIGINAL Round 1" dependency exists** in MergeRunner itself.
Checks #1/#2/#3 (L2488–L2648) only inspect the CURRENT winner worktree state:
- Check #1: `git status` on `ctx.worktree_path` → must be clean
- Check #2: `git rev-parse HEAD` on `ctx.worktree_path` == `facts.target_commit_pre_merge`
- Check #3: symbolic HEAD on `ctx.worktree_path` == `ctx.branch`

The `post_baseline_facts` refresh at L2033 in GateRunner also calls `derive_task_facts(self.ctx)`,
which uses the live ctx. If GateRunner holds the WINNER ctx and refreshes facts, the
diff is against the winner's base — correct.

**GOTCHA — `_prod_review` closure (L5320–5347):** The prod adapter closure captures `facts`
from the **outer scope** of `_cmd_auto_execute` (L5564: `ctx, facts = outcome.ctx, outcome.facts`).
This is the Round 1 facts. For Round 2+, `_prod_review` still passes these stale Round 1 facts
to `gate_runner.run_phase2`. GateRunner refreshes them internally at L2033
(`post_baseline_facts = derive_task_facts(self.ctx)`), but the initial `facts` arg
carries Round 1 data. This matters if any gate reads `facts` directly before the
refresh (gate 1 baseline at L1995 uses `facts` for the initial check). With fresh-per-round
and a new ctx, the outer `facts` variable should also be refreshed.

---

## Q4 — Pre/post-merge hooks or contract validations assuming Round 1 ctx?

**Reviewed:**

1. **`auto_engaged` event (L817–836):** emitted once, at Round 1. Bakes in `ctx.worktree_id`
   and `ctx.worktree_path` from the Round 1 ctx. `CrashRecoveryDispatcher` reads these events
   to classify crash state. If Round N ctx has a different `worktree_id`, the `auto_engaged`
   event records a DIFFERENT worktree than what merged. This is an **audit discrepancy**
   but not a runtime crash — recovery reads `auto_engaged.worktree_id` to locate the
   potentially live worktree for cleanup, but the Round N worktree won't match.

2. **`task_ready_to_merge` event (L2343–2358):** records `ctx.worktree_id`, `ctx.worktree_path`.
   These come from the passed-in (winner) ctx. No cross-reference against `auto_engaged`.

3. **`CrashRecoveryDispatcher.classify()` (line 3800):** checks journal for crash states,
   not for worktree_id consistency. State 3 (`auto_engaged_crashed`) detects presence of
   `auto_engaged` event without terminal event — no worktree_id cross-check here.

4. **`_task_already_completed` (L5505):** checks journal for `task_completed` event for
   `(run_id, task_id)`. No worktree_id involved.

5. **`write_auto_prepare_lock` / `consume_auto_prepare_lock` (L769–845):** these are
   consumed before subagent dispatch, entirely Round 1 scoped. No residual state
   carried to merge.

**Conclusion:** No lock files, stat timestamps, or journal readers hard-couple the
MergeRunner to ctx.worktree_id from Round 1's `auto_engaged` event. The discrepancy
is observable in audit logs but not runtime-breaking.

---

## Q5 — FAIL round worktree cleanup

**Current code (v0.8.2.1):** There is NO cleanup of FAIL-round worktrees.

The current prod `_prod_impl` adapter (L5312–5316) is a **no-op stub** — it returns `{}`
immediately. T18 ("will extend to re-dispatch on retry rounds 2+") is unimplemented.
So currently, the retry loop re-runs the codex review against the SAME Round 1 worktree
(facts passed via closure, same `ctx`). There is no Round 2+ worktree creation in the
current code.

**When P0.1 implements fresh-per-round:**

- `_prod_impl` for Round 2+ will call `create_task_worktree(...)` → new worktree.
- FAIL round worktrees (Round 1, Round 2, etc. that didn't pass) will accumulate
  under `.claude/worktrees/` unless explicitly cleaned up.
- No existing tracking structure records "FAIL ctx objects" — they'd only be locatable
  via `git worktree list` or filesystem enumeration.
- `Gate8VerificationRunner._9a_pass_cleanup` (around L3166) does `git worktree remove`
  for BOTH the task and verify worktrees after a successful gate 8. But that only
  runs on SUCCESS path (PASS round), and it removes the winning worktree AFTER merge.
- FAIL round worktrees have NO corresponding cleanup call anywhere.

**GOTCHA — `worktree_id` collision:** `create_task_worktree` uses
`<slug>+t{task_idx}+{shortsha_of_integration_target}`. If integration_target HEAD
doesn't advance between rounds (common in dev), Round 1 and Round 2 would produce
the SAME `worktree_id` → `git worktree add` would fail with "branch already exists".
This is the critical fresh-per-round naming collision problem.

**GOTCHA — `auto_engaged` for Round 2+:** `auto_dispatch_task` emits `auto_engaged`
(L817). If P0.1 calls `auto_dispatch_task` again for Round 2, a second `auto_engaged`
event is emitted with Round 2's worktree_id. `CrashRecoveryDispatcher` reads
`auto_engaged` events — two `auto_engaged` events for the same `(run_id, task_id)`
with no intervening terminal may trigger an unexpected crash classification.
Need to verify state machine handles multiple `auto_engaged` events, or bypass
`auto_dispatch_task` and call worktree creation + dispatch directly.

---

## Gotcha Summary

| # | Location | Risk | Severity |
|---|----------|------|----------|
| G1 | L5564, L5325 | `facts` closure in `_prod_review` captures Round 1 facts; GateRunner refreshes internally but outer `gate_runner.run_phase2(facts=facts)` arg is stale | Medium — GateRunner refresh at L2033 mitigates for gates 3+; gate 1 baseline arg still Round 1 |
| G2 | L421, L436 | `worktree_id = slug+t{n}+{shortsha}` — same sha if integration_target didn't advance between rounds → collision on `git worktree add` | HIGH — hard crash |
| G3 | L817–836 | Second call to `auto_dispatch_task` for Round 2 emits second `auto_engaged` event; crash recovery state machine may misclassify | Medium — needs audit of multi-engaged handler |
| G4 | None | No FAIL worktree cleanup anywhere; orphaned worktrees accumulate | Low (space/UX), no correctness risk |
| G5 | L2449, L2640 | MergeRunner Check #3 asserts `actual_branch == ctx.branch`; must pass WINNER ctx (not Round 1 ctx) — obvious but easy to miss at callsite | HIGH if wrong ctx passed |
| G6 | L2776 | `_derive_repo_root` hardcodes `parents[2]`; verify worktrees are at `.claude/worktrees/verify/<id>/` (depth 3+). Task worktrees at `.claude/worktrees/<id>/` — parents[2] = repo_root. Verify worktrees depth differs — irrelevant here but shows depth assumption is fragile | Low |
