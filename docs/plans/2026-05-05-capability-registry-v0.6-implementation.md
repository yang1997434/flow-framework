# Capability Registry Expansion v0.6 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wire 19 new capabilities (gstack / superpowers / pr-review-toolkit / planning-with-files / code-review) into Flow's capability registry, plus Phase 1 SKILL.md hat-shifted brainstorming, releasing as v0.6.0.

**Architecture:** Pure additive changes — no schema migration needed. New entries in `claude/capabilities/defaults.json`, new `{{capability:X}}` placeholders in 5 phase SKILL.md files + flow-orchestrator/SKILL.md. Comma-separated chain syntax (already used by `deploy_chain`) re-applied for `review_request_etiquette`. Full backwards compatibility — existing 14 capabilities unchanged.

**Tech Stack:** Python 3 (defaults.json + tests), Markdown (SKILL.md prompts), Bash (selftest + git). No new runtime dependencies.

---

## Source spec

`docs/specs/2026-05-05-capability-registry-v0.6-design.md` — read this first for context. This plan implements the spec's "Migration plan" section (12 steps) decomposed into bite-sized actions.

## File Structure

| File | Action | Responsibility |
|---|---|---|
| `claude/capabilities/defaults.json` | Modify | +19 capability entries |
| `claude/skills/flow/flow-phase1-plan/SKILL.md` | Modify | +hat-shift section, wire 2 caps |
| `claude/skills/flow/flow-phase2-execute/SKILL.md` | Modify | Wire 5 caps |
| `claude/skills/flow/flow-phase3-finish/SKILL.md` | Modify | Wire 8 caps |
| `claude/skills/flow/flow-phase4-sediment/SKILL.md` | Modify | Wire 2 caps |
| `claude/skills/flow/flow-orchestrator/SKILL.md` | Modify | +cross-cutting section (2 caps) |
| `tests/smoke/test_capability.py` | Modify | +REQUIRED_CAPS entries (19) |
| `docs/Skills-Phase映射.md` | Modify | Flip 19 rows + closing audit |
| `VERSION` | Modify | 0.5.9 → 0.6.0 |
| `CHANGELOG.md` | Modify | +v0.6.0 entry |

No new files. No deletions. Total 10 files touched.

## Open question resolutions (locked before coding)

- **Q5 (safety_guardrails auto-fire mechanism)**: resolved as **document-only invocation** in flow-orchestrator/SKILL.md. Same pattern as existing capabilities — orchestrator prompt instructs "invoke `{{capability:safety_guardrails}}` before destructive ops". Hook-based auto-fire deferred to v0.7 (would need new Bash hook script).
- **Q6 (review_request_etiquette chain schema)**: resolved as **comma-separated string** like existing `deploy_chain`. The renderer just substitutes the literal string — Claude reads the prompt and sees both skills mentioned. No schema change needed.

---

## Task 1: Add capability tests (TDD red phase)

**Files:**
- Modify: `tests/smoke/test_capability.py:38-43` (REQUIRED_CAPS set)

- [ ] **Step 1: Add 19 new capability names to REQUIRED_CAPS**

Open `tests/smoke/test_capability.py`. Find the `REQUIRED_CAPS` set (currently 13 entries). Replace with:

```python
REQUIRED_CAPS = {
    # Original 14 (v0.5.x baseline)
    "brainstorm", "ux_brief",
    "cross_model_consult", "cross_model_review", "cross_model_challenge",
    "tdd", "worktree", "parallel_dispatch",
    "ui_implement", "ui_audit", "ui_visual_review",
    "session_save", "deploy_chain", "behavioral_guidelines",
    # v0.6.0 additions — Phase 1 (2)
    "multi_step_plan", "dev_setup",
    # v0.6.0 additions — Phase 2 (5)
    "subagent_discipline", "execute_plan_discipline",
    "systematic_debug", "deep_investigate",
    "land_and_deploy",
    # v0.6.0 additions — Phase 3 (8)
    "verify_completion",
    "code_review_small", "code_review_large", "review_request_etiquette",
    "pre_land_review", "quality_health", "perf_baseline", "post_deploy_qa",
    # v0.6.0 additions — Phase 4 (2)
    "branch_finish", "changelog_gen",
    # v0.6.0 additions — Cross-cutting (2)
    "safety_guardrails", "weekly_retro",
}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest tests.smoke.test_capability.RegistryDefaults.test_all_required_capabilities_present -v`
Expected: FAIL with `missing capabilities: {'multi_step_plan', 'dev_setup', ...}` (19 missing)

- [ ] **Step 3: No commit yet** — commit happens after Task 2 makes it green.

---

## Task 2: Add 19 capability entries to defaults.json (TDD green phase)

**Files:**
- Modify: `claude/capabilities/defaults.json:79-80` (after `behavioral_guidelines`, before closing brace)

- [ ] **Step 1: Insert 19 new entries after `behavioral_guidelines`**

