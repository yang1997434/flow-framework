# Changelog

## v0.3.1-alpha (2026-05-04 later)

Sandbox dogfood test exposed bugs + completed three stubs.

### Bug fixes from dogfood test

- **Phase detection in `user-prompt-submit.py`** — was matching template placeholder
  text ("main session", "promoted", etc.) and reporting fresh task as "done".
  Rewrote `determine_phase` to extract section-by-section and check for non-comment,
  non-template content. Verified across 5 progressive states (empty → done).
- **`progress.md.template`** — simplified to use clear `<!-- TEMPLATE: 未填写 -->`
  markers in each section. Phase detector now reliably distinguishes filled vs
  template state.
- **`flow_conflict.py` regex** — original directive matcher required polarity at
  start of sentence (after `^` / `.\s+` / list bullet). Fixed to use `\b` word
  boundary anywhere; added stop-word filter for subject overlap; lowered
  threshold from 0.5 to 0.4 (with min-based denominator for sensitivity).

### Stubs completed (P1 + P2)

- **`flow_staleness.py` (P1)** — full implementation:
  - Scans memory files for cited path patterns (`.py`, `.ts`, `.md`, etc.)
  - Verifies path existence (project-relative, repo-relative, absolute)
  - Cross-references with `git log -N` to detect "modified after memory written"
  - Outputs human-readable findings or JSON for hook consumption
  - Exits non-zero on stale findings (CI-integrable)
- **`flow_promote.py` (P2)** — criteria validation:
  - Counts archived task mentions, vault MOC mentions, active project mentions
  - Cap warnings (Letta-anchored: vault pattern <300 lines / rules <200 lines)
  - `--check-only` mode prints metrics without promoting
  - `--force` to override criteria; `--confirm-rule` required for Lv3 rules tier
  - Credential grep self-check before write
  - Updates source frontmatter with `status: promoted` + target path + date
- **`pre-tool-task.py` jsonl injection hook (P2)** — Trellis-style:
  - Triggers on `Task` / `Agent` tool calls
  - Reads active task's `implement.jsonl` or `check.jsonl` (heuristic by prompt content)
  - Loads referenced spec files, injects into sub-agent prompt as `additionalContext`
  - 50 KB total / 10 KB per-file caps to avoid context bloat
  - Best-effort: silent exit on missing jsonl

### Stubs completed (P3)

- **`flow_conflict.py`** — heuristic conflict detection:
  - Extracts directives (always/never/must/should + Chinese equivalents) from rules
  - Pairs across files where polarity is opposite + subject overlap ≥ 0.4
  - Outputs suspect pairs for human/Claude review (not auto-resolution)
  - JSON output for hook consumption; scope flag (project/vault/global/all)

### Added

- `flow conflict` subcommand in CLI dispatcher
- `LICENSE` (MIT)
- `docs/USAGE.md` — step-by-step install / first-time setup / daily workflow / troubleshooting / cross-machine sync

### Smoke test results (dogfood task: token-counter CLI)

End-to-end run of all 4 phases on `/tmp/flow-test-real`:
- ✅ flow init → skeleton + .gitignore + ~/.flow/credentials.local
- ✅ flow task create → prd.md + progress.md from templates
- ✅ Phase 1-4 simulation (manual fill of prd.md, then implement, verify, sediment)
- ✅ flow save → journal entry with title / machine_id / status / commits
- ✅ flow task archive → moved to archive/2026-05/
- ✅ All 5 hooks: silent on no-op, correct output on triggers
- ✅ All scripts: idempotent, exit codes correct

### Still deferred

- Triage uses heuristic, not actual Haiku call (slash command guides the model)
- Auto-save does not auto-update `~/.claude/projects/.../memory/MEMORY.md` (call `/save` skill manually)
- Conflict detection is heuristic (false positives expected); LLM-based verification deferred

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
