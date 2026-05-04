---
description: "Continue current Flow task — advance to next phase based on progress.md state"
---

# /flow:continue

User wants to advance the current Flow task. Follow this protocol:

## Step 1 — Identify current state

```bash
CURRENT=$(cat .flow/.current-task 2>/dev/null)
if [ -z "$CURRENT" ]; then
    echo "No active Flow task. Run /flow:start <task> first."
    exit
fi
```

Read `${CURRENT}/prd.md` and `${CURRENT}/progress.md`.

## Step 2 — Determine current phase

Inspect progress.md sections:

| State | Current Phase | Next |
|-------|---------------|------|
| `## Plan` empty | Phase 1 | Continue brainstorm |
| `## Plan` filled, `## Execute Log` empty or sparse | Phase 2 | Implement |
| `## Execute Log` complete (per Plan), `## Verify Report` empty | Phase 3 | Verify + commit |
| `## Verify Report` filled, `## Sediment Notes` empty | Phase 4 | Sediment + auto-save |
| All sections filled | Done | Suggest `/flow:finish` to archive |

## Step 3 — Run the appropriate Phase

### Phase 1 → continue brainstorm
Invoke `superpowers:brainstorming` to keep filling prd.md.

### Phase 2 → implement
Read prd.md `## Acceptance Criteria` and `## Technical Approach`.

**Determine sub-agent dispatch** (per `docs/编码框架.md` Phase 2 rules):
1. **Task type**: interlocking design decisions → main session single-thread; independent modules / breadth-first → parallel sub-agents OK
2. **Change size**: ≤2 files = 1 agent no worktree / 3-9 files = 1 worktree agent / ≥3 modules = N worktree agents
3. **Tool count escape**: >10 tools → fallback to single agent

Before dispatching: write scope plan to progress.md `## Plan` section. **Sub-agent scopes MUST NOT overlap**.

Use:
- `superpowers:test-driven-development` (write tests first)
- `superpowers:using-git-worktrees` (when worktree needed)
- `superpowers:dispatching-parallel-agents` (when N≥2)
- `impeccable:frontend-design` or `frontend-design:frontend-design` (UI tasks)
- `Agent` tool with `model: opus` for implement, `subagent_type: general-purpose`, `isolation: worktree` when needed

After each sub-agent finishes, append to `## Execute Log`.

If stuck (same bug 3+ times): invoke `gstack:codex` (challenge mode).

### Phase 3 → verify + commit
**Use fresh-context Generator/Evaluator pattern**:
- Dispatch a fresh `Agent(subagent_type: "general-purpose", model: "sonnet")` 
- Pass: only `git diff` + `prd.md`
- Task: check Acceptance Criteria, run lint/typecheck/tests, run credential grep
- Sub-agent must NOT see main session history (avoids self-praise bias)

Codex review triggers (per config.yaml):
- critical / breaking / DB migration / API change / ≥10 files / ≥500 lines

Write `## Verify Report` section.

Draft commit message → confirm with user → commit.

### Phase 4 → sediment
**This is a separate phase, do not skip even if "no new sediment"**:
- Decide promotion: ADR / pattern / pitfall to which tier
- Write `## Sediment Notes` section
- Auto-save: invoke `yangpeng-claude-skills:save` and append journal entry
- Suggest `/flow:finish` to archive

## Constraints

- **Do not skip phases**
- **Do not jump ahead** — finish current phase first
- **Honor user override** (`你直接改` etc.) — proceed without sub-agent for current turn only
