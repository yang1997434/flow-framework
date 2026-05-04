---
description: "Resume Flow work after a session break — load state + run staleness check"
---

# /flow:resume

User is returning after a break. Restore context from breakpoint + verify nothing is stale.

## Step 0 — (v0.5) Personal /resume coordination

If the user has not yet run their personal `/resume` skill in this session,
suggest running it first for cross-conversation global state. After they do
(or if they say skip), continue with this command.

> "Have you run personal /resume yet? It loads MEMORY.md + session_latest.md.
> If not, run it first; then re-invoke /flow:resume for task-depth state.
> If you'd rather skip, say 'skip' and I'll proceed."

## Step 1 — Load active task

```bash
CURRENT=$(cat .flow/.current-task 2>/dev/null)
```

If empty: tell user "No active task. Run /flow:start <task> to begin."

If exists: read `${CURRENT}/prd.md` + `${CURRENT}/progress.md`.

## Step 1.5 — (v0.5) Load checkpoint files

If `${CURRENT}/.checkpoint/intent.md` exists:
- Read it. Surface the **Next Action** and **Mental Model** sections to the user.
- Note its `trigger` field — `manual` is highest fidelity, `auto-checkpoint`
  was written by autopilot (v0.6+), `autopilot-bail` means autopilot exited
  with concern.

If `${CURRENT}/.checkpoint/mechanical.json` exists:
- Compare its `ts` against intent.md's `ts`.
- If mechanical is > 5 min newer than intent, surface a staleness notice:
  *"Intent was last updated N minutes ago. Mechanical state shows M commits
  + K files touched since then. Review carefully before assuming intent is
  still fresh."*

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

Combine intent.md's Next Action (Step 1.5) with progress.md state.
If they agree → present concrete next step.
If they conflict → ask the user which is authoritative.

## Step 5 — Wait for user input

Don't auto-advance. Let user direct: "continue" / "edit prd" / "abandon and start new" / etc.
