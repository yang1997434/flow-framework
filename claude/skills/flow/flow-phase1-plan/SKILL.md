---
name: flow-phase1-plan
description: "Use when running Phase 1 of Flow framework — brainstorming requirements, parallel research, ADR-lite. Invoked by flow-orchestrator after task is created. Trigger: 'Phase 1', 'flow:plan', 'brainstorm this task'."
---

# Flow Phase 1 — Plan

Run the planning phase: brainstorm → (optional research) → prd.md complete → ADR-lite if applicable.

## Preconditions

- Active task exists: `.flow/.current-task` points to a task dir with empty/templated prd.md
- Task type + complexity already classified by Triage

## Step 0 — Deploy task config bootstrap

Before brainstorming, check whether this is a deploy task. Read `prd.md` header fields (`Type:` and `Complexity:` lines) — these are written during `/flow:start` triage and are the authoritative source for task classification.

If `Type: deploy` and `.flow/config.yaml` does not yet have a `deploy_target` field, invoke `{{capability:dev_setup}}` to detect platform (Fly/Render/Vercel/Netlify/Heroku/GitHub Actions/custom) and write deploy config. One-time per project. Skip if `deploy_target` already set.

## Step 1 — Brainstorm

Invoke `{{capability:brainstorm}}` skill. Follow its protocol:
- One question at a time
- Update prd.md immediately after each user answer
- No meta questions ("should I search?")
- Prefer offering 2-3 concrete options over open-ended questions

For UI tasks: **also invoke `{{capability:ux_brief}}`** to produce UX brief alongside requirements.

### Optional: perspective-shifted continuation

After base brainstorm completes (prd.md sketched), Flow may propose 0-N perspective-shifted brainstorming rounds. Each round = a new `{{capability:brainstorm}}` invocation with a hat-prompt prefix. Same one-question-at-a-time rhythm as base brainstorm. Output appends to prd.md as "Perspective Critique: <hat>" section.

Available hats (user picks; do not auto-chain all):

- **Engineer hat** — "You are a senior engineer reviewing this plan. Focus on architecture, data flow, edge cases, performance hotspots, and integration points. One question at a time."
- **DX hat** — "You are a developer-experience reviewer. Focus on friction points: setup steps, error messages, CLI ergonomics, time-to-first-value. One question at a time."
- **Security hat** — "You are a security reviewer. Focus on threat surfaces, secret handling, input validation, trust boundaries, and blast radius of destructive operations. One question at a time."

When to use which hat:
- Backend / API tasks → Engineer + Security
- CLI / SDK / dev tooling → Engineer + DX
- Anything touching prod / migrations / auth → add Security (regardless of task type)
- (Note: CEO / market-fit perspective intentionally not offered — Flow assumes user has validated commercial viability before reaching Phase 1)

### B/C-size tasks: file-protocol planning

For tasks projected at multi-day / multi-file (体量 B 或 C per Flow triage), invoke `{{capability:multi_step_plan}}` after base brainstorm. Size classification is stored in the `Complexity:` field of `prd.md` (written during `/flow:start` triage): `moderate` = 体量 B, `complex` = 体量 C. This creates `task_plan.md`, `findings.md`, `progress.md` per Manus-style protocol — complementary to brainstorming's prd.md (prd.md = WHAT, task_plan.md = stepwise HOW).

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
    model: "{{model:research}}",                  # primary alias
    description: "Research <topic>",
    prompt: "Research <specific question>; persist findings to {TASK_DIR}/research/<topic-slug>.md. Return only 1-line summary + file path."
  )
```

Multiple sub-agents in **one tool message** to run in parallel.

**Dispatch protocol (CRITICAL)**: Agent tool's `model` parameter is **enum-restricted** (only `sonnet|opus|haiku` aliases, no full IDs). Aliases resolve via `ANTHROPIC_DEFAULT_*_MODEL` env vars in `~/.claude/settings.json` — point them at the 1M-context variant for long-research depth.
- **Fallback chain**: if a dispatch returns "model not found / no access" error (e.g. env var pinned to a stale ID), retry that single sub-agent ONCE with the alias `opus` (recorded as `model_roles.research.fallback` in `defaults.json`). Opus alias also resolves to a 1M-context variant.
- **Never** route research sub-agents to the haiku alias — research depth requires Sonnet+ class.
- **Anti-regression**: this protocol replaces the older "render full ID into `model:`" approach which was incompatible with the enum-restricted tool param.

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
