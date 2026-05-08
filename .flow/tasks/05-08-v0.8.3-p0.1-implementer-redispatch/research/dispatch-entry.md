# P0.1 Research: Implementer Re-dispatch Architecture

**Date:** 2026-05-08  
**Scope:** `flow_orchestrator.py` + `dispatch_template.py`  
**File sizes:** orchestrator = 5805 lines, template = 235 lines

---

## Q1 — `auto_dispatch_task` (line 678)

**Signature:**
```python
def auto_dispatch_task(
    *, slug, task_idx, repo_root, dispatch_fn,
    contract, manifest, run_id, contract_path, contract_hash,
    integration_target="master", notifier=None,
) -> DispatchOutcome
```

**What it does (T10 orchestration shell):**

1. **Validation** (lines 735–762): contract, run_id, contract_hash, contract_path, manifest — all validated fail-loud before any disk write.
2. **Lock write** (line 801): `write_auto_prepare_lock(task_dir, lock)` — durable boundary for crash recovery. Lock pinned to `(run_id, task_id, contract_hash)`.
3. **Worktree creation** (lines 808–813): `create_task_worktree(repo_root, slug, task_idx, integration_target)` → `WorktreeContext`.
4. **`auto_engaged` event** (lines 817–836): 14-field schema written to `decisions.jsonl` before subagent invoked (Q7.2 invariant).
5. **Lock consume** (lines 843–845): `consume_auto_prepare_lock` after boundary marker is durable.
6. **Subagent dispatch** (line 865): `dispatch_fn(ctx, subagent_env=subagent_env, task_id=manifest.id)` — return value **INTENTIONALLY discarded** (PRD §1.2).
7. **Facts derivation** (line 867): `facts = derive_task_facts(ctx)` — authoritative state from disk, not from subagent return.
8. **T11 manifest verification** (lines 870–915): forbidden/out-of-scope check; blocked outcome if hit.
9. **Returns** `DispatchOutcome(status="ok"|"blocked", ctx=ctx, facts=facts, ...)`.

**Inputs summary:** slug + task_idx + repo_root + dispatch_fn (callable) + contract + manifest + run_id + contract_path + contract_hash + integration_target + notifier.

**Pure / idempotent?** NOT idempotent:
- Creates a git worktree on disk via `git worktree add -b <worktree_id> ...` (line 425). `worktree_id = f"{slug}+t{task_idx}+{shortsha}"` (line 421). Re-calling with the same inputs would hit `subprocess.CalledProcessError` because the branch/path already exists.
- Writes `auto_engaged` event to `decisions.jsonl`.
- Writes + consumes the `auto_prepare.lock` file.
- **Conclusion:** calling it a second time for the same task would fail at `git worktree add`.

---

## Q2 — `_invoke_subagent_dispatch` (line 5146) — Call Graph

```
_cmd_auto_execute (line 5425)
  └── auto_dispatch_task(dispatch_fn=_invoke_subagent_dispatch, ...)  [line 5544]
        └── dispatch_fn(ctx, subagent_env=..., task_id=...)           [line 865]
              └── _invoke_subagent_dispatch(ctx, **kw)                [line 5146]
                    └── import_module("flow_subagent_dispatch")        [line 5163]
                          └── mod.invoke(ctx, **kw)                   [line 5170]
```

`_invoke_subagent_dispatch` is a **dispatch shim**: it lazy-imports the T22 module (`flow_subagent_dispatch`) and calls `mod.invoke(ctx, **kw)`, propagating `subagent_env` and `task_id` kwargs. `auto_dispatch_task` calls it through the `dispatch_fn` parameter (not a direct call) — the indirection is the T19 test seam.

**Round 2+ path:**  
`dispatch_with_retry` (line 4937) calls `deps.run_implementer_round(state=state, prompt_prefix=prefix)` (line 5064). In production, this is `_prod_impl` (line 5312), which **does nothing** (returns `{}`). `_prod_impl` never calls `_invoke_subagent_dispatch`. There is no Round 2+ dispatch path today.

---

## Q3 — Option A vs B for Round 2+ Fresh Re-dispatch

### Option A: Re-call `auto_dispatch_task` inside `_prod_impl`

**Problems:**
- `auto_dispatch_task` always calls `create_task_worktree` → `git worktree add -b <worktree_id>`. The worktree_id is deterministic: `{slug}+t{task_idx}+{shortsha}`. Calling it twice for the same task with the same `shortsha` crashes at `git worktree add` (branch already exists).
- It re-writes `auto_engaged` event to `decisions.jsonl` — pollutes audit trail.
- It re-runs the full lock write/consume cycle — unnecessary overhead.
- **It cannot be called twice for the same task without cleanup first.**

### Option B: Extract `dispatch_implementer_in_fresh_worktree` helper

**Architecture alignment:**
- `_prod_impl` currently lives inside `_phase2_dispatch` (closure, lines 5312–5316). The test seam (`deps_factory`) already separates the implementer adapter from the retry loop — this is the intended extension point.
- `auto_dispatch_task` owns: lock, `auto_engaged` event, manifest verification. These belong to Round 1 only.
- A new helper needs only: create a new worktree (different `task_idx` or different `shortsha` suffix), invoke `_invoke_subagent_dispatch`, derive facts. No need for lock or `auto_engaged` re-emission.
- `_phase2_dispatch` already has `ctx`, `contract`, `manifest`, `run_id`, `task_id`, `task_dir`, `repo_root` available via its closure → `gate_runner` already uses them.

**Recommended: Option B.**