Open `claude/capabilities/defaults.json`. Find the closing `}` of `behavioral_guidelines` (line ~79). Replace the lines from end of `behavioral_guidelines` block through `},` (which currently closes the `capabilities` object — opening this for insertion) with the following — keep the trailing `},` that ends `capabilities`:

```json
    "behavioral_guidelines": {
      "default": "andrej-karpathy-skills:karpathy-guidelines",
      "description": "Phase 2 — surgical changes / define success criteria / surface assumptions / avoid over-engineering",
      "skip_if_not_available": true
    },

    "_v0_6_phase1_additions": "Phase 1 capabilities added in v0.6.0",
    "multi_step_plan": {
      "default": "planning-with-files:plan",
      "description": "Phase 1 — Manus-style file-protocol planning for B/C-size tasks (multi-day, multi-file)",
      "skip_if_not_available": true
    },
    "dev_setup": {
      "default": "gstack:setup-deploy",
      "description": "Phase 1 (deploy task) — detect platform (Fly/Render/Vercel/Netlify/Heroku/GitHub Actions/custom) + write deploy config",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },

    "_v0_6_phase2_additions": "Phase 2 capabilities added in v0.6.0",
    "subagent_discipline": {
      "default": "superpowers:subagent-driven-development",
      "description": "Phase 2 — sub-agent prompts + return contracts; pairs with parallel_dispatch (the latter handles orchestration mechanics)",
      "skip_if_not_available": true
    },
    "execute_plan_discipline": {
      "default": "superpowers:executing-plans",
      "description": "Phase 2 — discipline for following written implementation plans (closes the loop with multi_step_plan)",
      "skip_if_not_available": true
    },
    "systematic_debug": {
      "default": "superpowers:systematic-debugging",
      "description": "Phase 2 — iron-law 4-phase root-cause discipline; first-line debug before reaching for fixes",
      "skip_if_not_available": true
    },
    "deep_investigate": {
      "default": "gstack:investigate",
      "description": "Phase 2 — heavier debugging pipeline; escalation path when systematic_debug isn't enough",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },
    "land_and_deploy": {
      "default": "gstack:land-and-deploy",
      "description": "Phase 2 (deploy task) — one-shot merge + deploy + canary verify; alternative to deploy_chain (for small confident changes; deploy_chain is for large features needing separate ship → wait → land)",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },

    "_v0_6_phase3_additions": "Phase 3 capabilities added in v0.6.0",
    "verify_completion": {
      "default": "superpowers:verification-before-completion",
      "description": "Phase 3 — MANDATORY at entry; runs verification commands and confirms output before any 'done' claim. Closes a security-class gap where Flow previously allowed self-reported success without actual verification"
    },
    "code_review_small": {
      "default": "code-review:code-review",
      "description": "Phase 3 — daily-driver reviewer (5 Sonnet parallel + Haiku confidence scoring) for diff < 200 lines, single module",
      "skip_if_not_available": true
    },
    "code_review_large": {
      "default": "pr-review-toolkit:review-pr",
      "description": "Phase 3 — 6-specialist agent panel for diff ≥ 200 lines or multi-module PRs (high coverage but token-heavy)",
      "skip_if_not_available": true
    },
    "review_request_etiquette": {
      "default": "superpowers:requesting-code-review,superpowers:receiving-code-review",
      "description": "Phase 3 — discipline for requesting review (clear scope) + processing feedback (verify before agreeing); paired with code_review_small/large invocations",
      "skip_if_not_available": true
    },
    "pre_land_review": {
      "default": "gstack:review",
      "description": "Phase 3 — pre-merge diff review for SQL safety / LLM trust boundaries / conditional side effects; specialist patterns the general reviewers miss",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },
    "quality_health": {
      "default": "gstack:health",
      "description": "Phase 3 — composite 0-10 quality score using existing project tools (typecheck + lint + tests + dead code); fast quality gate at Phase 3 entry",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },
    "perf_baseline": {
      "default": "gstack:benchmark",
      "description": "Phase 3 — Web Vitals + resource size baseline regression compare for perf-sensitive backend tasks; pairs with quality_health (one for code quality, the other for runtime performance)",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },
    "post_deploy_qa": {
      "default": "gstack:qa",
      "description": "Phase 3 (deploy task) — active QA on deployed site (clicks login, fills forms); complements canary's passive monitoring (canary alerts on failure; post_deploy_qa actively verifies success)",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },

    "_v0_6_phase4_additions": "Phase 4 capabilities added in v0.6.0",
    "branch_finish": {
      "default": "superpowers:finishing-a-development-branch",
      "description": "Phase 4 — structured options for merge / PR / cleanup; closes the loop after sediment is written",
      "skip_if_not_available": true
    },
    "changelog_gen": {
      "default": "gstack:changelog-generator",
      "description": "Phase 4 — auto-generate user-facing changelog from commit history; eliminates manual CHANGELOG.md editing step (validate output once before flipping default; may overwrite vs append)",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },

    "_v0_6_cross_cutting_additions": "Cross-cutting capabilities added in v0.6.0 (any phase)",
    "safety_guardrails": {
      "default": "gstack:careful",
      "description": "Cross-cutting — destructive command warnings (rm -rf, DROP TABLE, force-push, git reset --hard, kubectl delete, migrations); orchestrator invokes before any destructive op. Hook-based auto-fire deferred to v0.7",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    },
    "weekly_retro": {
      "default": "gstack:retro",
      "description": "Cross-cutting — cross-task weekly review (commits / work patterns / quality trends). User-triggered or scheduled via /loop weekly. Fills the per-task-vs-cross-task gap (Phase 4 sediment is per-task; retro is per-week)",
      "requires_cli": "gstack",
      "skip_if_not_available": true
    }
```

