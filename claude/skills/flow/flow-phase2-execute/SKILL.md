---
name: flow-phase2-execute
description: "Use when running Phase 2 of Flow framework — sub-agent dispatch, worktrees, implement + check loop. Invoked after Phase 1 confirms requirements. Trigger: 'Phase 2', 'flow:execute', 'implement this task'."
---

# Flow Phase 2 — Execute

Turn prd.md into code. Dispatch sub-agents per task type + change size.

> **Safety**: before any destructive operation in this phase (`rm -rf`, `git reset --hard`, `DROP TABLE`, force-push, `kubectl delete`, migrations), invoke `{{capability:safety_guardrails}}`. See orchestrator §Cross-cutting capabilities.

## Auto mode (v0.8.1+)

Before doing anything else: if `progress.md` frontmatter sets
`autonomy_mode: auto`, invoke

```bash
flow orchestrator --auto-execute <slug>
```

The orchestrator owns (v0.8.1 dispatch foundation):

- **Worktree-per-task dispatch** (`<slug>+t<n>+<shortsha>` naming, dual-base
  recording).
- **8-gate runner**: 1 baseline / 2 subagent execution / 3 manifest verify /
  4 codex review / 5 acceptance criteria / 6 regression smoke /
  7 local merge / 8 post-merge verify in ephemeral verification worktree.
  **Gates run ONCE per dispatch in v0.8.1** — no retry loop (deferred to
  v0.8.2).
- **Tier 1 + Tier 2 notification** (`blocked.md` + OSC 9 + BEL with throttle).
- **Crash recovery** (5-state classifier: pre-lock / lock+dead-pid /
  auto_engaged crash / mid-merge / verification-orphan).
- **Nested-autonomy mechanical guard** (`FLOW_AUTONOMY_PARENT_PID` env var).

**NOT in v0.8.1** (deferred to v0.8.2): AFK timeout, budget enforcement
(5 counters with paused clock), Phase 2 retry loop, in-loop staleness
gate. v0.8.1 fails fast on first gate non-pass via Notifier. Run
`flow doctor <slug>` BEFORE invoking `--auto-execute` to check
staleness (the in-loop gate ships in v0.8.2).

### AFK runtime (v0.8.2 T2 — ships AFK monitor; T3 wires retry loop)

Contract field `afk_on_timeout` is now consumed at runtime via
`scripts/common/afk_monitor.py`:

- **Default `wait`** — autonomy norm; idle timeout produces no
  hard-stop snapshot, caller stays parked. Only the 24 h hard cap can
  override `wait` and force termination.
- **`abort`** — idle past `idle_seconds_threshold` (default 30 min)
  produces a `HardStopSnapshot(reason="afk_timeout", …)` via
  `apply_afk_check(monitor, slug, now_iso)`. Snapshot reuses the T1
  frozen `schema_version="v1"`.
- **24 h hard cap** — `clock.active_seconds(now) >= 86400` ALWAYS
  produces a snapshot, regardless of mode. This is the load-bearing
  invariant: even `wait` mode cannot exceed 24 h.
- **3 mechanical activity signals** reset the AFK timer (any one):
  monitored-dir file mtime tick, command issuance to subagent,
  subagent heartbeat / progress.md update.
- Pause/resume passthrough delegates to T1 `PausedClock`. Activity
  recorded during a pause does NOT auto-resume the clock (B-class
  state-machine guard).

T2 ships the helper dormant. T3 invokes `apply_afk_check` per
dispatch tick inside the Phase 2 retry loop alongside budget
enforcement. Both share the single hard-stop snapshot path.

### Retry-on-non-pass loop (v0.8.2 T3)

`flow_orchestrator.dispatch_with_retry` replaces v0.8.1 fail-fast
with a bounded retry loop. Two **independent** round caps:

- `phase2.retry.max_dispatch_retry_rounds` (default `3`) — caps
  implementer retries; advanced ONLY by review verdict `fail`.
- `phase2.review.max_codex_review_rounds` (default `2`) — caps codex
  review rounds; advanced ONLY by `rejected_with_rationale` (RWR).

**Five dual-counter invariants** (PRD §R2 / ADR-1):

1. `dispatch_retry_rounds` caps implementer-retry loops only.
2. `codex_review_rounds` is independent; RWR consumes it, NOT retry.
3. The 5 T1 budget counters cap EVERYTHING — round counters cannot
   outpace them; budget hit is checked before each round.
4. All 4 terminal paths (`budget_hit`, `retry_cap`, `codex_review_cap`,
   `afk_timeout`) emit the same `HardStopSnapshot v1` shape — see
   `scripts/common/snapshot.py`.
