# Changelog

## v0.5.4 (2026-05-04)

Patch release — single-fix for the SSH-only marketplace clone failure
flagged in PR #1's "Notes for reviewer" and reproduced on this machine
during v0.5.3 selftest.

### Fixed

- **`flow install` SSH→HTTPS fallback** — `cmd_register_marketplaces` and
  `cmd_install_plugins` now inject `GIT_CONFIG_COUNT/KEY/VALUE` env vars
  into their `claude plugin` subprocess calls, telling git (2.31+) to
  rewrite `git@github.com:` to `https://github.com/` for the duration
  of that subprocess only. Eliminates the "SSH host key not in
  known_hosts" failure path on machines without SSH keys configured.
  Zero impact on installs that already worked (the rewrite is a no-op
  when the URL is already HTTPS).

## v0.5.3 (2026-05-04)

Patch release with 6 fixes — 2 local L3-dogfood findings + PR #1 (path
placeholder fix from yang1997434) + 3 GitHub issues from other-machine
smoke testing.

### Fixed

- **PR #1** (path placeholders) — replaced 11 hardcoded `~/projects/flow-framework/`
  occurrences across 5 source files with `{{REPO_ROOT}}/` so the renderer
  in `flow_install.py` substitutes the real install path on every machine.
  Plus 2 regression checks in `flow_selftest.py`.
- **L-1 progress.md write race** — `post-tool-bash.append_commit_to_progress`
  and `post-tool-edit.upsert_files_section` both did unsync'd RMW on
  `progress.md`; concurrent fires could lose one section's update. Fix:
  added `safe_io.locked_text_rmw()` helper using fcntl.LOCK_EX; both
  hooks routed through it. New unit test verifies 8 threads × 25
  appends produce 200 distinct entries with no loss.
- **L-2 `/flow:pause` Step 6 task_path bug** — prompt did
  `Path(".flow/tasks") / Path(.current-task content)` but `.current-task`
  already contains the full relative path. Fixed to read directly.
- **Issue #6 `flow doctor` context-mode false-negative** — added a third
  positive signal: read `~/.claude/settings.json` `enabledPlugins` and
  match `context-mode@context-mode`. Doctor no longer warns on green
  installs.
- **Issue #3 `flow task archive` slug + finish ordering** — `archive`
  now strips `^\d{2}-\d{2}-` prefix so both dated and bare-slug forms
  work. `finish` no longer clears `.current-task` (leaves it for
  `archive`). The natural copy-paste from `flow task list` now works.
- **Issue #2 `/flow:codex-review` non-git fallback** — detects whether
  cwd is a git repo and falls back to `codex exec --skip-git-repo-check -`
  with a content-built prompt when not. Restores the "any project" promise.

### Tests

- Suite: 135 → 143 (+4 from `LockedTextRmw` test class, +4 from new
  `test_flow_task_cli.py` covering Issue #3 archive slug + finish
  ordering).
- Selftest now rejects rendered files containing `{{REPO_ROOT}}` or
  `projects/flow-framework` — regression guard from PR #1.

## v0.5.2 (2026-05-04)

Patch release fixing a test-isolation bug.

### Fixed

- **Hook integration tests leaked state into real `~/.flow/.runtime/`** — 3 of
  the 4 v0.5 tests (`test_v05_precompact_hook.py`, `test_v05_e2e.py`,
  `test_v05_sessionstart_compact.py`) invoked hook scripts via
  `subprocess.run` without an `env=` override, so the hook scripts read
  `FLOW_HOME` from the parent process and wrote test-task nudge-state
  files (`nudge-state-01-01-demo.json`, `nudge-state-01-01-e2e.json`) into
  the user's actual runtime dir. Discovered during v0.5.1 framework
  validation. Fix: pin `FLOW_HOME` to a per-test tempdir in each
  `subprocess.run` `env=` arg, mirroring `test_v05_postool_integration.py`'s
  `_isolated_runtime_env` pattern.

### Tests

- All 3 fixed tests now create a `runtime` tempdir in setUp and pass it via
  `env=` to subprocess hook invocations. tearDown cleans up.
- Suite total still 135 (no test added; isolation is internal).

