---
description: "Start a new Flow task — Triage + create .flow/tasks/ structure + run Phase 1 brainstorm"
argument-hint: <task description>
---

# /flow:start

User wants to start a new task using the Flow framework. Follow this protocol:

## Step 1 — Triage (classify task complexity)

Classify the task in ONE sentence:
- **trivial** — typo / single-line / one-shot fix → exit framework, just do it
- **simple** — clear goal, ≤2 files, well-defined scope
- **moderate** — multi-file, some ambiguity, needs brief brainstorm
- **complex** — vague goal / architectural / multi-layer / novel library

Also identify task type (affects skill chain):
- backend / API / CLI
- frontend / UI / visual
- data / script
- documentation / content
- deploy / ops
- research / learning

## Step 2 — If trivial: exit framework

Tell user "Triage = trivial, doing directly without framework", then just do the task.

## Step 3 — If simple+: create task structure

```bash
SLUG=<derived-from-task>  # kebab-case, no date prefix
DATE=$(date +%m-%d)
TASK_DIR=".flow/tasks/${DATE}-${SLUG}"

mkdir -p "${TASK_DIR}/research"
# Copy templates and substitute placeholders
sed -e "s/{{TASK_TITLE}}/${TASK_TITLE}/g" \
    -e "s/{{DATE}}/$(date -I)/g" \
    -e "s/{{SLUG}}/${SLUG}/g" \
    -e "s/{{TASK_TYPE}}/${TASK_TYPE}/g" \
    -e "s/{{COMPLEXITY}}/${COMPLEXITY}/g" \
    {{REPO_ROOT}}/templates/prd.md.template > "${TASK_DIR}/prd.md"

sed -e "s/{{SLUG}}/${SLUG}/g" \
    {{REPO_ROOT}}/templates/progress.md.template > "${TASK_DIR}/progress.md"

# Mark as active
echo "${TASK_DIR}" > .flow/.current-task
```

If `{{REPO_ROOT}}/scripts/flow_task.py` is available, prefer:
```bash
python3 {{REPO_ROOT}}/scripts/flow_task.py create "${TASK_TITLE}" --slug "${SLUG}" --type "${TASK_TYPE}" --complexity "${COMPLEXITY}"
```

## Step 4 — Run Phase 1 (Plan)

Now invoke the **flow-phase1-plan** skill (if loaded) or run inline:

1. **Brainstorm** — Use `{{capability:brainstorm}}` skill. One question at a time, fill prd.md.
2. **(UI tasks)** — Also invoke `{{capability:ux_brief}}` for UX brief.
3. **(Research needed)** — Dispatch parallel `general-purpose` sub-agents to write to `${TASK_DIR}/research/<topic>.md`. Return only summaries to main session.

   **Sub-agent dispatch protocol (CRITICAL)**: the Agent tool's `model` parameter is enum-restricted (only accepts `sonnet|opus|haiku` aliases — NOT full model IDs). Aliases resolve to concrete IDs via `ANTHROPIC_DEFAULT_*_MODEL` env vars in `~/.claude/settings.json` (1M-context variants recommended).
   - **Primary**: pass alias `{{model:research}}`.
   - **Fallback**: if a sub-agent dispatch returns "model not found / no access", retry that single sub-agent ONCE with the alias `opus` (defined as `model_roles.research.fallback` in `claude/capabilities/defaults.json`). Opus alias also resolves to a 1M-context variant.
   - **Never** route research sub-agents to the haiku alias — research depth requires Sonnet+ class.
4. **ADR-lite** — When a major decision is made, fill the Decision section in prd.md with Context / Decision / Consequences / Revisit triggers.
5. **(High-reversal-cost decision)** — Invoke `{{capability:cross_model_consult}}` (mode={{capability:cross_model_consult.args.mode}}) for cross-model second opinion.

After Phase 1 done, prd.md should be complete and user has confirmed requirements.

## Step 5 — Tell user next step

Tell user: "Phase 1 complete. Ready for Phase 2 implementation. Reply '/flow:continue' to proceed, or describe adjustments."

## Arguments

`$ARGUMENTS` = task description (free text from user).

## Constraints

- **Do NOT** edit code in this command — only Plan phase work
- **Do NOT** put credentials anywhere in `prd.md` — use `credentials_ref:` field
- **Do NOT** skip Phase 1 even if user says "just code it"
- **Honor user override**: if user includes "skip flow / 别走流程 / 直接改", ack briefly and exit framework
