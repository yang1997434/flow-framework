# Ralph bash-loop wrapper

> Created: 2026-05-04
> Slug: ralph-bash-loop
> Type: backend
> Complexity: moderate

## Goal

Phase 2 currently only has interactive mode (main session dispatches sub-agents). Add a third mode — `ralph-loop` — implemented as a self-contained bash script that re-invokes `claude --print` headlessly until the PRD's Acceptance Criteria checklist is fully ticked or a hard iteration cap fires. Avoids the official `ralph-wiggum` plugin because its in-session Stop hook conflicts with flow's `stop.py`.

## What I already know

- Research B (`.flow/tasks/05-04-audit-flow-issues/research/B-context-mode-ralph-loop.md`) confirms official ralph-wiggum cannot nest under flow sub-agents (Stop hook conflict).
- `coleam00/ralph-loop-quickstart` recommends bash loop over plugin for fresh-context isolation per iteration.
- v0.4 Phase 2 SKILL.md uses `{{capability:X}}` / `{{model:X}}` placeholders rendered at install time — must NOT modify those.

## Requirements

- New executable bash script `scripts/flow_ralph.sh`
- Args: `<task-slug>`, `--max-iterations N` (default 20), `--completion-promise STR` (default `RALPH_DONE`), `--dry-run`
- Reads `.flow/tasks/<date>-<slug>/prd.md` Acceptance Criteria checklist; `progress.md` for already-completed items
- Loop calls `claude --print --max-budget-usd 5 "<prompt>"` each iteration; logs to `~/.flow/.runtime/ralph-{slug}.log`
- Exit conditions: completion-promise string seen in output OR max iterations hit
- Failure of one iteration logs error and continues (doesn't abort loop)
- On finish, append a single Execute Log line to `progress.md`
- Must NOT pass any system prompt that would re-enter `flow:start` (avoid nested infinite loops)

## Acceptance Criteria

- [ ] `scripts/flow_ralph.sh` exists, executable, `set -euo pipefail`
- [ ] `--help` prints usage and exits 0
- [ ] `--dry-run` prints planned iterations without invoking `claude`
- [ ] Missing prd.md gives a friendly error (exit 1, message identifies path)
- [ ] `templates/flow.config.yaml.template` has `phase2_mode:` field documented
- [ ] `claude/skills/flow/flow-phase2-execute/SKILL.md` has new "Step 1.5 — Execution mode selection" section, all existing `{{capability:X}}` / `{{model:Y}}` placeholders unchanged
- [ ] `tests/smoke/test_ralph_loop.sh` exists, executable, all sub-tests pass
- [ ] No real call to `claude --print` in test suite

## Definition of Done

- bash -n syntax check on flow_ralph.sh and test_ralph_loop.sh
- Test runner exits 0 on success
- Phase 2 SKILL.md still contains every previous `{{...}}` placeholder

## Out of Scope

- Modifying install.sh / hooks / flow.py / flow_doctor.py
- Modifying capability registry defaults.json
- Real headless ralph runs (would burn tokens)

## Research References

- `.flow/tasks/05-04-audit-flow-issues/research/B-context-mode-ralph-loop.md`

## Technical Notes

- log dir: `~/.flow/.runtime/`
- Phase 2 SKILL.md placeholder list to preserve: `{{model:implement}}`, `{{model:review}}`, `{{capability:tdd}}`, `{{capability:cross_model_challenge}}`, `{{capability:ui_implement}}`
