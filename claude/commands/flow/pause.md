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

## Step 6 — (v0.5) Write intent.md snapshot

This captures your current mental state at the highest fidelity possible.
Write a markdown body covering the following sections, total length ≤ 1000 tokens:

- **## Current Intent** — 200-300 words: what you're working on right now
- **## Next Action** — one concrete step: file path, function, exact command
- **## Mental Model** — your remaining plan, decision rationale, assumptions
- **## Blockers** — external waits / blockers; may be empty
- **## Dont-Forget** — small details easily lost (e.g. "codex review left 5 nits")

Then write atomically via the helper:

```python
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "{{REPO_ROOT}}/scripts")
from common.safe_io import atomic_write_text, append_jsonl_locked
from common.checkpoint_paths import intent_path, history_path

intent_body = """\
---
schema_version: 1
trigger: manual
ts: <ISO timestamp now>
context_pct_estimated: <best-guess from your awareness, or 0>
task_slug: <task slug>
phase: <current phase>
supersedes: <previous trigger and ts, or none>
---

<the body sections you wrote above>
"""
atomic_write_text(intent_path(Path("<task path>")), intent_body)
append_jsonl_locked(history_path(Path("<task path>")), {
    "schema_version": 1,
    "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
    "event": "checkpoint",
    "trigger": "manual",
    "intent_len_chars": len(intent_body),
})
```

## Step 7 — (v0.5) Write cascade hint for personal /save

Outbox the hint so the user's personal `/save` skill picks it up next time
they save the session globally.

```python
from common.hint_outbox import write_hint
write_hint({
    "task_slug": "<task slug>",
    "task_path": "<absolute task path>",
    "phase": "<current phase>",
    "last_action": "<one sentence: what you just did>",
    "next_action": "<one sentence: what's next>",
    "pause_trigger": "manual",
})
```

## Step 8 — (v0.5) Mark nudge acknowledged

If a nudge had been pending, this manual pause counts as acknowledgement.

```python
from common.nudge import acknowledge
acknowledge(task_slug="<task slug>", via="manual_pause")
```

## Constraints

- **Don't archive** — task stays active in `.flow/.current-task`
- **Don't commit** — pause is for ungated WIP
