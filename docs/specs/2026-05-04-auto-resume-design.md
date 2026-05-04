---
title: Auto-Resume on Context Pressure — Design Spec
date: 2026-05-04
status: draft (awaiting user review)
target_versions: v0.5.0 (foundation) + v0.6.0 (autopilot)
---

# Auto-Resume on Context Pressure

## Problem

Today, when a long Flow task hits Claude Code's auto-compact (or fills context to
the brink), the model loses fresh in-flight intent — "what was I doing,
what's the next step, what plan was in my head." Recovery on the next turn
relies on the model re-reading `progress.md` + commits and re-deriving intent,
which is lossy and time-consuming. Manual `/flow:pause` exists but is too
coarse: by the time the user remembers to call it, context may already be
degraded.

User priority: **C (autonomous resume across compact) > A (recovery fidelity) > B (fewer manual steps)**.

## Non-goals

- Replace Lv1 trickle (commits, file touches) — those keep working as-is.
- Replace Lv2 phase-boundary or Lv3 distill markers — those keep working.
- Build a generic agent monitoring framework — this is Flow-specific.
- Solve sub-agent (Task tool spawn) context tracking — out of scope (no hooks
  inside Task-tool sub-agents).

## Architecture

Two-version split (driven by an external review that flagged the original
single-shot design as "prompt-driven optimism with logs"):

- **v0.5.0** — Foundation + manual flow hardening. Adds the safety
  infrastructure (atomic writes, file locks, hint outbox), PreCompact hook,
  per-task checkpoint files, enriched `/flow:pause` and `/flow:resume`,
  best-effort context-pressure nudge, cascade hint to user's personal
  `/save` skill. **No autopilot.** SessionStart on `compact` restores
  context but does not auto-execute.

- **v0.6.0** — Autopilot. Layers `/flow:start --autopilot`, hard budgets
  (tool calls / files / time / destructive-command denylist),
  `autopilot-state.json` state machine, R5 sanity check (downgrade-only —
  cannot vote to continue), and explicit `done_when` checklist (no fragile
  string matching). **Only ships after v0.5.0 has dogfooded for 1-2 weeks.**

The split is intentional: codex review noted that C1 autopilot built on
top of model self-compliance is unsafe for code-modifying work. v0.5 ships
the parts that stand on real OS guarantees (filesystem, locks, hooks).
v0.6 layers on autopilot only after v0.5 has proven reliable in dogfood.

### Component map (v0.5.0)

```
                         ┌─────────────────────────────────────┐
   /flow:start          │  <task>/.checkpoint/                 │
                        │  ├── intent.md       ← /flow:pause   │
                        │  ├── mechanical.json ← PreCompact +  │
                        │  │                    PostToolUse    │
                        │  └── history.jsonl   ← all events    │
                        └─────────────────────────────────────┘
                                          │
                                          ▼
   PreCompact hook ──────► writes mechanical.json (S1, no LLM)
   (fires before                  ↑
    auto-compact)                 │ also written by PostToolUse
                                  │ on threshold (60s throttle)
                                  ▼
   SessionStart on        ──► reads intent.md + mechanical.json,
   `compact` matcher          injects resume context (C2: model
                              waits for user signal — no auto-execute)

   PostToolUse hook ─────► context_estimator(transcript_path)
   (after every tool call)    if pct >= 50 and not nudged this cycle:
                                inject best-effort nudge instruction
                                update mechanical.json (throttled)

   /flow:pause ──────────► writes intent.md (model self-writes)
                           writes hint to ~/.flow/.runtime/hints/
                           personal /save consumes the hint later

   /flow:resume ─────────► (optionally) invokes personal /resume first,
                           then reads .checkpoint/, presents intent
                           + staleness assessment to user
```

### Component map (v0.6.0 additions)

```
   /flow:start --autopilot ──► writes autopilot-state.json

   PreCompact hook ─────► (NEW: directly fork python autopilot-checkpoint
                           script — no longer relies on model to comply
                           with injected instruction)
                           runs S2 distill + R5 sanity check
                           updates autopilot-state.json (compacts_used += 1)
                           if rails tripped → autopilot.active = false

   /flow:autopilot-checkpoint ──► same script as above, also callable
                                  manually for debug

   R5 sanity check ─────► uses EXTERNAL EVIDENCE only (git diff, test
                          status, PRD checklist), NOT model self-eval
                          downgrade-only: can vote stop, cannot vote continue
```

