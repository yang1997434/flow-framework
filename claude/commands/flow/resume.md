---
description: "Resume Flow work after a session break — load state + run staleness check"
---

# /flow:resume

User is returning after a break. Restore context from breakpoint + verify nothing is stale.

## Step 1 — Load active task

```bash
CURRENT=$(cat .flow/.current-task 2>/dev/null)
```

If empty: tell user "No active task. Run /flow:start <task> to begin."

If exists: read `${CURRENT}/prd.md` + `${CURRENT}/progress.md`.

## Step 2 — Read journal for context

Read last 50 lines of `.flow/workspace/${USER}/journal.md` if exists.

Also check auto-memory: scan `~/.claude/projects/.../memory/MEMORY.md` for related entries.

## Step 3 — Staleness check

For each file path / function name / commit hash referenced in prd.md and progress.md:

```bash
# For each cited path
[ -f "<path>" ] || echo "STALE: <path> no longer exists"

# For each cited path that exists, check if it was modified recently
git log -5 --oneline -- <path> | grep -i "<task-slug>" || echo "Path modified outside this task: <path>"
```

If staleness found:
- List items
- Ask user: keep / update / mark obsolete
- Don't apply stale memory until confirmed

## Step 4 — Determine current phase + next step

Same as `/flow:continue`. Tell user:
- Current task: `<title>`
- Current phase: `<X>`
- Last activity: `<from journal>`
- Next step: `<concrete action>`
- Stale items found: `<count>` (if any)

## Step 5 — Wait for user input

Don't auto-advance. Let user direct: "continue" / "edit prd" / "abandon and start new" / etc.