The lines `"_v0_6_*_additions": "..."` are JSON-comment workaround strings (since JSON has no native comments). They're harmless metadata that the resolver ignores (anything not in REQUIRED_CAPS is just dictionary noise to Python `dict.get`).

- [ ] **Step 2: Validate JSON syntactically**

Run: `python3 -c "import json; json.load(open('claude/capabilities/defaults.json'))"`
Expected: no output (silent success). Any output = JSON parse error.

- [ ] **Step 3: Run capability test to verify it now passes**

Run: `python3 -m unittest tests.smoke.test_capability.RegistryDefaults.test_all_required_capabilities_present -v`
Expected: PASS (`OK`)

- [ ] **Step 4: Run full capability test suite**

Run: `python3 -m unittest tests.smoke.test_capability -v`
Expected: All tests PASS.

- [ ] **Step 5: Commit**

```bash
git add tests/smoke/test_capability.py claude/capabilities/defaults.json
git commit -m "feat(capabilities): expand registry 14 → 33 (v0.6 spec Task 1-2)

19 new capabilities across Phase 1/2/3/4 + cross-cutting per
docs/specs/2026-05-05-capability-registry-v0.6-design.md.

Phase 1: multi_step_plan, dev_setup
Phase 2: subagent_discipline, execute_plan_discipline,
         systematic_debug, deep_investigate, land_and_deploy
Phase 3: verify_completion (MANDATORY), code_review_small,
         code_review_large, review_request_etiquette, pre_land_review,
         quality_health, perf_baseline, post_deploy_qa
Phase 4: branch_finish, changelog_gen
Cross-cutting: safety_guardrails, weekly_retro

verify_completion has no skip_if_not_available — superpowers is baseline.
All gstack-backed capabilities marked skip_if_not_available: true.
review_request_etiquette uses comma-chain like existing deploy_chain.

SKILL.md wiring follows in subsequent commits.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 3: Wire Phase 1 SKILL.md (hat-shift + 2 capabilities)

**Files:**
- Modify: `claude/skills/flow/flow-phase1-plan/SKILL.md`

- [ ] **Step 1: Read the current Phase 1 SKILL.md**

Run: `cat claude/skills/flow/flow-phase1-plan/SKILL.md`
Look for: the section after the base brainstorm invocation (`Invoke {{capability:brainstorm}}`). Note the current line numbers.

- [ ] **Step 2: Add hat-shift section after base brainstorm**

Find the line that says `Invoke `{{capability:brainstorm}}` skill. Follow its protocol:` (around line 17). After the brainstorm protocol section ends (before the next `##` heading), insert:

```markdown

### Optional: perspective-shifted continuation

After base brainstorm completes (prd.md sketched), Flow may propose 0-N perspective-shifted brainstorming rounds. Each round = a new `{{capability:brainstorm}}` invocation with a hat-prompt prefix. Same one-question-at-a-time rhythm as base brainstorm. Output appends to prd.md as "Perspective Critique: <hat>" section.

Available hats (user picks; do not auto-chain all):

- **Engineer hat** — "You are a senior engineer reviewing this plan. Focus on architecture, data flow, edge cases, performance hotspots, and integration points. One question at a time."
- **DX hat** — "You are a developer-experience reviewer. Focus on friction points: setup steps, error messages, CLI ergonomics, time-to-first-value. One question at a time."
- **Security hat** — "You are a security reviewer. Focus on threat surfaces, secret handling, input validation, trust boundaries, and blast radius of destructive operations. One question at a time."

When to use which hat:
- Backend / API tasks → Engineer + Security
- CLI / SDK / dev tooling → Engineer + DX
- Anything touching prod / migrations / auth → add Security (regardless of task type)
- (Note: CEO / market-fit perspective intentionally not offered — Flow assumes user has validated commercial viability before reaching Phase 1)
```

- [ ] **Step 3: Add multi_step_plan placeholder**

In the same Phase 1 SKILL.md, find an appropriate location (near the top or in a "B/C-size tasks" section). Add:

```markdown

### B/C-size tasks: file-protocol planning

For tasks projected at multi-day / multi-file (体量 B 或 C per Flow triage), invoke `{{capability:multi_step_plan}}` after base brainstorm. This creates `task_plan.md`, `findings.md`, `progress.md` per Manus-style protocol — complementary to brainstorming's prd.md (prd.md = WHAT, task_plan.md = stepwise HOW).
```