## Data model

All state files use JSON (with frontmatter for `.md`) and carry `schema_version`.
All writes are atomic (temp-file + fsync + rename). All concurrent appends use
`fcntl.flock`. `.checkpoint/` is `.gitignored` by default; users may opt in to
commit `intent.md` if they want it in PR diffs.

### `<task>/.checkpoint/intent.md` (v0.5.0 schema, v0.5.0 writes manual only)

LLM-quality "current state" snapshot. ~1000-token soft cap.

**v0.5.0 writes**: only `/flow:pause` writes this file. `trigger` is always
`manual` in v0.5.

**v0.6.0 writes**: autopilot also writes via PreCompact hook subprocess. New
`trigger` values `auto-checkpoint` (autopilot routine) and `autopilot-bail`
(autopilot tripped a rail) become valid. The schema below carries those
values from v0.5 to avoid a frontmatter migration in v0.6.

```markdown
---
schema_version: 1
trigger: manual | auto-checkpoint | autopilot-bail
ts: 2026-05-04T15:30:00+08:00
context_pct_estimated: 50
task_slug: 05-04-ctxmode-and-autosave
phase: phase-2-execute
supersedes: <previous trigger and ts, or "none">
---

## Current Intent
<200-300 words: what I'm working on right now>

## Next Action
<one concrete step: file path, function name, exact command>

## Mental Model
<plan in head: remaining steps, decision rationale, assumptions>

## Blockers
<external waits / blockers; may be empty>

## Dont-Forget
<small details easily lost — e.g. "codex review left 5 nits, unanswered">
```

Single file, overwritten on each write. Manual writes have priority via the
`trigger` field; resume reads the file as-is and uses `trigger` to decide
whether to fully trust the snapshot.

### `<task>/.checkpoint/mechanical.json` (v0.5.0)

Mechanical state, derived from existing data sources. No LLM cost.

```json
{
  "schema_version": 1,
  "ts": "2026-05-04T15:35:12+08:00",
  "trigger": "post-tool | precompact",
  "task_slug": "05-04-ctxmode-and-autosave",
  "phase": "phase-2-execute",
  "git": {
    "branch": "master",
    "head": "8a2edc8",
    "dirty_files": 2,
    "recent_commits": [
      {"hash": "8a2edc8", "subject": "fix: v0.4.1 P1 hardening — ..."}
    ]
  },
  "files_touched_recent": [
    "scripts/flow_install.py",
    "tests/smoke/test_p1_hardening.py"
  ],
  "context_pct_estimated": 55,
  "transcript_path_size_bytes": 524288,
  "estimator_confidence": "medium"
}
```

### `<task>/.checkpoint/history.jsonl` (v0.5.0)

Append-only audit log. Resume does not read this — debug / forensics only.

```jsonl
{"schema_version":1,"ts":"...","event":"checkpoint","trigger":"manual","ctx_pct":56,"intent_len_chars":847}
{"schema_version":1,"ts":"...","event":"nudge_emitted","ctx_pct":52,"acknowledged":false}
{"schema_version":1,"ts":"...","event":"nudge_acknowledged","via":"manual_pause"}
{"schema_version":1,"ts":"...","event":"precompact","mechanical_only":true,"ctx_pct":89}
```

### `~/.flow/.runtime/hints/<ts>-<seq>.json` (v0.5.0, append-only outbox)

Cascade hint to L3 (personal `/save`). One file per pause event. Files in
`hints/` are pending; consumer moves to `hints/processed/` after success.

```json
{
  "schema_version": 1,
  "task_slug": "05-04-ctxmode-and-autosave",
  "task_path": "/data/Claude/flow-framework/.flow/tasks/05-04-...",
  "phase": "phase-2-execute",
  "last_action": "ran v0.4.1 hardening regression tests, 95/95 green",
  "next_action": "brainstorm + spec auto-resume design",
  "ts": "2026-05-04T15:30:00+08:00",
  "pause_trigger": "manual"
}
```

Outbox semantics replace the original "single hint file" design (codex
flagged the single-file approach as lossy under concurrent writes).

### `~/.flow/.runtime/nudge-state-<task-slug>.json` (v0.5.0)

