---
description: "Save state before context switch — quick checkpoint mid-task"
---

# /flow:pause

User needs to step away or switch context mid-task. Save state without finishing.

## Difference from /flow:finish

- `/flow:finish` = task complete, archive
- `/flow:pause` = task NOT complete, just save current state for resume

## Protocol

## Step 1 — Load current

```bash
CURRENT=$(cat .flow/.current-task)
[ -z "$CURRENT" ] && { echo "No active task to pause"; exit; }
```

## Step 2 — Update progress.md with current state

Append to `## Execute Log`:
```markdown
| <timestamp> | PAUSE | <current scope> | <work-in-progress note> |
```

If currently in middle of an Execute or Verify step, note partial state:
- "Halfway through implementing X, at line Y"
- "Verify ran, 2 issues found pending fix"
- "Codex review queued"

## Step 3 — Auto-save

Invoke `{{capability:session_save}}` skill.

Also write a focused journal entry to `.flow/workspace/${USER}/journal.md`:
```markdown
## PAUSE: ${TASK_SLUG} — ${DATE} ${TIME}
- Phase: <current>
- Last action: <description>
- Next action when resume: <specific concrete>
- Blockers: <if any>
```

## Step 4 — Optional: stash uncommitted changes

If `git status --porcelain` shows uncommitted changes:
- Ask user: stash now or leave? Default = leave (work-in-progress)
- If stash: `git stash push -m "flow:pause ${TASK_SLUG} ${DATE}"`

## Step 5 — Confirm

Tell user:
- "Paused. Resume with `/flow:resume`"
- Optional: timestamp + journal entry path

## Constraints

- **Don't archive** — task stays active in `.flow/.current-task`
- **Don't commit** — pause is for ungated WIP
