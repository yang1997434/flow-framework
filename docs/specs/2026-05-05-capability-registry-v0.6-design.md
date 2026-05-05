---
title: "Capability Registry Expansion v0.6 — gstack/superpowers integration"
date: 2026-05-05
type: design
status: draft
target_release: v0.6.0
---

# Capability Registry Expansion v0.6

## Context

`docs/Skills-Phase映射.md` documents 60+ skills across all installed plugins (gstack, superpowers, impeccable, planning-with-files, code-review, pr-review-toolkit, etc.) for the backend task type. But `claude/capabilities/defaults.json` only wires **14** of them. The other 46+ are documentation, not active orchestration — Flow's phase prompts cannot invoke them via `{{capability:X}}` placeholders, so they only fire if the user manually triggers them.

Audit during 2026-05-05 session against the backend task chain (representative — patterns transfer to other task types) found **20 initial ADD candidates**. After a UX-driven prune of `gstack:plan-*-review` family (replaced by hat-shifted brainstorming) plus user-driven expansion for Tier-A discipline + prod/deploy ops, the final scope is **19 new capabilities + 1 SKILL.md change**.

## Decision

Add 19 capabilities to `claude/capabilities/defaults.json` (registry grows 14 → 33). Modify `claude/skills/flow/flow-phase1-plan/SKILL.md` to support hat-shifted brainstorming continuation in lieu of `gstack:plan-*-review` skills. Re-render prompt templates. Bump VERSION 0.5.9 → 0.6.0 (capability-count change is minor, not patch). Selftest must pass before push.

## Scope — 19 new capabilities

### Phase 1 — Plan / Brainstorm (2 adds)

| Capability | Default skill | Trigger | Justification |
|---|---|---|---|
| `multi_step_plan` | `planning-with-files:plan` | B/C-size tasks (multi-day, multi-file) | Manus-style file-protocol planning; complement to `superpowers:brainstorming` for tasks that exceed conversational scope |
| `dev_setup` | `gstack:setup-deploy` | Phase 1 of deploy task type (one-time per project) | Detect platform (Fly/Render/Vercel/Netlify/Heroku/GitHub Actions/custom) and write deploy config; necessary precondition for `deploy_chain` and `land_and_deploy` |

### Phase 2 — Execute / Implement (5 adds)

| Capability | Default skill | Trigger | Justification |
|---|---|---|---|
| `subagent_discipline` | `superpowers:subagent-driven-development` | Whenever sub-agents are dispatched | Pairs with existing `parallel_dispatch` — discipline (prompts, return contracts), the other (orchestration mechanics) |
| `execute_plan_discipline` | `superpowers:executing-plans` | When implementation plan exists | Discipline for following written plans; closes the loop with `multi_step_plan` |
| `systematic_debug` | `superpowers:systematic-debugging` | First time stuck on a bug | Iron-law 4-phase root-cause discipline before reaching for fixes |
| `deep_investigate` | `gstack:investigate` | When `systematic_debug` insufficient | Heavier debugging pipeline — escalation when systematic isn't enough |
| `land_and_deploy` | `gstack:land-and-deploy` | Phase 2 of deploy task — small confident changes | One-shot merge + deploy + canary verify; alternative to existing `deploy_chain` (ship + canary, separate steps). User picks based on confidence: small change → land_and_deploy; large feature → deploy_chain |

### Phase 3 — Finish / Verify (8 adds)

| Capability | Default skill | Trigger | Justification |
|---|---|---|---|
| `verify_completion` | `superpowers:verification-before-completion` | **Mandatory** at Phase 3 entry | **Closes a security-class gap** — without this, Flow's Phase 3 has no enforced "actually verify before claiming done" step. Currently the framework allows Claude to self-report success without running verification |
| `code_review_small` | `code-review:code-review` | Diff < 200 lines, single module | 5 Sonnet parallel + Haiku confidence scoring; the daily-driver reviewer |
| `code_review_large` | `pr-review-toolkit:review-pr` | Diff ≥ 200 lines, multi-module | 6-specialist agent panel; high-coverage but token-heavy |
| `review_request_etiquette` | `superpowers:requesting-code-review` + `superpowers:receiving-code-review` (chain) | Whenever `code_review_*` is invoked | Discipline for HOW to request review (clear scope, what to look at) and HOW to process feedback (verify before agreeing). Prevents performative agreement to bad suggestions |
| `pre_land_review` | `gstack:review` | Before merge for SQL/LLM/side-effect-heavy diffs | Specialized for database safety, LLM trust boundaries, conditional side effects — patterns the general reviewers miss |
| `quality_health` | `gstack:health` | Phase 3 entry | Composite 0-10 quality score using existing project tools (typecheck, lint, tests, dead code) — fast quality gate |
| `perf_baseline` | `gstack:benchmark` | Phase 3 of performance-sensitive backend tasks | Web Vitals + resource size baseline + regression compare; pairs with `quality_health` (one for code quality, the other for runtime performance) |
| `post_deploy_qa` | `gstack:qa` | Phase 3 of deploy task — after ship | Active QA on deployed site (clicks login, fills forms) — complements `canary` which is passive monitoring; without `post_deploy_qa`, deploy verification trusts canary alerts entirely |