Per-task nudge tracking. Keyed by task slug (not cwd hash) so multiple tasks
in the same project don't collide.

```json
{
  "schema_version": 1,
  "task_slug": "05-04-ctxmode-and-autosave",
  "current_window_id": "cycle-2026-05-04T14:00:00",
  "last_nudge_ts": "2026-05-04T15:00:00+08:00",
  "last_nudge_ctx_pct": 50,
  "acknowledged": false,
  "acknowledged_via": null
}
```

`current_window_id` rolls over on SessionStart `compact` matcher fire —
new compact cycle = new window = re-arm nudge.

### `<task>/.checkpoint/autopilot-state.json` (v0.6.0 only)

```json
{
  "schema_version": 1,
  "active": true,
  "task_slug": "05-04-ctxmode-and-autosave",
  "started_at": "2026-05-04T14:00:00+08:00",
  "max_compacts": 3,
  "compacts_used": 1,
  "done_when": [
    {"id": "tests-green", "check": "bash tests/smoke/run.sh", "expected_exit": 0},
    {"id": "readme-mentions-feature", "check": "grep -q 'auto-resume' README.md"}
  ],
  "rails_status": "ok | warn | tripped",
  "tool_call_budget": 500,
  "tool_calls_used": 142,
  "wallclock_budget_minutes": 480,
  "destructive_denylist": ["rm -rf", "git reset --hard", "git push --force"],
  "last_sanity_check": {
    "ts": "2026-05-04T15:30:00+08:00",
    "external_evidence": {
      "in_prd_scope": true,
      "tests_passing": true,
      "checklist_progress": "2/5"
    },
    "verdict": "continue | downgrade",
    "concern": null
  }
}
```

`done_when` replaces the v0.4-era `--completion-promise STR` — explicit checks
instead of fragile string matching.

`tool_call_budget` / `wallclock_budget_minutes` enforced by autopilot-checkpoint
script. `destructive_denylist` enforced by PreToolUse hook (ships in v0.6).

## Components

### v0.5.0 components

#### A. Context % estimator (`scripts/common/context_estimator.py`)

Pure function. Inputs: `transcript_path`. Outputs: `(pct: int | None, confidence: str)`.

Algorithm:
```python
raw_size = os.path.getsize(transcript_path)
estimated_tokens = raw_size / 4   # rough JSONL→token approximation
model = detect_model_from_transcript(transcript_path)
limit = MODEL_LIMITS.get(model, 200_000)
pct = min(100, round(estimated_tokens / limit * 100))
```

`MODEL_LIMITS` table:
```python
MODEL_LIMITS = {
    "claude-opus-4-7": 200_000,
    "claude-opus-4-7[1m]": 1_000_000,
    "claude-sonnet-4-6": 200_000,
    "claude-haiku-4-5-20251001": 200_000,
}
```

Confidence levels:
- `high` — file readable, model identified, size > 10KB
- `medium` — file readable, model identified, smaller files
- `low` — file unreadable, model unknown, or other ambiguity

Caller MUST treat `(None, "low")` as "skip this trigger" (do not false-fire).

**Acknowledged limitation**: estimate is a coarse trigger, NOT a safety
boundary. Real context fill may differ from estimate by ±20% due to JSON
metadata overhead, tool payload escaping, etc. Calibration data will be
collected during v0.5.0 dogfood (see Testing).

#### B. PreCompact hook (`claude/hooks/pre-compact.py`)

NEW. Triggered before Claude Code auto-compacts. Stdin: hook JSON with
`cwd`, `transcript_path`.

Behavior:
1. Find active task via existing `find_active_task` helper
2. Compute mechanical snapshot (call existing helpers + estimator)
3. Atomic write `<task>/.checkpoint/mechanical.json` (trigger=`precompact`)
4. Atomic append to `history.jsonl` with `event: precompact`
5. Exit 0 (never block compact)

Hook timeout budget: 5s. New entry in `settings.template.json`, single
matcher entry per Issue #415.

#### C. PostToolUse hook extensions

Existing `post-tool-bash.py` and `post-tool-edit.py` get a NEW responsibility
(in addition to current heartbeat / commit-trickle):

