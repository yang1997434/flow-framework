# Changelog

## v0.3.0-alpha (2026-05-04)

Initial extraction from vault into installable repo.

### Added

- Repo skeleton + bilingual README + install/uninstall scripts
- All 4 design docs mirrored from vault (`编码框架.md`, `Skills-Phase映射.md`, `框架对比.md`, `调研方法论.md`)
- 9 templates (prd / progress / pitfall / server / topology / ADR / pattern / config / config.local + gitignore snippet)
- 8 slash commands under `claude/commands/flow/` (start / continue / finish / resume / pitfall / promote / codex-review / pause)
- 5 skills under `claude/skills/flow/` (orchestrator + 4 phase skills)
- 4 hooks under `claude/hooks/` (session-start / user-prompt-submit / post-tool-bash / stop)
- Python helpers: `flow_init.py`, `flow_task.py`, `flow_save.py`, `flow_triage.py`, `flow.py` (CLI dispatcher)
- Common utilities: `paths.py`, `config.py`, `git.py`

### Status

- Foundation laid; not yet validated on real coding project
- Auto-checks (`flow_staleness.py`, `flow_conflict.py`, `flow_promote.py`) stubbed
- pre-tool-task.py jsonl injection hook stubbed (Trellis-style spec injection — not yet implemented)

### Known limitations

- Triage uses heuristic, not actual Haiku call (slash command guides the model to do classification)
- Auto-save writes to journal but does not auto-update `~/.claude/projects/.../memory/MEMORY.md` pointers (manual step)
- No staleness verification on session start (planned v0.3.1)
- No conflict pre-flight on rule load (planned v0.3.1)

## Predecessor: vault v0.2.1 (2026-05-04 earlier)

The design that this repo implements. See `docs/编码框架.md` CHANGELOG section for design history.