5. Every loop iteration advances EXACTLY ONE counter or terminates.
   No path is allowed to leave both counters static while continuing
   (J-class chained-paper-cut guard).

**Reviewer feedback transparency rule**: when a `fail` review forces
the implementer to retry, the reviewer's specific findings are
included as a prompt prefix BUT the 18-class blindspot trigger
checklist is stripped via `redact_blindspot_index()` (we want the
implementer to fix the bug, not cargo-cult the categorisation).

**progress.md round-incremental logging**: each round appends a row
to the `## Execute Log` table (no overwrites). If the section is
absent the helper no-ops — never crashes the loop on a malformed
progress.md.

**Pause/resume bracketing**: codex review wait time does NOT tick
AFK. The loop calls `afk.pause("codex_review")` before each review
and `afk.resume()` after.

## Reviewer self-check — 18-class blindspot framework

The full framework lives at `.flow/pitfalls/claude-review-blindspots.md`.
Reviewer agents (gate 4 codex pass + any human reviewer) MUST consult
the trigger checklists per class as part of self-check. The
`scripts/dispatch_template.py::build_reviewer_prompt` helper mounts
the same content into Python-built reviewer prompts; this SKILL.md
section is the equivalent for SKILL-driven reviewer flows.

Class summary (one line each — full triggers in the file above):

  A — Python falsy/truthy traps (.get + or, is None vs not in)
  B — design cross-reference semantics (enum × field cartesian product)
  C — architectural ordering / reachability (gate before exception swallow)
  D — bypass via fallback path (try/except return False; rc != 0 lying)
  E — shell=True + prefix-match = compound-command bypass; metachar guard
  F — identity check fail-open (missing hash → block, never skip)
  G — facts-from-disk: enumerate ALL state layers (HEAD/index/wt/untracked)
  H — external tool output parsing ambiguity (use -z / --json, not split)
  I — repeating earlier task's mistake; grep existing helpers before writing
  J — fix-chain paper-cuts (audit verdict / forensic / labels with happy path)
  K — plausible-justification trap; deviating from helper needs codex audit
  L — type-check vs presence-check (key in dict ≠ value is the right type)
  M — shared state file cross-task pollution (filter jsonl by task scope)
  N — disk identity vs ref identity (merge SHA, not branch ref)
  O — same-pid TOCTOU within-second (use µs ts for path-from-ts collisions)
  P — JSONL scope key must include task_id, not just run_id
  Q — filter + enumerate index drift (preserve original idx for audit)
  R — frontmatter / OSC injection (full splitlines() separator class)
  S — wire-up gap: helper exists but production never calls it
  T — codex counter-factual anchoring across review rounds

**Redaction rule** (do NOT skip this): review feedback that flows
back to the implementer (via `state.last_reviewer_feedback`) MUST NOT
include class letters. The `flow_orchestrator.redact_blindspot_index`
helper enforces this by stripping `A. ` / `Class A:` / `[A] ` /
`(A) ` line headers (letters A-T) before the next implementer round.
Use class letters to organize YOUR self-check; emit specific concrete
findings (file refs, line refs, exact behaviours) to the implementer.
Class labels in impl-facing feedback would let the implementer
cargo-cult the categorisation rather than fix the actual bug.

## Implementer prompt — K-class sentinel prohibition

`scripts/dispatch_template.py::build_implementer_prompt` auto-prepends
the K-class prohibition to every first-pass code dispatch. The text
is pinned by tests (`tests/smoke/test_dispatch_template.py`); do not
weaken it. Doc-only dispatches (`is_doc_only=True`) — progress.md
updates, sediment notes — opt out because the pre-commit review hook
they would bypass does not run on doc-only paths anyway. The default
is safe-by-default: prohibition prepended unless explicitly opted out.

**Subagent contract** (PRD §1.2): execute the per-task prompt; return
narrative summary ONLY. Orchestrator derives facts from worktree
`git diff` and test logs — subagent self-report fields are ignored.
DO NOT attempt manual worktree management or merge from inside this
SKILL — the orchestrator has the authority.

**Hard rule (§7)**: once `auto_engaged` event has been written, any
subsequent path to interactive mode MUST go through
`block + user choice`. NEVER silently switch — this includes crash
recovery on next-startup. If `flow orchestrator --auto-execute <slug>`
exits non-zero, follow `blocked.md` instructions for the user choice.

Exit codes:

- `0` = clean completion, OR contract missing → interactive fallback
  (legal pre-lock — user never opted in this attempt).
- `3` = block raised (`blocked.md` written; user resume needed).
- `4` = `aborted_nested` (nested-autonomy attempt detected).

## Step 1 — Determine dispatch strategy