1. Call `context_estimator(transcript_path)`
2. If `pct >= 50` AND `nudge-state.acknowledged == false` for current
   `window_id` AND `(now - nudge-state.last_nudge_ts) >= 60s`:
   - Update `nudge-state.json` (atomic write)
   - Inject `additionalContext`:
     ```
     <flow-checkpoint-suggested priority="medium" cycle="<window_id>">
     Context usage estimated at <pct>% (transcript size ~<bytes> bytes;
     estimator confidence: <conf>). Best moment to checkpoint while model
     is still clear.

     Tell the user verbatim before any other content (only once per session):
     > 💾 上下文已到 <pct>%。建议 /flow:pause 存档，新 session 跑 /flow:resume 续上。

     This is a soft hint — user may continue if they prefer. Do not interrupt
     in-flight tool sequences; surface at the next natural pause.
     </flow-checkpoint-suggested>
     ```
3. Throttled write to `mechanical.json` (only if last write > 60s ago)
4. Heartbeat / commit-trickle: unchanged

**Acknowledged best-effort**: nudge depends on the model relaying the text.
v0.5 dogfood will measure relay rate. If poor, v0.6 may add OS-level
notification via `PushNotification` tool (instructed in the same nudge text).

#### D. SessionStart on `compact` extension

Existing `claude/hooks/session-start.py`. Three matchers (`startup`, `clear`,
`compact`) currently share one handler. Extend the handler:

1. Detect matcher (input field `trigger` distinguishes them)
2. Existing behavior unchanged: quick guide + active task + pitfalls + skill diff
3. NEW (only on `compact` matcher with active task):
   a. Read `<task>/.checkpoint/intent.md` if present
   b. Read `<task>/.checkpoint/mechanical.json` if present
   c. Compose injection block:
      ```
      <flow-resumed-from-compact>
      ## Last Intent (from intent.md, ts=<ts>, ctx≈<pct>%, trigger=<trigger>)
      <intent.md body, full or truncated to 1500 tokens if larger>

      ## Latest Mechanical State (from mechanical.json, ts=<ts>)
      - Branch: <branch> @ <head>
      - Recent commits: <list>
      - Files touched recent: <list>

      ## Resume Mode
      MANUAL — present briefing to user, await their direction.

      ## Staleness
      [if mechanical.ts > intent.ts + 5min:]
      ⚠️ Mechanical state is <N> minutes newer than intent. Review commits
         + file edits before assuming intent is still fresh.
      </flow-resumed-from-compact>
      ```
   d. Append `history.jsonl` event `resumed_from_compact, mode=manual`
4. Window rollover: write a new `current_window_id` to nudge-state files for
   any active task (re-arms nudge after compact)

If `.checkpoint/` doesn't exist (e.g., task created before v0.5): skip the
new injection silently.

#### E. `/flow:pause` extension

Add to existing protocol after current 5 steps:

6. Compose intent.md content (model self-writes per template)
7. Atomic write `<task>/.checkpoint/intent.md` (trigger=`manual`)
8. Atomic write hint file `~/.flow/.runtime/hints/<ts>-<seq>.json`
9. Update `nudge-state.json` → `acknowledged=true, acknowledged_via=manual_pause`
10. Append history.jsonl: `event: checkpoint, trigger: manual`

Step 6 prompt for the model (in the slash command markdown):

> Write a focused intent snapshot covering: Current Intent (200-300 words),
> Next Action (one concrete step with file paths), Mental Model (your
> remaining plan), Blockers (or empty), Dont-Forget (small details).
> Total length ≤ 1000 tokens. Be specific over comprehensive.

#### F. `/flow:resume` extension

Add Step 0 + extend Step 4:

- **Step 0** (NEW): "Have you run personal `/resume` yet this session? If not,
  run it first for global state, then return to /flow:resume for task depth.
  If yes, skip to Step 1."
- **Step 1.5** (NEW): Read `<task>/.checkpoint/intent.md` and
  `mechanical.json`. Surface intent's "Next Action" prominently.
- **Step 4** (existing staleness check, ENRICHED): Compare
  `intent.ts` vs `mechanical.ts`. If gap > 5 min, surface staleness notice
  with concrete delta (commits, file edits since intent).

#### G. Cascade hint consumer integration (in user's personal `/save`)

The personal `/save` skill is owned by the user, not by Flow. We document
a 5-line addition to its Step 2c (write session_latest.md):

