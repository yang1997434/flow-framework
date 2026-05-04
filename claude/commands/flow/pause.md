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

**Failure-tolerance contract**: if any helper call below
(`atomic_write_text`, `append_jsonl_locked`, `write_hint`, `acknowledge`)
raises, log the exception inline to the user and continue — do NOT
abort `/flow:pause`. The journal entry from Step 3 is still the source
of truth; the v0.5 checkpoint files are best-effort enrichment.

**Populating `supersedes`**: if `intent_path(task_path)` already exists,
read its frontmatter and use the prior `trigger` + `ts` (e.g.
`manual@2026-05-04T15:30:00`). Otherwise use `none`.

**Populating `context_pct_estimated`**: import the estimator and call it
on the current session's transcript path. If the transcript path is
unknown to you in this command flow (it usually is — `/flow:pause` runs
in a slash-command context, not a hook), fall back to `0`. The PreCompact
and PostToolUse hooks capture the real value into mechanical.json
independently.

Then write atomically via the helper:

```python
import sys
from pathlib import Path
from datetime import datetime
sys.path.insert(0, "{{REPO_ROOT}}/scripts")
from common.safe_io import atomic_write_text, append_jsonl_locked
from common.checkpoint_paths import intent_path, history_path
from common.context_estimator import estimate_context_pct

# Resolve task_path once for all helpers.
task_path = (
    Path(".flow/tasks") / Path(".flow/.current-task").read_text(encoding="utf-8").strip()
).resolve()

# Best-effort context estimate. Slash commands typically don't have
# transcript_path; hook-driven mechanical.json carries the real value.
hook_input_transcript = ""  # fill in if known; otherwise leave empty
ctx_pct, _conf = estimate_context_pct(hook_input_transcript) if hook_input_transcript else (0, "low")

intent_body = f"""\
---
schema_version: 1
trigger: manual
ts: <ISO timestamp now>
context_pct_estimated: {ctx_pct or 0}
task_slug: <task slug>
phase: <current phase>
supersedes: <previous trigger@ts, or none — see contract above>
---

<the body sections you wrote above>
"""
atomic_write_text(intent_path(task_path), intent_body)
append_jsonl_locked(history_path(task_path), {
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
    "task_slug": task_path.name,
    "task_path": str(task_path),
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
acknowledge(task_slug=task_path.name, via="manual_pause")
```

## Constraints

- **Don't archive** — task stays active in `.flow/.current-task`
- **Don't commit** — pause is for ungated WIP