Read prd.md. Decide:

**Task type discriminator** (Cognition vs Anthropic reconciliation):

| Type | Strategy |
|------|---------|
| Breadth-first / read-only / independent modules | ✅ Parallel sub-agents OK |
| Interlocking design decisions (cross-file contracts) | ❌ Single-thread main session integrates |

**Change size**:

| Change | Model | Worktree | # Sub-agents |
|--------|-------|----------|-------------|
| ≤2 files, single module | Opus 4.7 | No | 1 |
| 3-9 files, 1-2 modules | Opus 4.7 | Yes | 1 |
| ≥3 independent modules | Opus 4.7 | Yes (each) | N |
| Novel architecture | Opus 4.7 | Case-by-case | 1 (complex decision) |

**Tool count escape hatch**: if task needs >10 distinct tools → fallback to single agent (arxiv 2512.08296 β=−0.330).

## Step 1.5 — Execution mode selection

flow Phase 2 supports three execution modes. Pick one based on the task profile and the project's `phase2_mode` setting in `.flow/config.yaml` (default: `interactive`).

| Mode | When to use | How it runs |
|------|-------------|-------------|
| `interactive` (default) | Most tasks. Main session orchestrates. | Steps 2-8 below |
| `wave-dispatch` | progress.md has `### Tasks` block with writes: declared per task. | Steps 1.6-1.9 below replace 2-6 |
| `parallel-subagents` | Legacy: ≥3 independent breadth-first scopes without writes: declarations | Same as v0.6 |
| `ralph-loop` | Long autonomous runs against well-specified PRD checklist | scripts/flow_ralph.sh |

When dispatching sub-agents, **also** invoke `{{capability:subagent_discipline}}` for prompt + return-contract conventions (parallel_dispatch handles the orchestration; subagent_discipline handles the per-agent contract).

When an implementation plan exists (from `{{capability:multi_step_plan}}` in Phase 1), invoke `{{capability:execute_plan_discipline}}` to follow it task-by-task with checkpoint commits.

**Why bash for ralph-loop, not the official ralph-wiggum plugin?** Anthropic's plugin loops via an in-session Stop hook, which (a) collides with flow's own `stop.py` and (b) cannot be cleanly nested inside a sub-agent — see `.flow/tasks/05-04-audit-flow-issues/research/B-context-mode-ralph-loop.md`. The bash wrapper sidesteps both issues by running each iteration as a fresh `claude --print` process.

**Rules for `ralph-loop` mode**:
- The PRD's Acceptance Criteria checklist is load-bearing; vague items will produce vague iterations.
- Always set a sane `--max-iterations` (it is the real budget cap; the completion-promise string match is best-effort and can be missed by the model).
- Do NOT inject a system prompt that re-enters `flow:start` — that would nest a Phase 2 inside Phase 2 indefinitely. The wrapper deliberately keeps prompts plain.
- For dry-runs / CI, pass `--dry-run` to print the planned prompt without spending tokens.

If `phase2_mode` is `interactive` or `parallel-subagents`, continue to Step 2. If it is `ralph-loop`, hand off to `scripts/flow_ralph.sh` and skip directly to Step 8 (Phase 2 done check) once the script exits.

## Step 1.6 — wave-dispatch mode: invoke wave planner

Detect `### Tasks` block in progress.md. If present:

1. Resolve cap from project config (`.flow/config.yaml` → `phase2.parallel_dispatch.cap`, default 3)
2. Invoke `{{capability:wave_planning}}` skill, passing task_slug, controller_model, cap
3. Receive ordered waves[]

If `wave_planning` capability is unavailable:
- Log `[wave-dispatch] capability unavailable, falling back to all-serial via subagent_dispatch`
- Treat all tasks as size-1 waves and proceed via Step 3 (legacy single-implementer dispatch)

## Step 1.7 — wave-dispatch mode: per-wave dispatch

For each wave in waves[]:

```
if wave.size == 1:
    dispatch via {{capability:parallel_dispatch}} (existing path, Step 3)
else:
    dispatch via {{capability:wave_dispatch}} (new flow-wave-runner)
```

If `wave_dispatch` capability is unavailable but `wave_planning` succeeded:
- Log fallback
- Decompose for preview but execute serially via subagent_dispatch

## Step 1.8 — wave-dispatch mode: wave barrier

After each wave runs:
- Collect terminal states from runner
- Apply default-block-on-failure policy:
  - `failed_blocking` → MUST FIX (cannot waive); dispatch fix subagent or escalate user
  - `blocked` / `timed_out` / `cancelled` → ask controller for explicit waive with logged rationale
  - `wave_verdict=critical_blocking` → fix before advancing