> Before composing Open Items, scan `~/.flow/.runtime/hints/` for any
> `*.json` files. For each: parse, include task / phase / next_action in
> Open Items, then move the file to `~/.flow/.runtime/hints/processed/`.

This is documented in the spec and as a comment in `flow_save.py` for
discoverability. Not enforced — works degraded if user doesn't add it
(hints just accumulate; v0.5 ships a `flow hint-prune` command for cleanup).

#### H. Atomic write / file lock library (`scripts/common/safe_io.py`)

NEW utility module. Public API:

```python
def atomic_write_text(path: Path, content: str, mode: int = 0o644) -> None:
    """Write content to path atomically (temp + fsync + rename)."""

def atomic_write_json(path: Path, obj: dict, indent: int = 2) -> None:
    """Atomic JSON write."""

def append_jsonl_locked(path: Path, record: dict, timeout_s: float = 2.0) -> bool:
    """Append one JSON line under fcntl.flock. Returns True on success,
    False on lock timeout."""
```

All state-file writes throughout v0.5+ MUST use these helpers. Ad-hoc
`open(path, 'w').write(...)` is banned.

#### I. Hint outbox library (`scripts/common/hint_outbox.py`)

Public API:

```python
def write_hint(payload: dict) -> Path:
    """Write a hint file with timestamp-seq filename, returns path."""

def list_pending() -> list[Path]:
    """Return all pending hints (in hints/, not in processed/)."""

def mark_processed(hint_path: Path) -> None:
    """Move processed hint into processed/ subdir."""
```

### v0.6.0 components (deferred — design only)

#### J. `/flow:start --autopilot`

New flag: `--autopilot --max-compacts N --done-when KEY=VALUE [...]`.
Writes `autopilot-state.json` at task creation.

#### K. PreCompact hook autopilot extension

The v0.5 PreCompact behavior (atomic write `mechanical.json`, append
`history.jsonl`) **always runs first**. Then, if `autopilot-state.json`
exists and `active: true`, the hook **additionally** forks the
autopilot-checkpoint Python script (subprocess.Popen, fire-and-forget).
**Does not** rely on injecting instruction for the model to comply.

The script:
1. Reads autopilot-state.json
2. Increments `compacts_used`; if >= max → `active=false, rails_status=tripped`,
   write hint, exit
3. Runs R5 sanity check using EXTERNAL EVIDENCE:
   - `git diff --stat origin/main...HEAD` — measure scope creep
   - `bash <test_command>` if defined — pass/fail
   - Parse PRD for checklist, count completed items
   - Check `tool_calls_used` vs budget
   - Check wallclock vs budget
4. If any external check fails → downgrade autopilot (`active=false`,
   `rails_status=warn`, write hint with concern)
5. Otherwise: write a templated `intent.md` with mechanical-derived
   summary + a "next steps" placeholder. NOT an LLM call (codex flagged
   that LLM-in-hook is wrong for autopilot critical path).

The model on resume sees the templated intent and can refine it during
its first turn. Codex's key insight: autopilot must work even if the model
is not cooperative.

#### L. Destructive-command denylist (PreToolUse hook addition)

When autopilot is active, intercept Bash tool calls. If command matches any
entry in `destructive_denylist`, block with `decision: "block"` and message:
"autopilot mode blocks <command>. Manual intervention required."

#### M. Hard budget enforcement

`tool_calls_used` tracked via PostToolUse. When >= `tool_call_budget`,
write hint and degrade autopilot.

Wallclock budget similar — checked at every PreToolUse.

## Data flow scenarios (final, v0.5.0)

### Scenario 1: Manual flow with nudge

