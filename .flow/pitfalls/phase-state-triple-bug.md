---
name: phase-state-triple-bug
date: 2026-05-05
project: flow-framework
severity: high
status: active
trigger_paths:
  - "claude/hooks/user-prompt-submit.py"
  - "scripts/flow_autosave.py"
  - "claude/hooks/stop.py"
  - ".flow/tasks/*/progress.md"
last_verified: 2026-05-05
---

# phase-state-triple-bug

## Symptom

`<flow-state>` hook reports `Current phase: done` for a task where Phase 1
hasn't even started (PRD empty, no user approval). User sees stale `done`
state, `/flow:continue` would skip to Phase 2 incorrectly.

Repro: start a fresh `/flow:start <task>`, let session close (Stop hook
fires once), then submit any new prompt — flow-state reports `done`.

## Root cause

Three bugs stack:

1. **Sediment Notes pollution**: `flow_autosave.py:append_distill_marker`
   wrote `- [TS] distill queued (trigger=stop)` lines into
   `progress.md ## Sediment Notes`.
2. **`is_section_filled` too lax**: stripped only HTML comments + blank
   lines; the autosave breadcrumb counted as user content.
3. **`determine_phase` not sequential**: returned `done` whenever Sediment
   Notes had any content, ignoring whether Plan/Execute/Verify were empty.

Stop hook fires on every session close → Sediment Notes immediately
non-empty → phase reports `done` from Phase 1 onward.

## Fix

- `flow_autosave.py`: route breadcrumb to
  `~/.flow/.runtime/autosave-log-<cwd>.md` (out-of-band of progress.md).
- `is_section_filled`: filter lines matching
  `^- \[\d{4}-\d{2}-\d{2} \d{2}:\d{2}\] distill queued` (defense-in-depth
  for old progress.md files).
- `determine_phase`: sequential AND-chain — Plan must be filled before
  Execute is checked, etc.

## Prevention

- Never write automation breadcrumbs into user-facing files that are
  consulted by state-determining functions. Use `~/.flow/.runtime/` instead.
- Phase determination must require **sequential** completion, not "biggest
  section filled wins".
- When designing a phase state machine, ask: "what writers besides the
  user can mutate the signal source?" — anti-tamper before relying on it.

## Why it matters

Sub-bug 1 alone caused user trust loss: every session close moved phase
to `done`, making `<flow-state>` actively misleading. Recurrence costs:
phase tracking is the framework's narrative spine — wrong phase →
wrong skill loaded → wrong agent dispatched.

## References

- Commit: (pending)
- Related: flow-protocol-needs-fallback-chain.md