- All clean → next wave

## Step 1.9 — wave-dispatch mode: end of last wave

When the last wave completes cleanly:
- Skip ahead to Step 8 (Phase 2 done)
- progress.md `## Execute Log` will have one row per task per wave

## Step 2 — Write scope plan to progress.md

Before any sub-agent dispatch, write `## Plan` in progress.md:

```markdown
## Plan
- main session: <integration role>
- sub-agent A → scope: src/auth/**, modify: login.ts/logout.ts
- sub-agent B → scope: src/api/**, modify: handlers.ts only
- sub-agent C → scope: tests/auth/**, modify: 新增 auth.test.ts
```

**Sub-agent scopes MUST NOT overlap**. This is the file-protocol replacement for sub-agent communication.

## Step 3 — Dispatch implement sub-agent(s)

For each sub-agent:

```
Agent(
  subagent_type: "general-purpose",
  model: "{{model:implement}}",
  isolation: "worktree",  # if change size warrants
  description: "Implement <scope>",
  prompt: """
    Task scope: <files / module>
    
    Read prd.md: <task_dir>/prd.md
    Read implement.jsonl (if exists): <task_dir>/implement.jsonl
      → load all referenced spec files
    Read relevant pitfalls matching scope (auto-loaded by trigger_paths)
    
    Write code per Acceptance Criteria.
    Don't commit. Run lint + typecheck before returning.
    
    Return: 1-line summary + list of changed files.
  """
)
```

For **UI tasks**: sub-agent prompt should include "Use `{{capability:ui_implement}}` skill for component design quality."

## Step 4 — TDD when applicable

If project has test infrastructure: invoke `{{capability:tdd}}` to write tests first, before implement sub-agent dispatches.

Also invoke `{{capability:behavioral_guidelines}}` once before the first implement sub-agent — its principles (surgical changes, define success criteria, surface assumptions, avoid over-engineering) are easy for the model to drop in long sessions and need re-surfacing at the implement boundary.

## Step 5 — Append to Execute Log

After each sub-agent completes, append a row to `## Execute Log`:

```markdown
| 2026-05-04 14:23 | sub-agent A | src/auth/** | login.ts/logout.ts updated, tests pass |
```

## Step 6 — Check sub-agent

Dispatch a check sub-agent (Sonnet, no worktree — reads diff):

```
Agent(
  subagent_type: "general-purpose",
  model: "{{model:review}}",
  description: "Quality check this diff",
  prompt: """
    Read prd.md (Acceptance Criteria), check.jsonl (if exists, load specs).
    Inspect git diff (uncommitted).
    Run lint + typecheck + applicable tests.
    Auto-fix simple issues; report complex issues.
    
    Return: pass/fail + list of issues.
  """
)
```

For **cross ≥3 layers** changes: upgrade check sub-agent to Opus.

### Deploy task type — execution path

Two alternative paths (user picks based on confidence; do not auto-pick — surface the choice):

- **`{{capability:deploy_chain}}`** (default for large features) — separate `ship` then `canary` monitoring then manual land. Use when you want to observe canary metrics before merging.
- **`{{capability:land_and_deploy}}`** — one-shot merge + deploy + canary verify. Use for **small confident changes** where round-tripping through observation feels like overhead.

Trigger condition: `Type: deploy` field in `prd.md` (set during `/flow:start` triage).

### When stuck on a bug

1. **First touch** — invoke `{{capability:systematic_debug}}` (iron-law 4-phase root-cause discipline). Don't reach for fixes; identify the root cause first.
2. **Escalate if insufficient** — if systematic_debug isn't producing the answer after 1-2 cycles, invoke `{{capability:deep_investigate}}` for the heavier debugging pipeline.
3. **Last resort** — `{{capability:cross_model_challenge}}` (mode={{capability:cross_model_challenge.args.mode}}) for adversarial cross-model attack on assumptions.

## Step 7 — Stuck protocol

If main session has tried fixing same bug 3+ times:
- Invoke `{{capability:cross_model_challenge}}` (mode={{capability:cross_model_challenge.args.mode}}) — attacks assumptions
- If still stuck after challenge: `/clear` and rewrite prompt with what was learned

## Step 8 — Phase 2 done

When all Plan items have outcomes in Execute Log AND check sub-agent reports pass:
- Tell user: "Phase 2 complete. `/flow:continue` for Phase 3 verify + commit."

## Constraints

- **Sub-agent scopes never overlap** — enforce in Plan section
- **Don't let main session write code** if sub-agent dispatch warrants — keep integration role pure
- **No commit in Phase 2** — that's Phase 3
- **For UI**: ensure impeccable / frontend-design skill is used in implement prompt
