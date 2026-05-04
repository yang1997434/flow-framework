# progress.md — ctxmode-and-autosave

## Plan

<!-- TEMPLATE: 未填写。Phase 1 末由主 session 写入：sub-agent scope 划分（互不重叠）或 "(single, main session implements)"。 -->

## Execute Log

<!-- TEMPLATE: 未填写。Phase 2 渐进 append。每个 sub-agent / 主 session 完成一段工作时追加一行。 -->

<!-- 表格示例（首行为表头，自动生效）：
| 时间 (YYYY-MM-DD HH:MM) | Agent | Scope | Outcome |
|------|-------|-------|---------|
-->

## Verify Report

- **Architecture in production**: v0.4 Layer1+Layer2 ctxmode/autosave shipped (`2b23a5b`) and has been in continuous use since. v0.4.1 hardening (`8a2edc8`) addressed 5 P1 issues from pre-merge review.
- **Auto-trickle proven live**: 25 `distill queued` entries in this very `progress.md` (between 06:36 and 11:01) demonstrate the Lv3 cooldown + heartbeat queue mechanism works as designed. Heartbeat trigger fired once at 10:01 after 159 tool calls — cooldown collapse to once-per-window confirmed.
- **Tests**: 135/135 smoke pass on `master @ 62cb629` (v0.5.1 HEAD). v0.4-era tests (`test_autosave.py` etc.) all green. v0.5 work shipped on top without regression. Selftest hook dry-fire 7/7. Credential grep self-check pass (and `(?i)` portability bug fixed in v0.5.1 `6c9ea2c`).
- **Linting**: no formal Python linter wired in this project; pre-commit hook + manual review per commit.
- **Skipped**: `gstack:codex` review and `impeccable:audit` — task is backend (no UI) and v0.4/v0.5 work was already reviewed via the multi-Task subagent-driven flow (each task got spec + code-quality reviewers).

**Status: PASS — v0.4 architecture in active production use; v0.5 + v0.5.1 shipped on top.**

## Sediment Notes

### Layer 1 / Layer 2 split (v0.4 architecture)

- **Layer 1** = context-mode plugin. Owns PreCompact / PostToolUse / SessionStart raw capture into `~/.context-mode/content/` (SQLite). flow no longer writes a raw journal — `stop.py` was thinned out, `flow_save.py` is now legacy and unreferenced.
- **Layer 2** = flow `flow_autosave.py`. Three event-driven tiers:
  - **Lv1** (cost: 0): `post-tool-bash.py` appends `git commit` lines to `progress.md ## Commits`; `post-tool-edit.py` batches Edit/Write tool calls into `## Files Touched`. Both debounced (mtime + count thresholds).
  - **Lv2** (cost: low) — phase switches and decision moments. Not implemented in this batch; will live in `flow_promote.py` and the phase commands. Out of scope here.
  - **Lv3** (cost: medium) — `/flow:pause | /flow:finish | Stop hook | PreCompact` queue an entry in `~/.flow/.runtime/distill-queue.jsonl` and write a "distill queued" marker to `progress.md ## Sediment Notes`. **No LLM call inside the hook.**

### Why the Lv3 hook does NOT call an LLM

1. Hook timeout — Stop hook is 15s; PreCompact even tighter. An LLM round-trip can blow that.
2. Cost — each session-end firing an LLM costs $$$ even when the session was a no-op.
3. Idempotency — five triggers all map to "distill once," and a queue-then-flush model lets the cooldown window collapse them naturally.

### How a future "real distill" should be wired

The queue file (`~/.flow/.runtime/distill-queue.jsonl`) is the contract. Three candidate dispatchers, in order of cleanest-to-most-invasive:

1. **SessionStart hook** reads queue, surfaces "you have N pending distills since last session — run /flow:distill?" via system-reminder. Claude (or user) decides whether to run actual LLM. Lowest cost, highest signal.
2. **`/flow:save` slash command** — takes the queue, builds a prompt that loads `progress.md` + last 50 commits + (optionally) the matching context-mode raw transcripts, calls the model, appends the distilled summary to `## Sediment Notes`, drains the queue.
3. **Explicit `flow distill --run` CLI** — for headless / CI / cron contexts. Reads queue, runs via `claude --headless` or direct API.

Recommended path: **option 1 + option 2 together** — SessionStart surfaces; user/Claude triggers `/flow:save` which actually distills. Option 3 is escape hatch.

### Cooldown / heartbeat semantics