- [ ] **Step 4: Add dev_setup placeholder for deploy tasks**

Find the area covering UI / deploy / task-type branching. If no such section exists, add at the end of Phase 1:

```markdown

### Deploy task type — first-time setup

If the triage classifies this task as `deploy`, and `.flow/config.yaml` does not yet have a `deploy_target` field, invoke `{{capability:dev_setup}}` to detect platform (Fly/Render/Vercel/Netlify/Heroku/GitHub Actions/custom) and write deploy config. One-time per project.
```

- [ ] **Step 5: Run render to verify no syntax errors**

Run: `python3 scripts/flow.py install render-prompts`
Expected: success message, no leftover `{{capability:X}}` warnings.

- [ ] **Step 6: Verify rendered file contains expected new content**

Run: `grep -E 'Engineer hat|Security hat|multi_step_plan|dev_setup' ~/.claude/skills/flow/flow-phase1-plan/SKILL.md`
Expected: 4 matches (one per term in the rendered file).

- [ ] **Step 7: Commit**

```bash
git add claude/skills/flow/flow-phase1-plan/SKILL.md
git commit -m "feat(phase1): hat-shifted brainstorming + multi_step_plan + dev_setup wiring

Adds optional perspective-shifted brainstorming continuation
(Engineer / DX / Security hats) using superpowers:brainstorming
re-invocation rather than gstack:plan-*-review (which dumps
list-style output and breaks the one-question-at-a-time rhythm).

Adds wiring for {{capability:multi_step_plan}} (B/C tasks via
planning-with-files) and {{capability:dev_setup}} (deploy task
type via gstack:setup-deploy).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 4: Wire Phase 2 SKILL.md (5 capabilities)

**Files:**
- Modify: `claude/skills/flow/flow-phase2-execute/SKILL.md`

- [ ] **Step 1: Read current Phase 2 SKILL.md**

Run: `cat claude/skills/flow/flow-phase2-execute/SKILL.md`
Note: existing wires for `parallel_dispatch`, `tdd`, `behavioral_guidelines`, `cross_model_challenge` (line ~133 for challenge).

- [ ] **Step 2: Add execute_plan_discipline + subagent_discipline near existing parallel_dispatch (line ~39)**

Find the `parallel-subagents` table row mentioning `{{capability:parallel_dispatch}}`. Add immediately after the table (or in a new paragraph following it):

```markdown

When dispatching sub-agents, **also** invoke `{{capability:subagent_discipline}}` for prompt + return-contract conventions (parallel_dispatch handles the orchestration; subagent_discipline handles the per-agent contract).

When an implementation plan exists (from `{{capability:multi_step_plan}}` in Phase 1), invoke `{{capability:execute_plan_discipline}}` to follow it task-by-task with checkpoint commits.
```

- [ ] **Step 3: Add systematic_debug + deep_investigate escalation chain**

Find the section covering "stuck on a bug" or add a new debug section after the existing implement guidance:

```markdown

### When stuck on a bug

1. **First touch** — invoke `{{capability:systematic_debug}}` (iron-law 4-phase root-cause discipline). Don't reach for fixes; identify the root cause first.
2. **Escalate if insufficient** — if systematic_debug isn't producing the answer after 1-2 cycles, invoke `{{capability:deep_investigate}}` for the heavier debugging pipeline.
3. **Last resort** — `{{capability:cross_model_challenge}}` (mode={{capability:cross_model_challenge.args.mode}}) for adversarial cross-model attack on assumptions.
```

- [ ] **Step 4: Add land_and_deploy alt path for deploy tasks**

Find any deploy-task-specific section, or add at the end:

```markdown

### Deploy task type — execution path

Two alternative paths (user picks based on confidence):

- **`{{capability:deploy_chain}}`** (default) — separate `ship` then `canary` monitoring then manual land. Use for **large features** where you want to observe canary metrics before merging.
- **`{{capability:land_and_deploy}}`** — one-shot merge + deploy + canary verify. Use for **small confident changes** where round-tripping through observation feels like overhead.

Do not auto-pick. Surface the choice to the user.
```

- [ ] **Step 5: Render + verify**

Run: `python3 scripts/flow.py install render-prompts && grep -cE 'execute_plan_discipline|subagent_discipline|systematic_debug|deep_investigate|land_and_deploy' ~/.claude/skills/flow/flow-phase2-execute/SKILL.md`
Expected: 5 (one per term, exactly matching what we added).

- [ ] **Step 6: Commit**

```bash
git add claude/skills/flow/flow-phase2-execute/SKILL.md
git commit -m "feat(phase2): wire 5 capabilities (debug chain + plan discipline + deploy alt)

- subagent_discipline pairs with existing parallel_dispatch
- execute_plan_discipline pairs with Phase 1 multi_step_plan
- systematic_debug → deep_investigate escalation chain
- land_and_deploy as alt to deploy_chain (user picks by confidence)

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 5: Wire Phase 3 SKILL.md (8 capabilities) — biggest task

**Files:**
- Modify: `claude/skills/flow/flow-phase3-finish/SKILL.md`

