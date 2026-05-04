---
name: flow-orchestrator
description: "Use when user says '走 Flow' / 'use Flow' / 'start a task with framework' / 'Flow:<task>'. Routes to Triage → Phase 1-4 skills. Main entry point for the Flow framework workflow. Trigger words: 走 Flow, 用 Flow, Flow 流程, Flow start, start a Flow task, 跑框架, flow:start"
---

# Flow Orchestrator

You are the entry point for the **Flow** AI coding framework. The user wants to work on a task using the framework's 4-phase workflow.

## Relationship to global rules

Flow runs **inside** Claude Code's global gravity, not on top of it. Specifically:
- `~/.claude/CLAUDE.md` and the rules under `~/.claude/rules/*.md` are auto-loaded at session start and apply at every phase.
- Flow does **not** replace those rules; it surfaces them at decision points where context dilution makes the model forget.
- When in doubt, prefer the rule over flow's own instruction.

## Your job

Identify intent → run Triage → invoke the right phase skill → don't do the work yourself, **delegate to phase skills**.

## Step 1 — Identify intent

Parse user message for:
- Task description (the actual work to do)
- Override: "skip flow / 别走流程 / 直接改" → exit framework, do it directly
- Resume: "继续上次的" → invoke `/flow:resume` instead

## Step 2 — Triage

Classify the task **complexity** in one sentence:

| Class | Criteria | Action |
|-------|----------|--------|
| trivial | typo / single line / one-shot | Exit framework, just do it |
| simple | clear goal, ≤2 files, well-defined | Light brainstorm, single sub-agent OK |
| moderate | multi-file, some ambiguity | Full brainstorm, may need worktree |
| complex | vague / architectural / multi-layer | Full Phase 1 + likely multi-sub-agent |

Also classify **task type**:

| Type | Example | Skill chain (Phase 2 emphasis) |
|------|---------|-------------------------------|
| backend / API / CLI | "fix dispatch bug" | superpowers + karpathy |
| frontend / UI | "add settings page" | impeccable + frontend-design |
| data / script | "process CSV" | karpathy + planning-with-files |
| documentation | "write API docs" | document-skills + clarify |
| deploy / ops | "deploy to staging" | `{{capability:deploy_chain}}` |
| research | "compare libraries" | active-research + planning-with-files |

## Step 3 — Bootstrap structure (if simple+)

If `.flow/` doesn't exist in project root, run setup:

```bash
mkdir -p .flow/{tasks,ADRs,patterns,pitfalls,workspace,archive}
mkdir -p .flow/workspace/${USER}
# Append .flow/ entries to .gitignore (.runtime/ + config.local.yaml + workspace/<user>/)
```

If `flow init` is available, prefer it.

## Step 4 — Create task

Use templates from `~/projects/flow-framework/templates/`:

```bash
SLUG=<derived-from-task-title>
DATE_PREFIX=$(date +%m-%d)
TASK_DIR=".flow/tasks/${DATE_PREFIX}-${SLUG}"

mkdir -p "${TASK_DIR}/research"
# Substitute placeholders and write prd.md, progress.md
echo "${TASK_DIR}" > .flow/.current-task
```

## Step 5 — Invoke Phase 1 skill

Load the **flow-phase1-plan** skill and follow its protocol. **Do NOT inline brainstorm here** — that's phase 1's job.

After phase 1 completes, prompt user: `"Phase 1 done. Reply '/flow:continue' to start Phase 2 or '/flow:pause' to break."`

## Constraints

- **You orchestrate, you don't do**. Each phase skill knows how to run itself.
- **Always create task structure** before any real work — even for simple tasks
- **Honor user override** (skip / 别走) — but **note in retro** that this task bypassed framework
- **Don't load all 4 phase skills at once** — load each on demand to avoid context bloat
- **Read** `~/projects/flow-framework/docs/编码框架.md` only when uncertain — don't read full doc on every invocation

## Quick Read Guide

When you (the orchestrator) need framework reference:
- Triage criteria: read this file's Step 2
- Phase X behavior: read `flow-phaseX-plan/SKILL.md`
- Specific skill chain: read `~/projects/flow-framework/docs/Skills-Phase映射.md` (only the relevant task type section)
- Pitfall trigger_paths: read `.flow/pitfalls/` and `~/data/knowledge-base/pitfalls/` matching current files

**Don't** read full design doc unless user asks "explain the framework".
