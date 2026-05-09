---
name: test-time-bomb-hardcoded-date-vs-real-now
date: 2026-05-08
project: flow-framework
severity: high
status: active
trigger_paths:
  - tests/smoke/test_phase2_retry_loop.py
  - tests/**/*afk*
  - tests/**/*time*
last_verified: 2026-05-08
---

# test-time-bomb-hardcoded-date-vs-real-now

## Symptom

Test PASSes locally + in CI on the day it's written, then silently FAILs
forever after. Discovered v0.8.3.1 hotfix: `test_phase2_dispatch_park_returns_rc5_no_merge`
asserts `_phase2_dispatch` returns rc=5 (PARKED_RECOVERABLE) for wait-
mode AFK timeout, but consistently returns rc=3 (terminal) instead. The
test PASSed at v0.8.3 ship-time (within hours of the hardcoded `start`)
and FAILed thereafter — invisible regression.

## Root cause

Mixing two clocks:

1. Test mock side: `start = datetime(2026, 5, 8, 0, 0, 0, tzinfo=timezone.utc)`
   + `AfkMonitor(start_iso=start_iso, hard_cap_seconds=99_999.0)`
   (~27.7h ceiling).
2. Production side: `_phase2_dispatch` uses
   `now_iso_fn=now_iso_utc` (real `time.time()`-based clock).

When the production code computes `active_seconds = real_now - start_iso`
and compares against `hard_cap_seconds`, the moment real_now drifts past
the hard cap (here ~27.7 hours after 2026-05-08 00:00 UTC), the AFK
monitor reports `afk_hard_cap` terminal **before** the wait-mode
idle_park check runs.

The test author probably intended `99_999.0` to be "huge enough to
ignore" but got the magnitude wrong (six 9s vs nine 9s).

## Fix pattern

When a test injects a mock AfkMonitor INTO production entry points
(rather than calling `dispatch_with_retry` directly with `now_iso_fn=mock`),
two safeguards required:

1. **Derive `start` from `datetime.now(timezone.utc)`**, not a hardcoded
   literal. Anchors the test's clock to wall time so the gap to real
   `now_iso_utc` stays minimal.
2. **Set `hard_cap_seconds = 99_999_999.0`** (~3 years), not `99_999.0`
   (~27.7h). Or use `float("inf")` if AfkMonitor accepts it.

Counter-pattern (safe): tests that call `dispatch_with_retry` directly
and pass `now_iso_fn=_make_now_fn(...)` (a deterministic step clock)
have NO real-time leakage; hardcoded `start` is fine because both sides
of the comparison are mock.

## Prevention

Before merging any AFK / time-sensitive test:

1. Grep new test for `datetime(YYYY, M, D` literals + `hard_cap_seconds=`
   numeric. If both present, verify the test does NOT call into a
   production function that uses `now_iso_utc` / `time.time()` /
   `datetime.now()`.
2. If injecting AfkMonitor into a production entry point, default to
   `start = datetime.now(timezone.utc)` + `hard_cap_seconds=99_999_999.0`.
3. Suspect any "tests pass on day-of-write" green light when production
   uses real time and the test uses fixed time anchors.

## Trigger paths (where to grep when this recurs)

- `tests/smoke/test_phase2_retry_loop.py` (the canonical example)
- Any test that imports `AfkMonitor` directly with hardcoded `start`
- Any test patching `_resolve_afk_monitor` (means real `now_iso_utc`
  reaches the monitor)

## Related

- v0.8.3.1 hotfix commit (this fix)
- v0.8.2.1 `ae340dc` introduced the rc=2 → rc=5 migration; the same
  hardcoded date + 99_999.0 cap survived that refactor unnoticed
- Earlier "985 PASS" reports (this session) were misleading because
  the test happened to be in the 27.7h window; subsequent runs caught
  the regression