- [ ] **Step 1: Read current Phase 3 SKILL.md**

Run: `cat claude/skills/flow/flow-phase3-finish/SKILL.md`
Note: existing wires for `cross_model_review` (line ~53), `ui_audit` and `ui_visual_review` (line ~55).

- [ ] **Step 2: Add MANDATORY verify_completion at Phase 3 entry**

Find the very first action of Phase 3 (likely after the `## Steps` or similar heading). Insert as the new first step:

```markdown

### Step 0 (MANDATORY): Verify before claiming done

Before any other Phase 3 action, invoke `{{capability:verify_completion}}`. This skill enforces:
- Run actual verification commands (tests, lint, type-check, smoke tests)
- Confirm output matches expected before claiming success
- Do not assert "tests pass" — show the test runner output

This is non-skippable. The capability has no `skip_if_not_available` flag because superpowers is a baseline plugin.
```

- [ ] **Step 3: Add quality_health gate after verification**

After the verify_completion step, add:

```markdown

### Step 1: Quality gate (gstack-dependent)

If gstack is installed, invoke `{{capability:quality_health}}` for a composite 0-10 quality score (typecheck + lint + tests + dead code). Treat scores < 7 as a Phase 3 blocker — investigate and fix before proceeding to review.

If gstack is not installed, this capability is skipped (`skip_if_not_available: true`). Manual quality check the user judges sufficient.
```

- [ ] **Step 4: Add diff-size routing for code review**

Find the existing code-review section (or add new). Replace / add:

```markdown

### Step 2: Code review (size-based routing)

Determine diff size: `git diff --stat <base>..HEAD | tail -1`

- **Diff < 200 lines** → invoke `{{capability:code_review_small}}` (5 Sonnet parallel + Haiku confidence scoring; daily-driver)
- **Diff ≥ 200 lines OR multi-module** → invoke `{{capability:code_review_large}}` (6-specialist agent panel; high coverage)

Threshold configurable in `.flow/config.yaml` via `phases.check.review_size_threshold` (default 200).

In **either** case, **also** invoke `{{capability:review_request_etiquette}}` for the discipline of:
- HOW to request (clear scope, what to look at)
- HOW to process feedback (verify before agreeing — don't blindly accept all suggestions)
```

- [ ] **Step 5: Add pre_land_review for high-risk diffs**

After the code review section:

```markdown

### Step 3: Pre-land review (conditional)

If the diff includes any of: SQL migrations, LLM prompt changes, conditional side-effects (feature flags, environment-dependent code paths), invoke `{{capability:pre_land_review}}` for specialist patterns the general reviewers miss.
```

- [ ] **Step 6: Add perf_baseline for perf-sensitive tasks**

After pre_land_review:

```markdown

### Step 4: Performance baseline (conditional)

If the task touches hot paths or critical user flows, invoke `{{capability:perf_baseline}}` for Web Vitals + resource size regression compare against the previous baseline. Pairs with quality_health (one for code quality, the other for runtime performance).
```

- [ ] **Step 7: Add post_deploy_qa for deploy tasks**

After perf_baseline:

```markdown

### Step 5: Post-deploy QA (deploy task only)

For deploy task types, after ship completes invoke `{{capability:post_deploy_qa}}` for active QA on the deployed site (clicks login, fills forms). Complements canary's passive monitoring — canary alerts on failure; post_deploy_qa actively verifies success.
```

- [ ] **Step 8: Render + verify**

Run: `python3 scripts/flow.py install render-prompts && grep -cE 'verify_completion|code_review_small|code_review_large|review_request_etiquette|pre_land_review|quality_health|perf_baseline|post_deploy_qa' ~/.claude/skills/flow/flow-phase3-finish/SKILL.md`
Expected: 8 or more (capability terms, may appear multiple times if cross-referenced).

- [ ] **Step 9: Commit**

```bash
git add claude/skills/flow/flow-phase3-finish/SKILL.md
git commit -m "feat(phase3): wire 8 capabilities (verify_completion mandatory + review routing + qa)

Closes the security-class gap where Phase 3 allowed self-reported
'done' without actual verification. verify_completion is now Step 0
and non-skippable.

Adds quality_health gate, diff-size code review routing
(small/large), review_request_etiquette discipline, pre_land_review
for high-risk diffs (SQL/LLM/side-effects), perf_baseline for
hot-path tasks, post_deploy_qa for deploy task active verification.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 6: Wire Phase 4 SKILL.md (2 capabilities)

**Files:**
- Modify: `claude/skills/flow/flow-phase4-sediment/SKILL.md`

- [ ] **Step 1: Read current Phase 4 SKILL.md**

Run: `cat claude/skills/flow/flow-phase4-sediment/SKILL.md`
Note: existing wire for `session_save` (line ~90).

- [ ] **Step 2: Add branch_finish at Phase 4 entry**

Find the start of Phase 4 actions. Add as new first step:

```markdown

### Step 1: Decide what to do with the development branch