**Rationale:**
- Round 1 and Round 2+ have fundamentally different invariants. Round 1 owns the lock + event + manifest check; Round 2+ only needs a fresh worktree + implementer + facts. Putting both in `auto_dispatch_task` conflates these layers (S-class: wire-up gap risk — dispatch_fn wire-up isn't the right abstraction for retry).
- The `RetryDeps.run_implementer_round` abstraction is the documented extension point for exactly this (T3 + T18 comment at line 5300–5302: "v0.8.2 T18 will extend this to re-dispatch on retry rounds 2+").
- Extracting a small `_dispatch_implementer_fresh_worktree(*, repo_root, slug, task_idx, integration_target, run_id, task_id, subagent_env, prompt_prefix) -> TaskFacts` helper keeps Round 1 path unchanged (no refactor of `auto_dispatch_task`) and wires Round 2+ through `_prod_impl` by calling the helper.
- `task_idx` for Round 2+ can be `0 + round_number` or a synthetic offset to avoid worktree_id collision (the `shortsha` component already makes them unique per-HEAD, but `task_idx` must differ if HEAD hasn't moved).

**Import boundary:** Both `_invoke_subagent_dispatch` and `create_task_worktree` are module-level in `flow_orchestrator.py`. The new helper stays in the same file — no import boundary issues.

---

## Q4 — `build_implementer_prompt` and `is_first_pass` asymmetry

**Signature** (`dispatch_template.py` line 128):
```python
def build_implementer_prompt(
    *,
    task_brief: str,
    reviewer_feedback: Optional[str] = None,
    is_first_pass: bool = True,
    is_doc_only: bool = False,
) -> str
```

**Fields:**
- `task_brief` — always emitted (line 167). No conditional.
- `reviewer_feedback` — appended after `"---"` separator and `"## Reviewer feedback"` header (lines 172–176), only if truthy.
- `is_first_pass` — currently **reserved / unused** in the function body (lines 158–163: comment says "is_first_pass flag is reserved for future callers"; the only branch is `if not is_doc_only`). `is_first_pass=True` vs `False` produces identical output today.
- `is_doc_only` — skips K-class sentinel (lines 162–163).

**How Round 1 calls it** (`dispatch_with_retry` line 5058):
```python
prefix = build_implementer_prompt(
    task_brief="",             # ← EMPTY
    reviewer_feedback=redacted_feedback or None,
    is_first_pass=True,        # ← always True, for all rounds
    is_doc_only=False,
)
```

**Asymmetry:**
- `is_first_pass=True` is hardcoded (line 5061) for **all** rounds, including Round 2+. Since the flag is currently a no-op, this has no behavioral effect, but it is semantically wrong — Round 2+ is not a first pass.
- When Round 2+ is wired, `is_first_pass` should be set to `(state.dispatch_retry_rounds == 0)` or derived from `round_num`. The flag was designed for this purpose (docstring: "reserved for future callers").
- The field is controlled at the call site in `dispatch_with_retry` (line 5058–5063), not in `_prod_impl`. The fix: pass `is_first_pass=(state.dispatch_retry_rounds == 0)` at the `build_implementer_prompt` call.

---

## Q5 — `task_brief=""` at line 5059 — Bug or Intentional?

**Current call site** (line 5058–5063):
```python
prefix = build_implementer_prompt(
    task_brief="",           # ← always empty string
    reviewer_feedback=redacted_feedback or None,
    is_first_pass=True,
    is_doc_only=False,
)
```

**Is it a bug?** Yes, for Round 2+. For Round 1, `_prod_impl` discards `prompt_prefix` entirely (line 5315: `del prompt_prefix; return {}`), so the empty brief is harmless. But once Round 2+ wires a real implementer re-dispatch, the implementer subagent will receive only the K-class prohibition + reviewer feedback, with no task context — it won't know what to implement.

**Where the actual brief comes from:**
- `contract.acceptance_criteria` — list of acceptance criterion strings, available in `_cmd_auto_execute` at line 5486 and passed to `_phase2_dispatch` as `criteria`.
- `contract_path` → `contract.json` → could contain a `task_description` or similar field (contract schema-dependent).
- The task's `prd.md` (`task_dir / "prd.md"`) — rich brief, already on disk.
- `manifest.id` / manifest fields — minimal.

**Recommendation for Round 2+:** `task_brief` should be populated from:
1. `task_dir / "prd.md"` read (or a summary thereof) — richest context for the implementer.
2. Fallback: `"\n".join(criteria)` from `contract.acceptance_criteria` — structured acceptance criteria already available in `_phase2_dispatch` closure.

`_phase2_dispatch` has `task_dir`, `criteria`, `contract` in scope. The fix is straightforward: before calling `build_implementer_prompt`, construct `task_brief` from `(task_dir / "prd.md").read_text()` (with a try/except for missing file, fallback to `"\n".join(criteria)`).

---

## Summary Table

| Item | Location | Status |
|------|----------|--------|
| `auto_dispatch_task` | line 678 | Round 1 only; not idempotent (worktree collision on retry) |
| `_invoke_subagent_dispatch` | line 5146 | Dispatch shim; called via `dispatch_fn` kwarg from `auto_dispatch_task` |
| `_prod_impl` stub | line 5312 | Stub returning `{}`; T18 extension point |
| `build_implementer_prompt` | `dispatch_template.py:128` | `task_brief=""` bug for Round 2+; `is_first_pass` reserved/no-op |
| Round 2+ recommended path | new helper in `flow_orchestrator.py` | Option B: extract `_dispatch_implementer_fresh_worktree`, wire through `_prod_impl` |