### Phase 4 — Sediment (2 adds)

| Capability | Default skill | Trigger | Justification |
|---|---|---|---|
| `branch_finish` | `superpowers:finishing-a-development-branch` | Phase 4 entry | Structured options for merge / PR / cleanup — closes the loop after sediment |
| `changelog_gen` | `gstack:changelog-generator` | After ship, before release | Auto-generates user-facing changelog from commit history; eliminates the manual CHANGELOG.md editing step |

### Cross-cutting (any phase, 2 adds)

| Capability | Default skill | Trigger | Justification |
|---|---|---|---|
| `safety_guardrails` | `gstack:careful` | Any destructive operation in any phase (`rm -rf`, `DROP TABLE`, `force-push`, `git reset --hard`, kubectl delete, migrations) | Cross-cutting safety — prod operations are common in user's workflow. Independent of phase; fires whenever destructive command pattern matches |
| `weekly_retro` | `gstack:retro` | User-triggered or scheduled (`/loop weekly /flow:retro`) | Cross-task weekly review (commits / work patterns / quality trends) — Phase 4 sediment is per-task; retro is per-week. Fills the per-task-vs-cross-task gap |

## Phase 1 SKILL.md change — hat-shifted brainstorming

**Why not add `plan_eng_critique` capability instead:** `gstack:plan-*-review` skills produce list-style output (N issues at once, wall of markdown). This breaks `superpowers:brainstorming`'s one-question-at-a-time rhythm that the user values. Wrapping gstack's output to re-queue questions one at a time is fragile (depends on gstack output format stability).

**Approach:** Extend `flow-phase1-plan/SKILL.md` to optionally invoke a second brainstorming round in a different perspective — same skill, same rhythm, different hat.

**Spec:**
- After base brainstorming completes (prd.md sketched), Flow proposes optional perspective-shifted continuation
- Each perspective = a new `superpowers:brainstorming` invocation with a hat-prompt prefix
- Available hats (not auto-chained — user picks 0-N):
  - **Engineer hat** — architecture, data flow, edge cases, performance (covers what gstack:plan-eng-review covered)
  - **DX hat** — developer experience, friction points (covers gstack:plan-devex-review)
  - **Security hat** — threat surfaces, secret handling, input validation, trust boundaries, blast radius of destructive operations
  - **(NOT included: CEO hat — user explicitly opted out)**
- Same one-question-at-a-time rhythm as base brainstorming → output appends to prd.md as "Perspective Critique: <hat>" section

**Implementation:** ~15 lines added to `flow-phase1-plan/SKILL.md` after the base brainstorm step.

## Out of scope (rejected during design)

| Candidate | Why rejected |
|---|---|
| `plan_ceo_critique` (gstack:plan-ceo-review) | User explicitly: "项目商业前景自己想清楚了" — don't need YC-partner-style market validation |
| `autoplan` (gstack:autoplan) | Bundles all 4 plan-*-review including CEO; also batches output (worst-case wall-of-text) |
| `plan_eng_critique` (gstack:plan-eng-review) | Replaced by hat-shifted brainstorming (above) for UX consistency |
| `plan_devex_critique` (gstack:plan-devex-review) | Same — covered by DX hat in brainstorming |
| `release_docs` (gstack:document-release) | Deferred to v0.7 — orthogonal to core verify/sediment loop |
| `project_learnings` (gstack:learn) | Overlaps with `/flow:promote`; need separate design pass |
| `security_audit` (gstack:security-review / cso) | Deferred — task-type tagging needed first |
| `silent-failure-hunter` | Per-PR opt-in via `code_review_large` already covers it |

## Migration plan