Before sediment writing, invoke `{{capability:branch_finish}}` for structured options:
- **Merge to base** (default for completed features)
- **Create PR** (requires team review)
- **Cleanup** (abandoned exploration)

This decision shapes downstream sediment scope (e.g. ADR-worthy decisions only when merging).
```

- [ ] **Step 3: Add changelog_gen after ship**

Find the section around release / publishing. Add:

```markdown

### After ship — auto-generate changelog

If the task culminated in a ship (new commits to base branch + version bump), invoke `{{capability:changelog_gen}}` to generate a user-facing changelog entry from the commit history. Validate output before committing — gstack:changelog-generator may overwrite or append to existing CHANGELOG.md depending on its detection heuristics.
```

- [ ] **Step 4: Render + verify**

Run: `python3 scripts/flow.py install render-prompts && grep -cE 'branch_finish|changelog_gen' ~/.claude/skills/flow/flow-phase4-sediment/SKILL.md`
Expected: 2.

- [ ] **Step 5: Commit**

```bash
git add claude/skills/flow/flow-phase4-sediment/SKILL.md
git commit -m "feat(phase4): wire branch_finish + changelog_gen

branch_finish at entry — decide merge/PR/cleanup before sediment.
changelog_gen after ship — auto-generate user-facing changelog
from commit history (validate output before flipping default).

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 7: Wire flow-orchestrator/SKILL.md (cross-cutting 2 capabilities)

**Files:**
- Modify: `claude/skills/flow/flow-orchestrator/SKILL.md`

- [ ] **Step 1: Read current orchestrator SKILL.md**

Run: `cat claude/skills/flow/flow-orchestrator/SKILL.md`
Find the section structure — likely has "## Phases" or "## Triage". Identify a good location for cross-cutting addition (typically near the end before "## References" or similar).

- [ ] **Step 2: Add cross-cutting section**

At the appropriate location (after phase descriptions, before any final references):

```markdown

## Cross-cutting capabilities (any phase)

These capabilities are not phase-specific — invoke them whenever the trigger pattern matches, regardless of which Flow phase you're in.

### Safety guardrails

Before any **destructive operation** in any phase, invoke `{{capability:safety_guardrails}}`. Destructive includes:

- File system: `rm -rf`, `git clean -fd`, `git reset --hard`
- Database: `DROP TABLE`, `TRUNCATE`, `DELETE FROM` without WHERE, migrations
- Source control: `git push --force`, `git branch -D`, force-push to shared branches
- Infrastructure: `kubectl delete`, `terraform destroy`, removing prod resources

This capability provides destructive-command warnings the user can override per-occurrence. Hook-based auto-fire (so the user never has to remember to invoke) is deferred to v0.7 — for now, the orchestrator (you) is responsible for triggering it before the destructive command.

### Weekly retrospective

User-triggered (or scheduled via `/loop weekly /flow:retro`). Invoke `{{capability:weekly_retro}}` for cross-task weekly review:
- All commits across the week
- Work patterns (parallel vs serial, big PRs vs small)
- Quality trends (test coverage delta, type errors over time)

This complements Phase 4 sediment which is per-task — retro is per-week, fills the gap between task-local learnings and project-level direction.
```

- [ ] **Step 3: Render + verify**

Run: `python3 scripts/flow.py install render-prompts && grep -cE 'safety_guardrails|weekly_retro' ~/.claude/skills/flow/flow-orchestrator/SKILL.md`
Expected: 2 or more.

- [ ] **Step 4: Commit**

```bash
git add claude/skills/flow/flow-orchestrator/SKILL.md
git commit -m "feat(orchestrator): wire cross-cutting safety_guardrails + weekly_retro

safety_guardrails: orchestrator-invoked before destructive ops
(rm -rf / DROP TABLE / force-push / kubectl delete / migrations).
Hook-based auto-fire deferred to v0.7.

weekly_retro: cross-task review user-triggered or scheduled,
complements per-task Phase 4 sediment.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 8: Update docs/Skills-Phase映射.md

**Files:**
- Modify: `docs/Skills-Phase映射.md:310-331` (closing audit section)

- [ ] **Step 1: Update the closing audit table**

Find the "v0.2 没整合的（重大缺失）" section (around line 324). Replace with:

```markdown
**v0.6.0 新整合的**：
- ✅ 19 个 capability 全部进 registry（详见 `docs/specs/2026-05-05-capability-registry-v0.6-design.md`）
- ✅ Phase 3 verify_completion 必触（关闭 "false done" 安全口子）
- ✅ Phase 1 hat-shifted brainstorming（Engineer / DX / Security 视角）
- ✅ Phase 3 code review 按 diff size 路由（small / large）
- ✅ Cross-cutting safety_guardrails + weekly_retro
- ✅ Deploy 任务 dev_setup + land_and_deploy + post_deploy_qa 全链
- ✅ Phase 4 changelog_gen + branch_finish

