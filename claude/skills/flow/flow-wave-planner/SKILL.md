---
name: flow-wave-planner
description: "Use when Phase 2 detects a wave-eligible plan (`### Tasks` block in progress.md). Decomposes plan into waves using mechanical disjointness + LLM concept-veto. Trigger: 'plan waves', 'decompose plan'."
---

# Flow Wave Planner — v0.7

Decompose a Phase 2 plan into ordered waves where each wave's tasks can run in parallel safely.

## When invoked

Phase 2 orchestrator invokes this skill after reading progress.md, when a `### Tasks` YAML block is present. The orchestrator hands you:
- `<task-slug>` — the task directory
- `controller_model` — current model identifier
- `cap` — concurrency cap (project config or default 3)

## Step 1 — Cache check (fast path)

```bash
python3 scripts/flow_wave_planner.py cache-check \
  --task-slug "<slug>" \
  --controller-model "<model>" \
  --cap "<cap>"
```

Exit 0 with cache JSON on stdout = cache hit, no recompute. Skip to Step 5.
Exit 1 = cache miss (stale or absent). Continue to Step 2.

## Step 2 — Parse plan + run mechanical disjointness

```bash
python3 scripts/flow_wave_planner.py decompose \
  --task-slug "<slug>" \
  --controller-model "<model>" \
  --cap "<cap>"
```

This produces a candidate wave structure based on:
- Contiguous-prefix wave packing
- Mechanical disjointness via `globs_overlap`
- SHARED_ARTIFACTS overlap → forced serial
- Broad-glob (`*`, `**`, `**/*`) → forced serial

Output is JSON to stdout: `{ "candidate_waves": [...], "rationale": [...] }`.

## Step 3 — LLM concept-veto pass

For each candidate wave with size > 1, scan the tasks' descriptions and read-paths. Apply the LLM concept-check (this is YOU, the controller).

**Veto signals** (any one → split the wave at that boundary):
- Tasks both modify the same orchestrator behavior in different files
- One task introduces a new type/contract; another task consumes a similar type
- One task is documentation; another implements the documented behavior (drift risk)
- Both tasks edit related capability registry semantics
- Tasks share an implicit invariant not expressed in writes/reads

For each vetoed pair, emit a rationale entry:
```json
{
  "pair": ["task-id-a", "task-id-b"],
  "verdict": "serial",
  "mechanical": "writes-disjoint",
  "llm_check": "FAILED — both tasks redefine error handling contract"
}
```

The split rule (round-3 absorbed): if you veto pair (a, b) within wave W, **do not just drop b** — emit the wave at the boundary so tasks after b also restart in plan order.

## Step 4 — Write cache

```bash
python3 scripts/flow_wave_planner.py write-cache \
  --task-slug "<slug>" \
  --controller-model "<model>" \
  --cap "<cap>" \
  --waves-json '<the JSON from step 2 + rationale from step 3>'
```

Cache lands at `.flow/tasks/<slug>/wave-decomposition.json` keyed by all 5 invalidation keys.

## Step 5 — Return waves to Phase 2

Return the wave list to Phase 2 orchestrator. It will dispatch each wave per Section 4 of the spec:
- Wave size 1 → existing v0.6 dispatch path via `{{capability:parallel_dispatch}}` + `{{capability:subagent_discipline}}` (single-implementer is just N=1 of the same orchestration)
- Wave size > 1 → `{{capability:wave_dispatch}}` (this version's runner)

## Failure modes

- Plan has no `### Tasks` block → return single wave containing the whole plan as one task (signals "main session implements")
- `### Tasks` block has malformed YAML → escalate to controller; do NOT auto-fix
- Cache file corrupt → log + recompute
- Any task missing `writes:` → that task forms its own wave (strict serial)

## Forbidden moves

- DO NOT promote serial tasks to parallel based on LLM intuition. LLM can only DOWNGRADE parallel → serial. Mechanical disjointness is the positive proof.
- DO NOT skip past a non-joiner. Contiguous-prefix is mandatory; round-3 spec lock.
- DO NOT modify the plan file. Cache is the only thing this skill writes.