## v0.5.1 (2026-05-04)

Patch release covering two bugs found during v0.5.0 final validation
that were deferred to a follow-up.

### Fixed

- **`credential_grep` `(?i)` regex prefix** — GNU `grep -E` treats `(?i)` as
  a literal optional group, breaking the intended case-insensitive scan.
  The `-i` flag (already passed) provides case-insensitivity correctly.
  Local dev environments with `ugrep` aliased as `grep` masked the bug.
- **Selftest dry-fire fixtures** for `pre-compact.py` and `post-tool-edit.py`
  — these v0.5 hooks were validated by `flow doctor` isolation check +
  live invocation, but absent from `flow_selftest.py::HOOK_FIXTURES`.

### Tests

- Restored the credential-leak integration test case in
  `test_v05_postool_integration.py` that v0.5.0's polish work had to
  substitute due to the credential_grep regex bug.
- Smoke suite total: 134 → 135.
- Selftest hook dry-fire count: 5 → 7.

## v0.5.0 (2026-05-04)

Foundation for **auto-resume on context pressure**. Manual flow hardening
+ infra for v0.6.0 autopilot. Spec at `docs/specs/2026-05-04-auto-resume-design.md`.

### Highlights

- **PreCompact hook** — writes mechanical snapshot before Claude Code auto-compacts.
- **Per-task `.checkpoint/`** — `intent.md` + `mechanical.json` + `history.jsonl`
  capture in-flight state. `.gitignored` by default.
- **Atomic writes + fcntl.flock** — all v0.5+ state files go through `safe_io.py`.
  Concurrent appends to `history.jsonl` proven race-free under 8 threads.
- **Append-only hint outbox** — replaces single-file cascade hint that codex
  pre-merge review flagged as lossy.
- **Context-pressure nudge** — PostToolUse hook estimates context % from
  `transcript_path`, suggests `/flow:pause` once per compact cycle when ≥50%.
  Best-effort: model relays text, user sees in conversation.
- **Enhanced `/flow:pause`** — writes intent.md snapshot + cascade hint.
- **Enhanced `/flow:resume`** — reads checkpoint, surfaces Next Action,
  warns on staleness.
- **SessionStart on `compact`** — restores intent + mechanical context after
  auto-compact, model awaits user signal (no auto-execute).

### Added

- `scripts/common/safe_io.py` — atomic_write_text / atomic_write_json /
  append_jsonl_locked
- `scripts/common/hint_outbox.py` — write_hint / list_pending / mark_processed
- `scripts/common/context_estimator.py` — estimate_context_pct
- `scripts/common/checkpoint_paths.py` — per-task path helpers
- `scripts/common/mechanical.py` — build_payload (mechanical.json schema)
- `scripts/common/nudge.py` — maybe_nudge_text / acknowledge / rotate_window
- `claude/hooks/pre-compact.py` — PreCompact hook
- `tests/smoke/test_v05_*.py` — 8 new test modules including
  `test_v05_postool_integration.py` (subprocess-driven hook integration).
  Suite total: 73 → 133 cases.

### Changed

- `claude/hooks/post-tool-bash.py` — adds nudge + throttled mechanical update
- `claude/hooks/post-tool-edit.py` — adds nudge + throttled mechanical update
- `claude/hooks/session-start.py` — compact-matcher branch reads checkpoint
- `claude/commands/flow/pause.md` — Steps 6-8 (intent.md + hint + ack)
- `claude/commands/flow/resume.md` — Step 0 (personal /resume hint) + 1.5
- `scripts/flow_install.py` — `pre-compact.py` added to FLOW_OWNED_MARKERS
- `scripts/flow_init.py` — propagates `.checkpoint/` to project `.gitignore`
- `claude/hooks/settings.template.json` — PreCompact entry

### Not yet shipped (deferred to v0.6.0)

- `/flow:start --autopilot` and autopilot state machine
- R5 sanity check via external evidence (downgrade-only)
- Hard budgets (tool calls / files / time)
- Destructive-command denylist
- Explicit `done_when` checklist replacing completion-promise

These are designed in the spec but require dogfooding v0.5.0 first.

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