1. **Edit `claude/capabilities/defaults.json`** — add 19 new entries with `description`, `default`, optional `requires_cli`, `skip_if_not_available`. For `review_request_etiquette`, value is a chain (`requesting → receiving`); schema currently supports comma-separated chain like `deploy_chain` does
2. **Edit `claude/skills/flow/flow-phase1-plan/SKILL.md`** — add hat-shift section (~15 lines, includes Engineer / DX / Security hats); wire `{{capability:multi_step_plan}}` (B/C-size trigger) and `{{capability:dev_setup}}` (deploy task trigger)
3. **Edit `claude/skills/flow/flow-phase2-execute/SKILL.md`** — wire `{{capability:execute_plan_discipline}}`, `{{capability:subagent_discipline}}`, `{{capability:systematic_debug}}` (escalation path to `{{capability:deep_investigate}}`), `{{capability:land_and_deploy}}` (deploy task alt path)
4. **Edit `claude/skills/flow/flow-phase3-finish/SKILL.md`** — wire `{{capability:verify_completion}}` (mandatory entry), `{{capability:quality_health}}` (gate), routing between `{{capability:code_review_small}}` / `{{capability:code_review_large}}`, `{{capability:review_request_etiquette}}` (paired with code_review_*), `{{capability:pre_land_review}}` (conditional), `{{capability:perf_baseline}}` (perf-sensitive backend tasks), `{{capability:post_deploy_qa}}` (deploy tasks)
5. **Edit `claude/skills/flow/flow-phase4-sediment/SKILL.md`** — wire `{{capability:branch_finish}}`, `{{capability:changelog_gen}}` (after ship)
6. **Edit `claude/skills/flow/flow-orchestrator/SKILL.md`** — add cross-cutting section describing `{{capability:safety_guardrails}}` (auto-fire on destructive command patterns) and `{{capability:weekly_retro}}` (user-triggered)
7. **Run `flow install render-prompts`** to substitute new placeholders into `~/.claude/{commands,skills}/flow/`
8. **Update `docs/Skills-Phase映射.md`** — flip the 19 affected rows from "documented but not wired" to "active capability"; update closing audit (line 310-331) to reflect new coverage
9. **Run `flow_selftest.py`** — must pass; rendered prompts must have no leftover `{{capability:X}}` for new entries
10. **Bump VERSION 0.5.9 → 0.6.0** — capability-count change is minor, not patch
11. **Update `CHANGELOG.md`** — v0.6.0 entry covering scope above
12. **Commit + tag + push + GitHub release**

## Open questions / risks

1. **Auto-trigger vs opt-in**: `verify_completion` MUST be mandatory at Phase 3 entry (security-class gap). `safety_guardrails` MUST auto-fire on destructive patterns (cross-cutting safety). Others (`quality_health`, `pre_land_review`, `perf_baseline`, `post_deploy_qa`, etc.) — default to **propose, don't auto-run**; explicit yes/no in SKILL.md
2. **Diff-size routing for code review**: `code_review_small` vs `code_review_large` threshold. Default: 200 lines (configurable in `.flow/config.yaml`). Need a measurement helper or rely on user judgment
3. **`gstack:*` capabilities require gstack installation**: 8 of 19 new capabilities depend on gstack (`dev_setup`, `deep_investigate`, `pre_land_review`, `quality_health`, `perf_baseline`, `post_deploy_qa`, `land_and_deploy`, `changelog_gen`, `safety_guardrails`, `weekly_retro`). All marked `skip_if_not_available: true` — Flow degrades gracefully without gstack but loses those capabilities. Doc must call out the dependency clearly so users know what they lose
4. **`land_and_deploy` vs existing `deploy_chain` coexistence**: both target Phase 2 of deploy task. Spec defines them as alternatives (user picks based on confidence), not chain. Phase 2 SKILL.md must clearly present the choice without auto-picking. Need explicit decision-aid prose
5. **`safety_guardrails` auto-fire mechanism**: gstack:careful is a slash-command skill; how does Flow auto-trigger it on destructive patterns? Likely via Bash hook pre-tool match (similar to user's existing `pre-commit-review.sh` pattern). May require a new hook script — investigate during implementation
6. **`review_request_etiquette` is a chain (request → receive)**: defaults.json schema currently uses `default` as single string. `deploy_chain` already uses comma-separated form. Confirm parser handles this for non-deploy chains too — if not, schema update needed
7. **Phase 1 hat-shift `superpowers:brainstorming` re-invocation**: depends on the brainstorming skill being callable multiple times in one session with different prompt prefixes. Need to verify this doesn't conflict with brainstorming's `<HARD-GATE>` (which prevents implementation). Should be fine since hat-shifted rounds are still brainstorming, not implementation
8. **`changelog_gen` runs on commit history but Flow's CHANGELOG.md is curated by hand currently**: gstack:changelog-generator might overwrite vs append. Need to test once before flipping default

## Success criteria

- `flow_selftest.py` PASSED with 19 new capabilities resolved
- Rendered phase SKILL.md files contain no leftover `{{capability:X}}` placeholders
- Manually trigger Phase 3 in a test task → `verify_completion` fires before any `done` claim
- Manually trigger destructive command (e.g. `git reset --hard`) → `safety_guardrails` warning fires
- v0.6.0 GitHub release published with the 19-capability summary
- `docs/Skills-Phase映射.md` "推荐写法" closing audit (line 310-331) updated to reflect new coverage
