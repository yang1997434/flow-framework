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
  model: "opus",  # implement = opus by default
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

For **UI tasks**: sub-agent prompt should include "Use `impeccable:frontend-design` skill for component design quality."

## Step 4 — TDD when applicable

If project has test infrastructure: invoke `superpowers:test-driven-development` to write tests first, before implement sub-agent dispatches.

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
  model: "sonnet",  # check = sonnet
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

## Step 7 — Stuck protocol

If main session has tried fixing same bug 3+ times:
- Invoke `gstack:codex` (challenge mode) — GPT-5.5 attacks assumptions
- If still stuck after challenge: `/clear` and rewrite prompt with what was learned

## Step 8 — Phase 2 done

When all Plan items have outcomes in Execute Log AND check sub-agent reports pass:
- Tell user: "Phase 2 complete. `/flow:continue` for Phase 3 verify + commit."

## Constraints

- **Sub-agent scopes never overlap** — enforce in Plan section
- **Don't let main session write code** if sub-agent dispatch warrants — keep integration role pure
- **No commit in Phase 2** — that's Phase 3
- **For UI**: ensure impeccable / frontend-design skill is used in implement prompt
