---
name: subagent-misread-brief-do-not-add-modules
date: 2026-05-08
project: flow-framework
severity: medium
status: active
trigger_paths:
  - "claude/skills/flow/flow-phase2-execute/SKILL.md"
  - "scripts/dispatch_template.py"
  - "scripts/flow_orchestrator.py"
last_verified: 2026-05-08
---

# subagent-misread-brief-do-not-add-modules

## Symptom

T3 dispatch brief said "do NOT add new modules" intending "don't create
new `.py` files outside the listed scope". Subagent interpreted as "don't
modify existing files for production wire-up either" and shipped the new
`dispatch_with_retry` abstraction + tests but left `_cmd_auto_execute`
(the production entrypoint) unchanged. Required a follow-up T3.1 dispatch
to wire production. Subagent's quote:

> "Production wire-up of `dispatch_with_retry` into `_cmd_auto_execute`
> is left for a follow-up T (per the prompt: 'do NOT add new modules';
> the loop is callable + tested in isolation per ADR-1's DI contract)."

## Root cause

Negative phrasing "do NOT add X" is ambiguous when X is broad. Subagent
prioritized literal compliance over goal completion. The dispatch_with_retry
abstraction was technically callable + testable in isolation, so subagent
declared success.

This is also adjacent to **18-class D (control flow drift)**: refactor
that ships a new path while leaving the old one wired in production.
T3 D-class self-check passed because subagent interpreted "preserve
fail-fast semantics" as "leave old path active".

## Prevention

**Brief language hygiene**:

- ❌ "do NOT add new modules" — ambiguous (file? module path? abstraction?)
- ✅ "may modify existing files: A, B, C. may NOT create new `.py` files
   in scripts/common/. The deliverable is X — production must flow through
   the new path; legacy path becomes unreachable from `<entrypoint>`."

**Acceptance criteria phrasing**:

- ❌ "all 5 invariants tested" — passes if abstraction tests pass
- ✅ "all 5 invariants tested AND production entrypoint `<X>` calls the
   new path AND legacy `<Y>` is unreachable from production (verify via
   spy test trap)"

**Dispatch checklist** (add to dispatch_template.build_implementer_prompt):

```
Before declaring done, verify:
- [ ] Production entrypoint(s) actually call the new code path
- [ ] Legacy path(s) being replaced are unreachable from production
       (test it with a spy / mock that fails if invoked)
- [ ] Acceptance criteria tested via PRODUCTION code path, not just
       isolated abstraction tests
```

## Workaround used in v0.8.2

Dispatched T3.1 with focused brief specifically targeting the wire-up
gap. T3.1 added the spy test (`test_phase2_dispatch_routes_through_retry_loop`
asserts `GateRunner.run_phase2` is NOT called) which closes the D-class
loophole.

## v0.8.3 candidate

Promote the "Before declaring done" checklist into
`scripts/dispatch_template.py::build_implementer_prompt` so every implementer
prompt includes it automatically. Tests already exist for K-class禁令
prepend; pattern extends naturally.

## Related

- v0.8.2 T3 commit `52829a0` (incomplete delivery)
- v0.8.2 T3.1 commit `fd79ed9` (wire-up fix)
- 18-class D (control flow drift) — adjacent
- `dispatch_template.K_CLASS_SENTINEL_PROHIBITION` (T4) — sister hardening
  for a different blind spot
