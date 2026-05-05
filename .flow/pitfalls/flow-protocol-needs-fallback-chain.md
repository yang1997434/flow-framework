---
name: flow-protocol-needs-fallback-chain
date: 2026-05-05
project: flow-framework
severity: high
status: active
trigger_paths:
  - "claude/commands/flow/start.md"
  - "claude/skills/flow/flow-phase1-plan/SKILL.md"
  - "claude/capabilities/defaults.json"
last_verified: 2026-05-05
---

# flow-protocol-needs-fallback-chain

## Symptom

When the `model:research` env var routing breaks (see
`agent-sonnet-alias-stale.md`), Flow's `/flow:start` protocol provides no
guidance on what to do. Operators fall back to `model: "haiku"` — which
**is not designed for research depth** and produces shallower findings —
because it's the only enum value that "works" without further config.

## Root cause

Three protocol gaps:

1. **Specific-ID rendering**: `defaults.json` shipped `claude-sonnet-4-6`
   (full ID) for `model_roles.research.default`. The Agent tool's `model`
   parameter is **enum-restricted to `sonnet|opus|haiku` aliases** — full
   IDs are rejected with `InputValidationError`.
2. **No fallback in protocol**: `/flow:start` and `flow-phase1-plan/SKILL.md`
   said "use `{{model:research}}`" but said nothing about what to do if
   dispatch fails.
3. **No anti-pattern guidance**: nothing warned operators that downgrading
   research-class to `haiku` is wrong.

## Fix

1. `defaults.json` `model_roles.*.default` → use aliases (`sonnet`, `opus`,
   `haiku`) not full IDs. Add `fallback` field per role.
2. `/flow:start` and `flow-phase1-plan/SKILL.md` add explicit text:
   - **Primary**: pass alias `{{model:research}}`
   - **Fallback**: on dispatch failure, retry once with the alias from
     `model_roles.research.fallback` (currently `opus`)
   - **Never** route research sub-agents to `haiku` — depth matters
3. Concrete model id selection happens at `ANTHROPIC_DEFAULT_*_MODEL` env
   var level (1M-context variant recommended for long-research).

## Prevention

When adding new placeholder substitutions in flow source:
- Verify the rendered output is **valid input** for the consuming tool.
  If the tool has an enum, the renderer must produce one of the enum values.
- Always document the **fallback** behavior next to the placeholder, not
  somewhere else in the codebase.
- Anti-regression test: `tests/smoke/test_capability.py:MODEL_HARDCODE_RE`
  must catch any new `model: "alias"` literal in source files.

## Why it matters

Without an explicit fallback chain, the operator under pressure picks
the wrong model class. A single Phase 1 done with Haiku instead of
Sonnet 1M can produce shallower research → wrong design choices →
mid-Phase-2 rework. Recurrence cost: hours of rework × probability,
plus erosion of trust in the framework.

## References

- Commit: (pending)
- Related: agent-sonnet-alias-stale.md
- Related: phase-state-triple-bug.md
