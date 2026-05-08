# Test Fixtures Research — P0.1 implementer redispatch

> Researched: 2026-05-08
> Researcher: subagent (Claude Sonnet 4.6)

---

## Q1 — Tests that exercise `dispatch_with_retry` / `_phase2_dispatch`

### Files found

| File | What it covers | Type |
|------|---------------|------|
| `tests/smoke/test_phase2_retry_loop.py` | Primary retry-loop suite (R3.1/R3.2/R3.3); `dispatch_with_retry` directly + `_phase2_dispatch` via `deps_factory`; AFK idle park rc=5; MergeRunner spy; round-cap terminals | **Unit (full fake)** |
| `tests/smoke/test_dual_counter_invariants.py` | J-class invariant 5 — every iteration advances exactly one counter; drives `dispatch_with_retry` with scripted fakes | **Unit (full fake)** |
| `tests/smoke/test_e2e_v0_8_2_p0.py` | D-class regression suite for v0.8.2 P0 fixes; mixes `dispatch_with_retry` and `_phase2_dispatch` (with `deps_factory`) for terminal paths and wire-up | **Unit (full fake)** |

No test currently uses `_prod_impl` real code path. No integration test spins up a real git repo or worktree.

---

## Q2 — `RetryDeps` protocol + `dispatch_with_retry` signature

**`RetryDeps` dataclass** (`flow_orchestrator.py:4767`):
```python
@dataclass
class RetryDeps:
    run_implementer_round: Callable[..., dict]
    run_codex_review: Callable[..., str]
```

Both callables receive the same keyword args:
- `run_implementer_round(*, state: RetrySessionState, prompt_prefix: str, **_kw) -> dict`
  - Returns counter-delta dict (keys: `tokens_in`, `tokens_out`, `cost_usd`, `dispatch_count`, `active_wallclock_minutes`; plus `model_id`/`pricing_version` for cost)
  - Empty dict `{}` is legal (Round 1 stub behavior)
- `run_codex_review(*, state: RetrySessionState, impl_deltas: dict, **_kw) -> str`
  - Must return one of: `"pass"` | `"fail"` | `"rejected_with_rationale"`
  - Any other value → `ValueError` (PRD R3 invariant 5)

**`dispatch_with_retry` signature** (`flow_orchestrator.py:4937`):
```python
def dispatch_with_retry(
    *, state: RetrySessionState, config: RetryConfig,
    budget, afk: AfkMonitor, deps: RetryDeps,
    now_iso_fn: Callable[[], str],
) -> tuple[str, HardStopSnapshot | None]:
```
Returns `(outcome, snapshot)` where outcome ∈ `{"pass", "budget_hit", "retry_cap", "review_cap", "afk_idle_park", "afk_hard_cap", "afk_aborted"}`.

**`_phase2_dispatch`** accepts optional `deps_factory: Callable[..., RetryDeps] | None` (line 5229).
When provided, `deps_factory(**kw)` is called with slug, task_dir, contract, manifest, facts, ctx, criteria, gate_cmds, run_id, task_id.

---

## Q3 — Existing fakes for Round 2+ behavior

**Current fake pattern** (from `test_phase2_retry_loop.py`):

```python
def _scripted_impl(outcomes: list):
    seq = list(outcomes)
    def f(*args, **kwargs): return seq.pop(0)
    return f

deps = RetryDeps(
    run_implementer_round=_scripted_impl([
        {"tokens_in": 100},  # Round 1
        {"tokens_in": 80},   # Round 2
    ]),
    run_codex_review=_scripted_review(["fail", "pass"]),
)
```