**v0.6.0 仍未整合（推 v0.7）**：
- ❌ 安全护栏 hook 自动触发（safety_guardrails 当前是文档触发；v0.7 加 Bash hook）
- ❌ release_docs（gstack:document-release）
- ❌ project_learnings（gstack:learn —— 等 /flow:promote 重叠问题厘清）
- ❌ security_audit / cso（等 task-type tagging）
- ❌ 文档输出类（docx / pptx / pdf —— 任务类型扩展时引入）
- ❌ 内容 / 发布类（baoyu-* —— 内容创作非 Flow 主线）

**结论**：v0.6.0 是"代码任务全链 + UI 任务覆盖 + 部署任务全链"。v0.7 关注 hook-based 自动化与跨项目 learning 沉淀。
```

- [ ] **Step 2: Commit**

```bash
git add docs/Skills-Phase映射.md
git commit -m "docs(skills-map): update closing audit for v0.6.0 capability expansion

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 9: Bump VERSION + update CHANGELOG

**Files:**
- Modify: `VERSION`
- Modify: `CHANGELOG.md`

- [ ] **Step 1: Bump VERSION**

Replace `0.5.9` with `0.6.0`:

```
0.6.0
```

- [ ] **Step 2: Add CHANGELOG entry at top (above v0.5.9)**

Insert immediately after the `# Changelog` heading:

```markdown
## v0.6.0 (2026-05-05)

Capability registry expansion — wires 19 new capabilities from gstack /
superpowers / pr-review-toolkit / planning-with-files / code-review into
Flow's per-phase orchestration. Capability count grows 14 → 33. Phase 1
gains hat-shifted brainstorming (Engineer / DX / Security perspectives)
that replaces gstack:plan-*-review's batched output with one-question-
at-a-time UX consistent with `superpowers:brainstorming`.

### Added — Phase 1 (2 capabilities + hat-shift)

- `multi_step_plan` → `planning-with-files:plan` (B/C-size tasks)
- `dev_setup` → `gstack:setup-deploy` (deploy task initialization)
- Phase 1 SKILL.md hat-shifted brainstorming continuation
  (Engineer / DX / Security hats; user picks 0-N; same one-question-
  at-a-time rhythm as base brainstorm)

### Added — Phase 2 (5 capabilities)

- `subagent_discipline` → `superpowers:subagent-driven-development`
  (pairs with parallel_dispatch — discipline + orchestration)
- `execute_plan_discipline` → `superpowers:executing-plans`
  (closes loop with multi_step_plan)
- `systematic_debug` → `superpowers:systematic-debugging`
  (4-phase root-cause discipline; first-line debug)
- `deep_investigate` → `gstack:investigate`
  (escalation when systematic_debug insufficient)
- `land_and_deploy` → `gstack:land-and-deploy`
  (alt to deploy_chain; one-shot for small confident changes)

### Added — Phase 3 (8 capabilities)

- **`verify_completion`** → `superpowers:verification-before-completion`
  **MANDATORY at Phase 3 entry — closes a security-class gap where
  Flow previously allowed self-reported success without actual
  verification. Non-skippable.**
- `code_review_small` → `code-review:code-review`
  (5 Sonnet parallel + Haiku confidence; diff < 200 lines)
- `code_review_large` → `pr-review-toolkit:review-pr`
  (6-specialist agent panel; diff ≥ 200 lines)
- `review_request_etiquette` →
  `superpowers:requesting-code-review,superpowers:receiving-code-review`
  (request scope discipline + verify-before-agreeing)
- `pre_land_review` → `gstack:review`
  (SQL safety / LLM trust / conditional side effects)
- `quality_health` → `gstack:health`
  (composite 0-10 quality score; Phase 3 entry gate)
- `perf_baseline` → `gstack:benchmark`
  (Web Vitals + resource size regression; perf-sensitive tasks)
- `post_deploy_qa` → `gstack:qa`
  (active deployed-site QA; complements canary's passive monitoring)

### Added — Phase 4 (2 capabilities)

- `branch_finish` → `superpowers:finishing-a-development-branch`
  (structured merge / PR / cleanup decision)
- `changelog_gen` → `gstack:changelog-generator`
  (auto-generate user-facing changelog from commit history)

### Added — Cross-cutting (2 capabilities)

- `safety_guardrails` → `gstack:careful`
  (destructive command warnings — orchestrator invokes before
  rm -rf / DROP TABLE / force-push / kubectl delete / migrations.
  Hook-based auto-fire deferred to v0.7)
- `weekly_retro` → `gstack:retro`
  (cross-task weekly review; user-triggered or `/loop weekly`)

### Out of scope (rejected during design)

- `plan_ceo_critique` (gstack:plan-ceo-review) — user opted out
- `autoplan` (gstack:autoplan) — bundles all 4 plan-*-review
- `plan_eng_critique` / `plan_devex_critique` — replaced by hat-shift
- `release_docs` / `project_learnings` / `security_audit` /
  `silent-failure-hunter` — deferred to v0.7

### Migration

Pure additive — no existing capability removed or renamed. Project-level
overrides in `.flow/config.local.yaml` continue to work. Re-run
`flow install render-prompts` after upgrade to substitute new
`{{capability:X}}` placeholders into `~/.claude/{commands,skills}/flow/`.

### Tests

`tests/smoke/test_capability.py` REQUIRED_CAPS extended with 19 new
entries. Verifies all 33 capabilities resolve to known skill specs.

```