- Default `distill_cooldown_minutes = 5`: stop / precompact / heartbeat triggers within 5 min of last distill are silently dropped (counter still bumps).
- Explicit triggers (`pause`, `finish`, `manual`) **bypass cooldown** — user said so, do it.
- Heartbeat needs **both** `last_distill > 30 min ago` **AND** `tool_count >= 50` (AND, not OR — research phases that read 100 files but never invoke a save shouldn't get spurious distills, and a 30-minute thinking pause shouldn't either).
- `tool-count.txt` is reset on every distill (heartbeat or otherwise). No per-trigger counters.

### Issue #415 mitigation (settings.template.json)

Each PostToolUse matcher gets its own entry: Bash, Edit, Write are three separate matchers, not a combined `(Bash|Edit|Write)`. context-mode's installer does in-place edits of sibling matcher entries; sharing a matcher with context-mode's hooks would let its installer silently drop ours. Added Edit + Write entries (+2 vs the previous template).

### Files added / changed (this worktree)

- new: `scripts/flow_autosave.py`, `claude/hooks/post-tool-edit.py`, `tests/smoke/test_autosave.py`, `claude/hooks/settings.template.json` (worktree-local; mirrors main repo's parallel work)
- changed: `claude/hooks/stop.py` (deleted raw-save logic, queues Lv3 distill), `claude/hooks/post-tool-bash.py` (added Lv1 commit append + heartbeat bump, kept credential grep), `templates/flow.config.yaml.template` (autosave block), `scripts/flow_doctor.py` (context-mode check), `dependencies.json` (mirrored from main).

### Open follow-ups (not in this batch)

- Lv2 phase-switch save — needs a small LLM template + integration with `flow_promote.py`. Punt to a separate task.
- SessionStart "you have N pending distills" surfacing — punt to the SessionStart handoff task.
- The `/flow:save` slash command logic for actually draining the queue with an LLM call — that's the "real distill" follow-up.

### Postscript: v0.5 + v0.5.1 shipped on top (2026-05-04)

This task remained the active pointer while v0.5 (auto-resume / context-pressure hardening) and v0.5.1 (credential_grep + selftest fixtures patch) shipped on top. Out of scope for this PRD but operationally relevant:

- v0.5.0 = 19 commits, tag `v0.5.0`, [release](https://github.com/yang1997434/flow-framework/releases/tag/v0.5.0). Adds PreCompact hook + per-task `.checkpoint/` + atomic safe_io + context-pressure nudge + enhanced `/flow:pause` & `/flow:resume` + SessionStart compact-matcher.
- v0.5.1 = 3 commits, tag `v0.5.1`, [release](https://github.com/yang1997434/flow-framework/releases/tag/v0.5.1). Fixes `credential_grep` `(?i)` portability + selftest fixtures for new hooks.
- Polish queue (14 reviewer items) bundled into 3 commits before v0.5.0 release commit — preserves plan-as-spec discipline.
- Subagent-driven workflow (implementer → spec reviewer → code-quality reviewer per task) proven repeatable; documented in `session_latest.md` for v0.6 reuse.

**v0.5 design / impl artifacts**: `docs/specs/2026-05-04-auto-resume-design.md`, `docs/plans/2026-05-04-auto-resume-v0.5.0.md`. Spec was codex-reviewed pre-implementation; plan was self-reviewed.

**One bug class to remember (codex framing)**: prompt-driven optimism — don't build safety paths on "model will comply with instruction." Saved as separate persistent memory.

**v0.5.x follow-ups still open** (NOT v0.5.1):
- User-side `post-pr-review.sh` hook collision on PostToolUse[Bash] — Issue #415 risk. NOT a flow bug; user-side fix.

- [2026-05-04 06:36] distill queued (trigger=stop)

- [2026-05-04 06:50] distill queued (trigger=stop)

- [2026-05-04 06:55] distill queued (trigger=stop)

- [2026-05-04 07:05] distill queued (trigger=stop)

- [2026-05-04 07:14] distill queued (trigger=stop)

- [2026-05-04 07:20] distill queued (trigger=stop)

- [2026-05-04 07:26] distill queued (trigger=stop)

- [2026-05-04 07:34] distill queued (trigger=stop)

- [2026-05-04 07:41] distill queued (trigger=stop)

- [2026-05-04 07:51] distill queued (trigger=stop)

- [2026-05-04 08:03] distill queued (trigger=stop)

- [2026-05-04 08:12] distill queued (trigger=stop)

- [2026-05-04 08:19] distill queued (trigger=stop)

- [2026-05-04 08:26] distill queued (trigger=stop)

- [2026-05-04 08:31] distill queued (trigger=stop)

- [2026-05-04 08:40] distill queued (trigger=stop)

- [2026-05-04 08:49] distill queued (trigger=stop)

- [2026-05-04 08:58] distill queued (trigger=stop)

- [2026-05-04 09:05] distill queued (trigger=stop)

- [2026-05-04 09:31] distill queued (trigger=stop)

- [2026-05-04 10:01] distill queued (trigger=heartbeat) — after 159 tool calls

- [2026-05-04 10:18] distill queued (trigger=stop)

- [2026-05-04 10:28] distill queued (trigger=stop)

- [2026-05-04 10:51] distill queued (trigger=stop)

- [2026-05-04 11:01] distill queued (trigger=stop)

## Retro (optional)

<!-- TEMPLATE: 自由格式回顾——什么 worked / didn't / 框架反馈。可省略。 -->

## Files Touched

_Updated 2026-05-04 11:02 (last 20 unique edits)_:

- `.flow/tasks/05-04-ctxmode-and-autosave/progress.md`
- `/home/yangpeng/.claude/projects/-data-Claude/memory/session_latest.md`
- `CHANGELOG.md`
- `VERSION`
- `scripts/flow_selftest.py`
- `tests/smoke/test_v05_postool_integration.py`
- `claude/hooks/post-tool-bash.py`
- `tests/smoke/test_v05_e2e.py`
- `.gitignore`
- `tests/smoke/test_v05_sessionstart_compact.py`
- `tests/smoke/test_v05_safe_io.py`
- `claude/commands/flow/pause.md`
- `scripts/common/safe_io.py`
- `scripts/common/nudge.py`
- `claude/hooks/post-tool-edit.py`
- `claude/hooks/session-start.py`
- `scripts/flow_init.py`
- `claude/commands/flow/resume.md`
- `tests/smoke/test_install_logic.py`
- `scripts/flow_install.py`

## Commits

- [2026-05-04 06:42] `d559cef` fix: complete v0.3.1 — bugs found in dogfood + 3 stubs done

- [2026-05-04 06:50] `2b23a5b` release: v0.4.0 — capability registry + automated install + worktree-per-task + autosave layer

- [2026-05-04 07:13] `8a2edc8` fix: v0.4.1 P1 hardening — 5 issues from pre-merge review

- [2026-05-04 08:49] `f9c3dd8` docs: spec for auto-resume on context pressure (v0.5/v0.6 split)

- [2026-05-04 09:02] `d3676e9` docs: implementation plan for v0.5.0 auto-resume foundation

- [2026-05-04 09:15] `6b64f01` feat(v0.5): add safe_io — atomic writes + fcntl.flock

- [2026-05-04 09:21] `94273f2` feat(v0.5): add hint_outbox — append-only hint queue

- [2026-05-04 09:25] `fdcfa74` feat(v0.5): add context_estimator — coarse context % from transcript

- [2026-05-04 09:28] `8057ac7` feat(v0.5): add checkpoint_paths — per-task .checkpoint/ helpers

- [2026-05-04 09:34] `3c949a6` feat(v0.5): add mechanical — build mechanical.json payload

- [2026-05-04 09:41] `b75b6ac` feat(v0.5): add PreCompact hook

- [2026-05-04 09:45] `d4b69e7` feat(v0.5): install PreCompact hook via settings template

- [2026-05-04 09:51] `b66bf3a` feat(v0.5): nudge helper + post-tool-bash extension

- [2026-05-04 10:00] `9933f1d` feat(v0.5): post-tool-edit nudge + mechanical mirror

- [2026-05-04 10:07] `34bf0ac` feat(v0.5): SessionStart compact-matcher injects resume context

- [2026-05-04 10:13] `10b47e7` feat(v0.5): /flow:pause writes intent.md + outbox hint

- [2026-05-04 10:20] `7db4aa8` fix(v0.5): substitute {{REPO_ROOT}} in slash command prompts

- [2026-05-04 10:23] `768ce8c` feat(v0.5): /flow:resume reads .checkpoint/ + staleness assessment

- [2026-05-04 10:26] `e898430` feat(v0.5): flow init propagates .checkpoint/ to project .gitignore

- [2026-05-04 10:34] `f566163` polish(v0.5): apply reviewer feedback on hooks + nudge

- [2026-05-04 10:35] `e9cfd2d` polish(v0.5): tighten /flow:pause prompt clarity

- [2026-05-04 10:38] `d59d9e7` polish(v0.5): rename test, add integration + staleness tests

- [2026-05-04 10:44] `504f9a2` chore: bump VERSION 0.4.0 → 0.5.0 + CHANGELOG

- [2026-05-04 10:47] `992d59e` test(v0.5): end-to-end pause → simulated compact → resume

- [2026-05-04 10:56] `6c9ea2c` fix(v0.5.1): drop (?i) prefix from credential_grep pattern

- [2026-05-04 10:57] `8c0b2f2` fix(v0.5.1): add selftest fixtures for new v0.5 hooks

- [2026-05-04 10:57] `62cb629` chore: bump VERSION 0.5.0 → 0.5.1 + CHANGELOG