**Gap**: All existing fakes return identical-shaped empty/token-delta dicts regardless of round number. None:
- Track which `ctx` object was active per round
- Verify ctx distinctness between rounds
- Simulate worktree creation
- Assert `state.failed_rounds` accumulation (field doesn't exist yet)

**`state.dispatch_retry_rounds` increment** is tested at line 150 and 213 of `test_phase2_retry_loop.py` — confirmed correct. But no test checks that the prod impl receives distinct ctx per round.

---

## Q4 — Test pattern recommendation for AC1–AC4

**Recommended approach: (a) + (b)**

### (a) Enhanced unit fake — `_prod_impl_fake` — for AC1/AC3/AC4

Extend the `deps_factory` pattern with a fake that:
1. Records which `state.dispatch_retry_rounds` value was active at call time (AC1)
2. Captures the `prompt_prefix` per round (AC3)
3. Returns a new synthetic `WorktreeContext`-like object per call, stored on `state.winner_ctx` (AC4)

This requires no real filesystem and stays within the existing `deps_factory` seam.

**Key AC4 gap**: `_phase2_dispatch` currently uses a single `ctx` arg (from Round 1 `auto_dispatch_task`) to instantiate `MergeRunner` (line 5611). P0.1 must change `_phase2_dispatch` to pass `winner_ctx` (the ctx from the round that passed) to `MergeRunner`. A unit test can spy on `MergeRunner.__init__` to verify it received the winning ctx, not the original.

### (b) Minimal integration test — for AC2 (fresh worktree per round)

The only AC that requires real filesystem is AC2 — "Round 2 worktree path ≠ Round 1 worktree path, and Round 2 branch is clean (no Round 1 modifications)".

This needs:
1. A real temp git repo (use `git init` in `tempfile.TemporaryDirectory`)
2. Drive `_prod_impl` directly (not through `_phase2_dispatch`) with `state.dispatch_retry_rounds = 1`
3. Assert that `create_task_worktree` is called and returns a `WorktreeContext` with a different `worktree_path` than a prior ctx

**Smallest integration test that gives confidence** (~20 lines):
```python
def test_round2_creates_fresh_worktree():
    with tmp_git_repo() as repo_root:
        ctx1 = create_task_worktree(slug="t", task_idx=0, repo_root=repo_root, ...)
        # Mutate worktree1 — add a file
        (ctx1.worktree_path / "round1.txt").write_text("dirty")
        ctx2 = create_task_worktree(slug="t", task_idx=1, repo_root=repo_root, ...)
        assert ctx2.worktree_path != ctx1.worktree_path
        assert not (ctx2.worktree_path / "round1.txt").exists()
```

The real `_prod_impl` AC1 integration test is heavier (requires a fake subagent dispatch) and can be deferred to AC9's new test file.

---

## Q5 — `tests/conftest.py` shared helpers

**No `tests/conftest.py` exists** — confirmed missing.

Each test file is self-contained. Shared patterns used across files:
- `_make_state()` — creates `RetrySessionState` (defined locally per file)
- `_make_budget()` — wraps `bc.make_default_set(_LIMITS)`
- `_make_afk()` — creates `AfkMonitor` with huge thresholds
- `_scripted_impl()` / `_scripted_review()` — scripted fake callables
- `_fake_deps_factory(**_kw)` — inline closure in each test method

No git repo factory helper exists anywhere. A new `tests/smoke/helpers.py` (or inline in the new test) must provide `tmp_git_repo()` for integration tests.

---

## Smallest test addition for AC1–AC4 confidence

**Two new tests in `tests/smoke/test_p0_1_redispatch.py`**:

1. **Unit: `test_round2_impl_called_with_distinct_ctx` (AC1/AC3/AC4)**
   - Drive `_phase2_dispatch` with `deps_factory` fake
   - Fake `run_implementer_round` records `(state.dispatch_retry_rounds, id(ctx_arg))` per call
   - Fake stores winning ctx in a closure, spy on `MergeRunner.__init__` via monkeypatch
   - Verify: impl called twice, Round 2 ctx-id ≠ Round 1 ctx-id (AC2 proxy), prompt_prefix on Round 2 contains non-empty feedback (AC3), MergeRunner received Round 2 ctx (AC4)
   - Pure fake — no real FS beyond `tempfile` for `task_dir`

2. **Integration: `test_fresh_worktree_has_no_round1_mutations` (AC2)**
   - Real `git init` in tempdir
   - Call `create_task_worktree` twice with task_idx 0 and 1
   - Mutate worktree 0, verify worktree 1 is clean
   - ~30 lines

These two tests cover all 4 ACs with minimal surface. AC1 real subagent dispatch can stay as a manual verification / future CI test.

---

## File Refs

| Symbol | Location |
|--------|---------|
| `RetryDeps` | `scripts/flow_orchestrator.py:4767` |
| `RetrySessionState` | `scripts/flow_orchestrator.py:4741` |
| `dispatch_with_retry` | `scripts/flow_orchestrator.py:4937` |
| `_prod_impl` stub | `scripts/flow_orchestrator.py:5312` |
| `_phase2_dispatch` / `deps_factory` wiring | `scripts/flow_orchestrator.py:5229, 5285` |
| `MergeRunner` instantiation (winner_ctx gap) | `scripts/flow_orchestrator.py:5611` |
| `create_task_worktree` | `scripts/flow_orchestrator.py:362` |
| Primary retry-loop test suite | `tests/smoke/test_phase2_retry_loop.py` |
| Dual-counter invariant tests | `tests/smoke/test_dual_counter_invariants.py` |
| v0.8.2 P0 E2E regression suite | `tests/smoke/test_e2e_v0_8_2_p0.py` |