```
t=0     /flow:start v0.5.0-feature
t=0+ε   SessionStart on startup: existing behavior

t=0..40min  user works, ctx <50%, no nudge

t=40min ctx crosses 50%:
        - PostToolUse hook: estimator returns 52, "high" confidence
        - nudge-state.json updated (atomic): acknowledged=false
        - additionalContext injected
        - mechanical.json updated (first time this cycle)
        - history.jsonl: event=nudge_emitted, ctx_pct=52
        Model on next turn relays:
          "💾 上下文已到 52%。建议 /flow:pause 存档..."

t=42min user keeps working (legitimate decision)
        - PostToolUse: ctx still rising; nudge already emitted this cycle, skip
        - mechanical.json throttled (60s since last); skip

t=55min user runs /flow:pause
        - existing 5 steps run
        - intent.md atomically written (trigger=manual)
        - hint file written to outbox: hints/2026-05-04T15:55:00-001.json
        - nudge-state: acknowledged=true, via=manual_pause
        - history.jsonl: event=checkpoint, trigger=manual

t=56min user runs personal /save
        - personal /save Step 2c reads hints/, finds 1 file
        - includes "Active flow task: ..." in Open Items
        - writes session_latest.md atomically
        - moves hint → hints/processed/2026-05-04T15:55:00-001.json

t=24h   new session, "继续"
        - personal /resume reads session_latest.md, sees flow task
        - briefing tells user: "Run /flow:resume to load task depth"
        - /flow:resume:
          * Step 0: detects personal /resume already ran, skip
          * Step 1.5: reads intent.md (yesterday 15:55, manual)
          * Step 4: intent.ts and mechanical.ts both yesterday (no staleness)
          * Surfaces "Next Action" to user
        - User OK, continues
```

### Scenario 2: Compact happens while user away (no manual pause)

```
t=0..70min  user works, ctx 0% → 88%, never paused

t=40min nudge fires as in Scenario 1; user not at keyboard, model has no
        natural turn to relay (still mid-tool-call sequence)

t=70min Claude Code triggers auto-compact
        - PreCompact hook: writes mechanical.json (trigger=precompact, ctx=88%)
        - history.jsonl: event=precompact

t=70min+ε  Compact happens. Context squeezed.

t=70min+ε+1  SessionStart on `compact` matcher:
        - Reads .checkpoint/intent.md → not present (user never paused)
        - Reads .checkpoint/mechanical.json → present (just written)
        - Composes injection: only Latest Mechanical State, no Last Intent
        - Resume Mode: MANUAL
        - Stale notice: "No intent snapshot. Inferring from progress.md
          + mechanical state."
        - Window rollover: nudge-state new current_window_id

t=70min+5  user comes back, types something
        - Model already has mechanical state in context
        - Model reads progress.md per its existing habit (Lv1 trickle has
          recent commits + file touches)
        - Reconstructs intent from data, presents briefing to user
        - User confirms direction, work resumes

       Lossy compared to Scenario 1 (no fresh intent), but recoverable.
```

## Error handling and degradation

| Failure | Behavior | Visibility |
|---------|----------|-----------|
| PreCompact hook crashes | exit 0; compact proceeds; no fresh mechanical | SessionStart `compact` shows "no mechanical snapshot this cycle" |
| Atomic rename fails (disk full) | write helper raises; caller catches and reports | `/flow:pause` shows error, user retries |
| Hint outbox write fails | logged to stderr; pause main flow continues | personal `/save` reads zero hints next time |
| Estimator returns `(None, "low")` | hook silently skips nudge | user gets no proactive reminder |
| `transcript_path` unreadable | estimator returns `(None, "low")` | same as above |
| SessionStart finds no `.checkpoint/` | falls back to existing behavior | unchanged for tasks created before v0.5 |
| Lock timeout on jsonl append | record dropped, stderr logged | minor audit gap, no functional impact |
| Schema version mismatch | reader raises with migration instructions | user sees clear error |
| Model doesn't relay nudge | user never sees it | manual `/flow:save` still works |

**Fail-closed principle**: where a state transition is ambiguous, prefer
"do nothing safe" over "guess and proceed." Codex flagged the original
design as "soft-degrading where it should hard-stop."

## Testing strategy

### Unit tests (`tests/smoke/test_v05_*.py`)

| File | Coverage |
|------|----------|
| `test_atomic_write.py` | rename atomicity, crash mid-write, partial-file detection |
| `test_outbox_queue.py` | concurrent writes, no lost updates, processed/ move |
| `test_context_estimator.py` | known transcript fixtures, ±5% accuracy claim, model detection |
| `test_hint_cascade.py` | /flow:pause writes hint → personal save reads → moves to processed/ |
| `test_precompact_hook.py` | input fixtures → mechanical.json schema validity |
| `test_sessionstart_compact.py` | with/without checkpoint files, additionalContext correctness |
| `test_intent_md_parse.py` | frontmatter + body section extraction |
| `test_staleness_detect.py` | mechanical.ts > intent.ts + 5min triggers warning |
| `test_flock_concurrent.py` | parallel jsonl appends, no record interleaving |
| `test_safe_io.py` | atomic_write helpers, lock helpers |

