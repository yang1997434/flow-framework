---
name: flow-phase1-plan
description: "Use when running Phase 1 of Flow framework — brainstorming requirements, parallel research, ADR-lite. Invoked by flow-orchestrator after task is created. Trigger: 'Phase 1', 'flow:plan', 'brainstorm this task'."
---

# Flow Phase 1 — Plan

Run the planning phase: brainstorm → (optional research) → prd.md complete → ADR-lite if applicable.

## Preconditions

- Active task exists: `.flow/.current-task` points to a task dir with empty/templated prd.md
- Task type + complexity already classified by Triage

## Step 1 — Brainstorm

Invoke `{{capability:brainstorm}}` skill. Follow its protocol:
- One question at a time
- Update prd.md immediately after each user answer
- No meta questions ("should I search?")
- Prefer offering 2-3 concrete options over open-ended questions

For UI tasks: **also invoke `{{capability:ux_brief}}`** to produce UX brief alongside requirements.

## Step 2 — Auto-context (before asking)

Before asking ANY question, check:
- Repo structure (`find` / `grep` for similar patterns)
- Existing conventions in CLAUDE.md / spec/
- Existing pitfalls matching this task's files (auto-loaded by trigger_paths)

Write findings into prd.md `## What I already know`.

## Step 3 — Research (if needed)

Trigger conditions:
- ≥2 independent research topics
- Unfamiliar third-party library / framework selection
- Cross-platform / industry conventions to compare
- User says "best practice" / "how others do it"

**Don't research inline in main session.** Dispatch parallel sub-agents:

```
For each topic:
  Agent(
    subagent_type: "general-purpose",
    model: "{{model:research}}",
    description: "Research <topic>",
    prompt: "Research <specific question>; persist findings to {TASK_DIR}/research/<topic-slug>.md. Return only 1-line summary + file path."
  )
```

Multiple sub-agents in **one tool message** to run in parallel.

After sub-agents return:
- Read each `research/*.md` (or just the summary)
- Synthesize into prd.md `## Research References` (1-line takeaway + file link per topic)

## Step 4 — Diverge → Converge

After initial understanding, proactively consider:
- **Future evolution** (what this becomes in 1-3 months)
- **Related scenarios** (parity with adjacent flows)
- **Failure / edge cases** (conflicts, retries, idempotency)

Present to user as: "I want to consider 3 categories before locking MVP scope. Here are 1-2 bullets each. Which to include?"

Update prd.md `## Requirements` and `## Out of Scope` based on answer.

## Step 5 — ADR-lite (if major decision)

When a design decision with non-trivial reversal cost is made:

```markdown
## Decision (ADR-lite)
**Context**: <why this decision was needed>
**Decision**: <what + what was rejected>
**Consequences**: 
- Short-term cost
- Long-term benefit
- Reversibility
**Revisit triggers**: <conditions to re-open>
```

For **high reversal cost** decisions: invoke `{{capability:cross_model_consult}}` (mode={{capability:cross_model_consult.args.mode}}) for cross-model second opinion.

## Step 6 — Final confirmation

Show user a structured summary:
```
**Goal**: <one sentence>
**Requirements**:
**Acceptance Criteria**: (testable checkboxes)
**Definition of Done**: (quality bar)
**Out of Scope**:
**Technical Approach**:
**Decision (ADR-lite)** if applicable

Does this match? If yes, ready for Phase 2.
```

User confirms → done with Phase 1.

## Phase 1 completion criteria

| Condition | Required |
|-----------|----------|
| prd.md `## Goal` filled | ✅ |
| prd.md `## Acceptance Criteria` has testable items | ✅ |
| prd.md `## Out of Scope` filled | ✅ |
| User confirms requirements | ✅ |
| `research/*.md` exists if complex topic | recommended |
| `## Decision (ADR-lite)` if major decision | when applicable |

After done, mark progress.md `## Plan` section with the agreed plan and tell user: "`/flow:continue` to start Phase 2."

## Constraints

- **One question at a time** — don't dump 5 questions
- **No code changes in Phase 1** — pure planning
- **Research goes to files**, not chat
- **No credentials in prd.md** — use `credentials_ref:` field
