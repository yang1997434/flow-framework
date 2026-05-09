# Flow dispatch telemetry — schema v1

> **Status**: frozen for v0.8.5. Field additions / removals require a
> schema_version bump + reader-side compat path.
> **Source**: `scripts/common/telemetry.py`.
> **PRD**: `.flow/tasks/05-08-v0.8.5-dispatch-telemetry-feedback-enrich/prd.md`
> §R1, §R2, §R3.

## Storage

* **Path**: `<task_dir>/telemetry.jsonl`
  (`task_dir = .flow/tasks/<slug>/`)
* **Format**: one JSON object per line, append-only via
  `safe_io.append_jsonl_locked` (fcntl.LOCK_EX).
* **Lifecycle**: archived alongside the task dir; no separate
  migration step.
* **Gitignore**: `.flow/tasks/**/telemetry.jsonl` (raw events live on
  the operator's disk only — never committed).

## Event shape (v1)

```jsonc
{
  "ts":               "<ISO8601 UTC, second precision, Z suffix>",
  "schema_version":   1,
  "task_slug":        "<slug from progress.md>",
  "round_num":        <int, 1-indexed>,
  "phase":            "worktree_create"
                    | "implementer"
                    | "reviewer"
                    | "gate_run"
                    | "codex_review",
  "duration_ms":      <int>,
  "outcome":          "pass" | "fail" | "skip" | null,    // FROZEN ENUM (codex review I2)
  "fail_reason_raw":  <string or null>,    // verdict / repr(exc) / state.last_reviewer_feedback (original verdict string preserved here when normalisation collapsed it to fail)
  "fail_category":    null,                 // reserved — v0.8.6+ classifier
  "worktree_id":      <string or null>      // null for events that pre-date worktree creation
}
```

### Field semantics

| Field             | v0.8.5 semantics |
|-------------------|------------------|
| `ts`              | UTC timestamp at event-emit time (ON span exit, not span entry). Second precision; no microseconds. |
| `schema_version`  | Always `1`. A v2 reader MUST be backward-compatible with v1 readers. |
| `task_slug`       | Copied from `RetrySessionState.task_slug` (= the task dir name). |
| `round_num`       | `state.dispatch_retry_rounds + 1` at event-emit time. Round 1 = first implementer dispatch. |
| `phase`           | One of the five PRD R3 phases. Tuple `telemetry.PHASES` is the enumerated source-of-truth. |
| `duration_ms`     | Wall-clock milliseconds inside the `timed_span` context. Includes wait time on subprocess / subagent dispatch. |
| `outcome`         | FROZEN: `pass` / `fail` / `skip` / `null`. Verbose verdict strings (`blocked`, `rejected_with_rationale`, `inconclusive`) are normalised by `telemetry.normalize_outcome()` — the original string is preserved verbatim in `fail_reason_raw`. `null` is reserved for phases that had no meaningful outcome (e.g. early infra failure where `set_outcome` never ran). |
| `fail_reason_raw` | Free-form text. Reviewer phase records `state.last_reviewer_feedback` or the verdict status; gate_run records `halted_at=<gate>`; worktree_create records `repr(exc)` from the underlying subprocess. |
| `fail_category`   | Reserved. v0.8.5 always `null`. v0.8.6+ classifier may populate (e.g. `manifest_violation`, `gate1_baseline_fail`, `codex_rejected`). |
| `worktree_id`     | Round 2+ events carry the round-discriminated worktree id (`<slug>+t<n>+r<N>+<sha>`). Round 1 + early phases may have `null`. |

## Five-phase coverage (PRD R3)

| Phase             | Wraps                                                         | Round 1?                  | Round 2+?    |
|-------------------|---------------------------------------------------------------|---------------------------|--------------|
| `worktree_create` | `git worktree add` inside `_dispatch_implementer_fresh_worktree` | n/a (already created)    | yes          |
| `implementer`     | `deps.run_implementer_round(...)` callable                    | yes (no-op for prod adapter) | yes       |
| `reviewer`        | `deps.run_codex_review(...)` callable (full review wall time) | yes                       | yes          |
| `gate_run`        | `gate_runner.run_phase2(...)` inside `_prod_review`           | yes                       | yes          |
| `codex_review`    | Emitted ONCE when reviewer outcome is `rejected_with_rationale` | only when codex consulted | only when codex consulted |

## Failure-mode invariants

* `emit_event` **never raises**. Any exception (`OSError`, lock
  timeout, type error) is swallowed; a `WARN:` line goes to stderr
  and `telemetry.swallow_count()` is incremented.
* Telemetry emit must NEVER block the dispatch loop. The whole
  module is observability-only.
* Disabled (`contract.dispatch.telemetry: off`) → no file is created;
  no swallow counter increment.

## Known limits

* **Hunk header quality** (relevant to `feedback_enrichment`, not
  `telemetry`): Python / JS / TS produce informative `@@` headers via
  git's default textconv; JSON / YAML / Markdown / config files
  typically yield empty or noisy headers.
* **Same-second collisions**: two events emitted in the same second
  share `ts`. Order is preserved by JSONL append order; downstream
  aggregators MUST NOT sort solely by `ts`.
* **Cross-task aggregation**: not built in v0.8.5 — operators run
  ad-hoc `find .flow/tasks -name telemetry.jsonl`.
* **Untracked nested files inside new directories** are reported as
  the parent directory entry only (`?? dir/` from
  `git status --porcelain`); per-file structural map for new
  directories is degraded. Tracked in v0.8.6 backlog as I3-B
  (see `.flow/v0.8.5-known-limits.md`).
* **Performance**: a single fcntl-locked append + JSON serialise.
  Each phase event adds ≤ 1 ms of orchestrator overhead in the steady
  state. The retry loop emits at most 5 events per round × N rounds.

## Schema evolution policy

| Change                                | Allowed in v0.8.5? |
|---------------------------------------|--------------------|
| Add a new phase string                | NO — schema v2     |
| Add a new field                       | NO — schema v2     |
| Remove or rename a field              | NO — schema v2     |
| Change semantics of an existing field | NO — schema v2     |
| Populate `fail_category` (currently `null`) | NO — schema v2 (the `null` sentinel is part of the v1 contract) |

For v0.8.6 a fail-category classifier is on the roadmap; the v1
schema reserves the `fail_category` field name so the v2 bump is
additive in spirit (existing readers continue to read the field as
either `null` or a string).
