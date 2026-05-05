---
name: flow-wave-runner
description: "Use when Phase 2 dispatches a wave with size > 1. Runs paired implementer + spec-reviewer per task in parallel, sequentially merges with FILES_CHANGED verification, dispatches code-quality reviewer at wave end. Trigger: 'run wave', 'dispatch parallel implementers'."
---

# Flow Wave Runner — v0.7

Execute a single wave of N independent tasks (N ≤ cap, default 3) safely.

## Inputs

Phase 2 orchestrator hands you:
- `wave` — list of N task objects (id, writes, reads, description)
- `<task-slug>` — parent task directory in `.flow/tasks/<slug>/`
- `wave_branch_base` — git SHA to base this wave's integration branch on
  - Wave[0]: repo HEAD at Phase 2 start
  - Wave[k>0]: HEAD of Wave[k-1] integration branch

## Step 1 — Set up wave integration branch

```bash
WAVE_INDEX=<k>
WAVE_BRANCH="flow/wave-${TASK_SLUG}-${WAVE_INDEX}"
git checkout -b "$WAVE_BRANCH" "$WAVE_BRANCH_BASE"
```

## Step 2 — Set up worktrees

For each task in the wave, invoke `{{capability:worktree}}` with:
- branch name: `$WAVE_BRANCH-task-${TASK_ID}`
- base: `$WAVE_BRANCH_BASE`

## Step 3 — Dispatch paired (implementer + spec reviewer) for each task IN PARALLEL

Use a SINGLE message with N tool calls so all dispatches happen in parallel.

### Implementer prompt template

```
Subagent task: implement <TASK_ID>.

Files you may write (DECLARED writes:):
<task.writes>

Files you may read:
<task.reads + the task prd.md>

Implementation guidance:
- Follow {{capability:tdd}} — write failing tests first
- Use {{capability:behavioral_guidelines}} surgical-changes principle
- Stay strictly within the writes: scope. Anything outside will be flagged
  as wave contamination at merge time and the entire wave will abort.
- Generated files (e.g., src/generated/**) must be in writes: explicitly,
  otherwise add them and notify the controller.

Return a 6-field status report. Token budget: 600 tokens.

  STATUS: done | done_with_concerns | blocked | timed_out
  COMMIT_SHA: <full sha or "none">
  SUMMARY: 3-5 bullets — what changed and why
  CONCERNS: [] or short list
  NEXT: 1-2 lines — integration notes for controller

DO NOT report FILES_CHANGED. The controller will derive it from git.
DO NOT report failed_blocking / failed_nonblocking_waived / cancelled —
those are controller-derived states.
```

### Spec reviewer prompt template (paired with each implementer)

```
Spec review of <TASK_ID> after implementer commit.

Read prd.md (Acceptance Criteria) for <task-slug>.
Read commit at <COMMIT_SHA>.

Verify:
- All acceptance criteria touched by this task are addressed
- Implementer stayed within writes: scope (use git diff --name-only HEAD~1..HEAD)
- No undeclared file modifications

Return:
  VERDICT: approved | issues_found
  COMMIT_SHA: <reviewed sha>
  ISSUES: [] or list of {file, line, severity: critical|important|minor, fix_required: bool}
  RETRY_COUNT: 0 | 1 | 2
```

### Fix loop

If spec_reviewer returns `issues_found` AND `RETRY_COUNT < 2`:
- Re-dispatch the SAME implementer with the issues attached
- Re-dispatch the spec reviewer
- RETRY_COUNT++

If RETRY_COUNT == 2 and still failing:
- Mark task as `failed_blocking` (controller-derived state)
- Do not abort sibling tasks
- Continue to step 4 with this task in non-mergeable state

## Step 4 — Sequential merge into wave branch (controller-only)

This step is the round-3 fix: per-task pre/post SHA diff, NOT cumulative.

```bash
for TASK in $(echo "$WAVE_TASK_IDS_IN_PLAN_ORDER"); do
  # Skip non-mergeable terminal states
  STATE=$(get_task_state "$TASK")
  case "$STATE" in
    done|done_with_concerns) ;;  # mergeable
    *) continue ;;
  esac

  PRE_MERGE_SHA=$(git rev-parse HEAD)
  if ! git merge --no-ff "flow/wave-${TASK_SLUG}-${WAVE_INDEX}-task-${TASK}"; then
    # Conflict
    record_state "$TASK" contaminated
    abort_wave "$WAVE_INDEX" "merge conflict in $TASK"
    exit 1
  fi
  POST_MERGE_SHA=$(git rev-parse HEAD)

  # Per-task diff verification
  ACTUAL=$(python3 scripts/flow_wave_runner.py diff-names \
    --pre "$PRE_MERGE_SHA" --post "$POST_MERGE_SHA")
  
  if ! python3 scripts/flow_wave_runner.py verify-subset \
       --actual "$ACTUAL" --declared "<task.writes>"; then
    record_state "$TASK" contaminated
    abort_wave "$WAVE_INDEX" "$TASK wrote outside declared writes"
    exit 1
  fi
done
```

## Step 5 — Code-quality reviewer dispatch (at wave end)

After all merges succeed, dispatch ONE code-quality reviewer subagent against the integrated wave-branch diff.

```
Subagent task: code quality review of wave <WAVE_INDEX> integrated diff.

Diff scope: git diff <WAVE_BRANCH_BASE>..HEAD on $WAVE_BRANCH

Tasks merged in order: <task-id list>

Review for:
- Cross-task duplication
- Style drift between concurrent implementations
- Implicit contract drift (one task assumes a function shape another defined)
- Code-level inconsistencies that no individual spec reviewer would catch

Return:
  WAVE_VERDICT: approved | critical_blocking | minor_deferred
  CRITICAL_ISSUES: []  # block next wave
  MINOR_ISSUES: []      # write to .flow/tasks/<slug>/followup.md
  CROSS_TASK_OBSERVATIONS: 2-3 bullets
```

### Oversize fallback (round-3 absorbed)

If wave-branch diff exceeds ~10K tokens of diff content:
- Skip the cross-cutting reviewer
- Dispatch N per-task reviewers, each scoped to one task's diff
- Mark this wave's task pattern in cache as needing size=1 next recompute

## Step 6 — Wave barrier judgment (controller)

Inputs at barrier:
- N task terminal states (done / done_with_concerns / blocked / timed_out / failed_blocking)
- 1 wave_verdict (approved / critical_blocking / minor_deferred)

Rules:
- Any `failed_blocking` → MUST FIX, cannot waive. Dispatch fix subagent or escalate to user.
- Any `blocked` / `timed_out` / `cancelled` → blocks next wave; controller may waive with logged rationale via:
  ```bash
  python3 scripts/flow_wave_runner.py waive \
    --task-slug "$TASK_SLUG" \
    --task-id "$TASK_ID" \
    --state "blocked" \
    --rationale "no downstream task depends on $TASK_ID; deferring to followup"
  ```
- `wave_verdict=critical_blocking` → fix before advancing
- `wave_verdict=minor_deferred` → write minor issues to followup, advance
- All-clean → advance to wave[k+1]

## Forbidden moves

- DO NOT skip the `git diff --name-only <pre>..<post>` step — using subagent self-report is unsafe
- DO NOT octopus-merge the worktrees — sequential merge in plan order only
- DO NOT waive `failed_blocking` — that's the line locked at round-3
- DO NOT promote `done_with_concerns` to terminal-blocking state — concerns are logged but non-blocking
