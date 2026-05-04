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

<!-- TEMPLATE: 未填写。Phase 3 末写。各项必须有具体值（pass / fail / 跳过原因），不能留 pending。 -->

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

- [2026-05-04 06:36] distill queued (trigger=stop)

## Retro (optional)

<!-- TEMPLATE: 自由格式回顾——什么 worked / didn't / 框架反馈。可省略。 -->

## Files Touched

_Updated 2026-05-04 06:47 (last 13 unique edits)_:

- `tests/smoke/test_install_logic.py`
- `scripts/flow_selftest.py`
- `claude/hooks/post-tool-bash.py`
- `scripts/flow_install.py`
- `.gitignore`
- `CHANGELOG.md`
- `VERSION`
- `claude/skills/flow/flow-phase3-finish/SKILL.md`
- `claude/skills/flow/flow-phase4-sediment/SKILL.md`
- `claude/skills/flow/flow-phase2-execute/SKILL.md`
- `claude/skills/flow/flow-orchestrator/SKILL.md`
- `dependencies.json`
- `claude/capabilities/defaults.json`

## Commits

- [2026-05-04 06:42] `d559cef` fix: complete v0.3.1 — bugs found in dogfood + 3 stubs done
