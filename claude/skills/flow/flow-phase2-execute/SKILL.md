---
name: flow-phase2-execute
description: "Use when running Phase 2 of Flow framework — sub-agent dispatch, worktrees, implement + check loop. Invoked after Phase 1 confirms requirements. Trigger: 'Phase 2', 'flow:execute', 'implement this task'."
---

# Flow Phase 2 — Execute

Turn prd.md into code. Dispatch sub-agents per task type + change size.

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
| `interactive` (default) | Most tasks. Main session orchestrates: writes Plan, dispatches sub-agents, integrates, decides when to stop. Human-in-the-loop friendly. | Steps 2–8 below, with you as the conductor. |
| `parallel-subagents` | ≥3 independent modules / breadth-first scopes that don't share contracts. Currently the dominant flow Phase 2 mode. | Same as interactive but with N sub-agents in parallel; each in its own worktree. Use `{{capability:parallel_dispatch}}` for orchestration discipline. |

When dispatching sub-agents, **also** invoke `{{capability:subagent_discipline}}` for prompt + return-contract conventions (parallel_dispatch handles the orchestration; subagent_discipline handles the per-agent contract).

When an implementation plan exists (from `{{capability:multi_step_plan}}` in Phase 1), invoke `{{capability:execute_plan_discipline}}` to follow it task-by-task with checkpoint commits.

| `ralph-loop` | Long autonomous runs against a well-specified PRD checklist (every Acceptance Criterion is independently testable). Useful overnight / when you want to walk away. | Shell out to `scripts/flow_ralph.sh <task-slug>`. The script repeatedly invokes `claude --print` headlessly with fresh context per iteration; it picks the next `- [ ]` from prd.md, implements it, ticks the box, and exits when either the completion-promise string appears or `--max-iterations` (default 20) is hit. Logs land in `~/.flow/.runtime/ralph-<slug>.log`. |

**Why bash, not the official ralph-wiggum plugin?** Anthropic's plugin loops via an in-session Stop hook, which (a) collides with flow's own `stop.py` and (b) cannot be cleanly nested inside a sub-agent — see `.flow/tasks/05-04-audit-flow-issues/research/B-context-mode-ralph-loop.md`. The bash wrapper sidesteps both issues by running each iteration as a fresh `claude --print` process.

**Rules for `ralph-loop` mode**:
- The PRD's Acceptance Criteria checklist is load-bearing; vague items will produce vague iterations.
- Always set a sane `--max-iterations` (it is the real budget cap; the completion-promise string match is best-effort and can be missed by the model).
- Do NOT inject a system prompt that re-enters `flow:start` — that would nest a Phase 2 inside Phase 2 indefinitely. The wrapper deliberately keeps prompts plain.
- For dry-runs / CI, pass `--dry-run` to print the planned prompt without spending tokens.

If `phase2_mode` is `interactive` or `parallel-subagents`, continue to Step 2. If it is `ralph-loop`, hand off to `scripts/flow_ralph.sh` and skip directly to Step 8 (Phase 2 done check) once the script exits.

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