- [ ] **Step 3: Commit**

```bash
git add VERSION CHANGELOG.md
git commit -m "release: bump VERSION 0.5.9 → 0.6.0

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"
```

---

## Task 10: Verify + render + selftest

- [ ] **Step 1: Re-render all prompts (defensive)**

Run: `python3 scripts/flow.py install render-prompts`
Expected: success, all rendered files updated.

- [ ] **Step 2: Run full selftest**

Run: `python3 scripts/flow_selftest.py`
Expected: `>> Self-test PASSED — install is functional.`
If fails: investigate the specific failure and fix before proceeding. Do NOT proceed to release on failed selftest.

- [ ] **Step 3: Run capability tests**

Run: `python3 -m unittest tests.smoke.test_capability -v`
Expected: All tests PASS.

- [ ] **Step 4: Smoke check — verify_completion enforcement**

Open the rendered Phase 3 SKILL.md:
Run: `head -30 ~/.claude/skills/flow/flow-phase3-finish/SKILL.md`
Expected output contains "Step 0 (MANDATORY): Verify before claiming done" and the substituted skill name `superpowers:verification-before-completion`.

- [ ] **Step 5: Smoke check — safety_guardrails docs**

Run: `grep -A3 'safety_guardrails' ~/.claude/skills/flow/flow-orchestrator/SKILL.md`
Expected: cross-cutting section with destructive op list visible.

- [ ] **Step 6: Smoke check — Phase 1 hat-shift**

Run: `grep -B1 -A3 'Engineer hat\|Security hat' ~/.claude/skills/flow/flow-phase1-plan/SKILL.md`
Expected: both hats present in rendered output.

- [ ] **Step 7: No commit needed** (verification step only)

If any smoke check fails, return to the relevant Task and fix.

---

## Task 11: Tag + push + GitHub release

- [ ] **Step 1: Verify clean working tree**

Run: `git status`
Expected: `nothing to commit, working tree clean`. If not, address uncommitted work first.

- [ ] **Step 2: Verify all v0.6.0 commits pushed-ready**

Run: `git log --oneline origin/master..HEAD`
Expected: 9 commits (Tasks 2, 3, 4, 5, 6, 7, 8, 9 each = 1 commit; Task 1 has no commit).

- [ ] **Step 3: Push commits to origin**

Run: `git push origin master`
Expected: success.

- [ ] **Step 4: Create + push v0.6.0 tag**

```bash
git tag -a v0.6.0 HEAD -m "v0.6.0 — capability registry expansion 14 → 33"
git push origin v0.6.0
```
Expected: tag creation + push success.

- [ ] **Step 5: Create GitHub release as latest**

```bash
gh release create v0.6.0 --latest \
  --title "v0.6.0 — capability registry expansion (14 → 33)" \
  --notes "$(awk '/^## v0\.6\.0/,/^## v0\.5\.9/' CHANGELOG.md | sed '$d')"
```

This pulls the v0.6.0 changelog block (between v0.6.0 heading and v0.5.9 heading) and uses it as release notes.

- [ ] **Step 6: Verify release published**

Run: `gh release list --limit 3`
Expected: `v0.6.0 — capability registry expansion (14 → 33)  Latest  v0.6.0  <today's timestamp>`.

---

## Self-Review (post-plan, pre-execute)

After executing the plan above, the implementer should verify:

**Spec coverage check** — for each spec section, point to the implementing task:
- Spec "Phase 1 (2 adds)" → Task 2 (registry) + Task 3 (SKILL.md wiring)
- Spec "Phase 2 (5 adds)" → Task 2 + Task 4
- Spec "Phase 3 (8 adds)" → Task 2 + Task 5
- Spec "Phase 4 (2 adds)" → Task 2 + Task 6
- Spec "Cross-cutting (2 adds)" → Task 2 + Task 7
- Spec "Phase 1 SKILL.md hat-shift" → Task 3
- Spec "Migration plan" 12 steps → Tasks 2-11 cover all
- Spec "Open questions" → Q5/Q6 resolved in pre-flight; Q1-Q4, Q7-Q8 surface during implementation

**Type / name consistency check** — capability names used:
- All 19 names listed in Task 2 Step 1 match exactly with Tasks 3-7 SKILL.md `{{capability:X}}` references
- All 19 names match Task 1's REQUIRED_CAPS additions
- Task 8 closing audit references same names

**Placeholder scan** — no TBD / TODO / "implement later" anywhere ✓

---

## Execution handoff

Plan complete and saved to `docs/plans/2026-05-05-capability-registry-v0.6-implementation.md`. Two execution options:

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration. Each subagent gets one task spec and returns when complete.

**2. Inline Execution** — Execute tasks in this session using `superpowers:executing-plans`, batch execution with checkpoints for review.

**Which approach?**
