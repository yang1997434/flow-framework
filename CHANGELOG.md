# Changelog

## v0.4.0 (2026-05-04)

Major v0.4 release. Addresses 9 sub-projects identified in the v0.3.1 audit
(`05-04-audit-flow-issues`). Production goal: **断点能记 / 长期记忆能沉淀 / 踩坑能沉淀**.

### Highlights

- **Capability registry** — prompt files no longer hard-code skill names. 13 capabilities
  + 5 model roles, all overridable via `flow.config.local.yaml`. Skill churn → 1 yaml line.
- **Real-automation install** — `install.sh` declarative (`dependencies.json`-driven):
  registers marketplaces, installs plugins via `claude plugin install`, merges hooks into
  `~/.claude/settings.json` with isolated matcher entries (Issue #415 mitigation), runs
  `flow selftest` to prove it actually works.
- **Worktree-per-task (default `shared`, opt-in `worktree`)** — task isolation that doesn't
  poison the shared tree on cross-task switches. `flow status` / `flow switch` UX added.
- **Three-tier autosave** — Lv1 trickle (git commit / file touch append) / Lv2 phase-boundary
  / Lv3 distill-on-pause-or-stop with cooldown + heartbeat. Layer-1 raw persistence delegated
  to context-mode plugin (hard dep).
- **Ralph bash-loop** — Phase 2 alt mode for autonomous PRD-checklist runs. `scripts/flow_ralph.sh`
  reimplements the Anthropic ralph-wiggum pattern in bash to avoid Stop-hook collisions.
- **Skill diff hook** — SessionStart compares installed plugins to last snapshot, scores
  capability overlap (Szymkiewicz–Simpson), surfaces "consider replacing X with Y" suggestions.
- **Rule integration** — flow phases explicitly reference `~/.claude/rules/code-delivery.md`,
  `code-review.md`, `knowledge-base.md`; new `behavioral_guidelines` capability invokes
  Karpathy's guidelines at the implement boundary.

### Added

- `dependencies.json` — declarative manifest (system commands / marketplaces / plugins)
- `claude/capabilities/defaults.json` — built-in capability + model_role mapping
- `scripts/flow_capability.py` — resolver + template renderer (dotted access supported)
- `scripts/flow_install.py` — install orchestrator (5 subcommands + dry-run)
- `scripts/flow_doctor.py` — environment diagnostic incl. hook-isolation + context-mode check
- `scripts/flow_selftest.py` — functional verification (hooks dry-fire / init / task-roundtrip /
  plugins / rendered prompts / doctor recap)
- `scripts/flow_skill_diff.py` — capability overlap analysis with per-(spec, version) cache
- `scripts/flow_autosave.py` — Lv1/Lv2/Lv3 orchestrator (queue-based; LLM distill deferred to
  next interactive session, never invoked from hook context)
- `scripts/flow_ralph.sh` — Phase 2 ralph-loop wrapper (375 LOC, dry-run + fake-mode for tests)
- `claude/hooks/post-tool-edit.py` — Lv1 file-touch tracking with debounce
- `claude/hooks/settings.template.json` — replaces old `.snippet`; templated `{{REPO_ROOT}}`
- `tests/smoke/` — 73 unittest cases + 12 ralph bash-test cases (was: empty in v0.3.1)

### Changed

- `claude/skills/flow/*/SKILL.md` and `claude/commands/flow/*.md` — every concrete skill name /
  model name replaced with `{{capability:X}}` / `{{model:Y}}` placeholder; rendered at install
- `install.sh` — slim orchestrator over `flow_install.py`; auto-removes legacy symlinks before
  rendering; runs selftest at end and fails install if non-functional
- `claude/hooks/stop.py` — removed raw save (delegated to context-mode); now triggers Lv3 distill
  with 5-min cooldown
- `claude/hooks/post-tool-bash.py` — added Lv1 git-commit append (hash-uniqueness debounce);
  preserved credential grep
- `claude/hooks/session-start.py` — runs `flow_skill_diff.py diff --quiet` (best-effort, 8s
  timeout) and surfaces pending suggestions in injected context
- `scripts/flow_task.py` — worktree creation + `.location` file + `cmd_status` (tree view) +
  `cmd_switch` (eval-friendly cd output) + worktree cleanup on archive
- `templates/flow.config.yaml.template` — new fields: `task_isolation`, `phase2_mode`, `autosave`
- `templates/progress.md.template` — added YAML frontmatter (`slug` / `status` / `phase` /
  `blocked_by`) for `cmd_status` to read

### Fixed (P0 from audit)

- `claude/hooks/pre-tool-task.py:62` — removed `or True` debug leftover
- `scripts/flow_task.py:cmd_archive` — capture `was_current` BEFORE `shutil.move` (was: any
  archive cleared `.current-task` pointer regardless of which task was archived)
- `scripts/flow_promote.py:rewrite_frontmatter_for_promotion` — extracted as pure function;
  `strip()` instead of `rstrip()` to avoid blank-line accumulation across re-promotions

### Removed

- `claude/hooks/settings.json.snippet` — superseded by `settings.template.json`

### Pitfalls captured this cycle

1. **render write-through symlink** — first install attempt wrote rendered output back into
   source templates because `~/.claude/skills/flow/` was a legacy symlink to repo. Fixed by
   adding fail-loud detection in `flow_install.py render-prompts` + auto-unlink in `install.sh`.
2. **Sub-agent model selection** — dispatching with `model: sonnet` failed silently when that
   model wasn't accessible in the local environment. Now: don't specify model unless certain.
3. **`shutil.move` then query stale state** — pattern repeats across the codebase; archive
   logic in `flow_task.py` was the surface case but worth promoting to vault.

### Acknowledgments

v0.4 absorbed two external plugins as dependencies:
- [`mksglu/context-mode`](https://github.com/mksglu/context-mode) (Elastic 2.0) — Layer-1 raw
  session persistence + tool output sandboxing
- Anthropic's `ralph-wiggum` pattern — reimplemented in bash to avoid Stop-hook collisions

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