Target: ≥ 30 new test cases for v0.5.0.

### Integration tests

- end-to-end: `/flow:start` → simulate edits → `/flow:pause` → verify
  checkpoint files → simulate session restart with `compact` matcher →
  verify additionalContext injection format
- concurrent `/flow:pause` and PostToolUse mechanical update — both
  succeed without corruption
- multiple hint files in outbox processed in chronological order

### Manual dogfood (v0.5.0 → v0.6.0 gate)

Run on Flow's own development for 1-2 weeks. Collect:

1. **Nudge relay rate**: `history.jsonl` shows N nudges emitted. How many
   actually reached the user (manually counted from conversation logs)?
   Target: > 70% relay rate to consider nudge mechanism viable.
2. **Real compact reliability**: `history.jsonl` shows M `precompact` events.
   How many led to a clean SessionStart restoration? Target: 100%.
3. **Estimator calibration**: At each nudge event, user manually records
   actual context % (Claude Code shows this in some clients). Compare to
   estimator's `ctx_pct_estimated`. Compute median offset and adjust
   threshold default for v0.6.
4. **Race condition observations**: any `history.jsonl` corruption,
   missing hints, lock timeouts. Target: zero.

If dogfood reveals v0.5 is not robust enough (e.g., relay rate < 50%,
or race conditions surface), v0.6 must address before shipping autopilot.

## Migration

v0.4.x → v0.5.0:
- New PreCompact hook entry in `settings.template.json`. `flow install`
  picks it up automatically.
- New `<task>/.checkpoint/` directory created on first event for each
  task; older tasks unaffected until they trigger a checkpoint.
- `.gitignore` entry added: `.flow/tasks/*/.checkpoint/`. `flow init`
  ensures this on new projects; existing projects need a manual
  `.gitignore` line (documented in CHANGELOG).
- No breaking API changes.

v0.5.0 → v0.6.0:
- New `--autopilot` flag, opt-in only. No effect on tasks created without it.
- `autopilot-state.json` only present in autopilot tasks.
- Destructive-command denylist enforced via PreToolUse hook only when
  autopilot active for the active task.
- No breaking changes to v0.5 features.

## Open questions (parked for implementation phase)

- Exact format of `done_when` checks (v0.6) — `bash` command + expected exit
  vs declarative DSL.
- Whether to surface estimator confidence in the nudge text or hide it.
- Whether `/flow:resume` should auto-invoke personal `/resume` or only
  suggest it (depends on user preference — likely a config flag).

## Acceptance criteria

### v0.5.0 ships when

- [ ] All 5 v0.5 components (PreCompact hook, /flow:pause ext, SessionStart
      ext, /flow:resume ext, atomic-write/outbox libraries) implemented
- [ ] `safe_io.py` and `hint_outbox.py` covered ≥ 90% by unit tests
- [ ] All 30+ new test cases pass in CI
- [ ] PreCompact hook installed by `flow install` (Issue #415-clean entry)
- [ ] `.gitignore` for `.checkpoint/` propagated by `flow init`
- [ ] `flow doctor` reports v0.5 hook isolation status
- [ ] Manual end-to-end dogfood on this very repo: pause → compact → resume
      cycle works at least 3 times without intervention

### v0.6.0 ships when

- [ ] v0.5 dogfood targets met (relay > 70%, compact 100%, no races)
- [ ] All v0.6 components implemented per spec
- [ ] R5 sanity check uses external evidence, downgrade-only enforced
- [ ] Hard budgets respected in test scenarios
- [ ] Destructive denylist blocks listed commands in autopilot mode
- [ ] At least 1 multi-hour autopilot dogfood run successful (no corruption,
      clean degradation when needed)

## References

- Original brainstorm transcript: this conversation, 2026-05-04
- External design review: codex (gpt-5.x) consult session
  `019df335-f6fb-70a2-8dd4-acf1dc461318`
- Existing autosave architecture: `scripts/flow_autosave.py`
- Existing pause/resume: `claude/commands/flow/pause.md`, `resume.md`
- Personal save skill being cascaded to: `~/.claude/plugins/.../yangpeng-claude-skills/save/SKILL.md`
- Issue #415 (Claude Code hook isolation): tracked in `flow_install.py` markers
