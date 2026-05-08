---
name: flow-phase3-finish
description: "Use when running Phase 3 of Flow framework — fresh-context verify (Generator/Evaluator), Codex review, commit. Invoked after Phase 2 implementation done. Trigger: 'Phase 3', 'flow:verify', 'finish and commit'."
---

# Flow Phase 3 — Finish (Verify + Commit)

Verify the diff against prd.md + spec, run cross-model review if triggered, commit.

> **Safety**: before any destructive operation in this phase (`git reset --hard`, force-push, `git branch -D`, `git clean -fd`), invoke `{{capability:safety_guardrails}}`. See orchestrator §Cross-cutting capabilities.

### Step 0 (MANDATORY): Verify before claiming done

Before any other Phase 3 action, invoke `{{capability:verify_completion}}`. This skill enforces:
- Run actual verification commands (tests, lint, type-check, smoke tests)
- Confirm output matches expected before claiming success
- Do not assert "tests pass" — show the test runner output

This is non-skippable. The capability has no `skip_if_not_available` flag because superpowers is a baseline plugin.

### Step 0.5 — Verify gate (v0.8.1+)

If `contract.json` defines `acceptance_criteria`, invoke

```bash
flow acceptance --run <slug>
```

This dispatches to `flow_acceptance.AcceptanceRunner` with `phase=3`.
Phase 3 retry rules apply (R2: behavior / e2e / regression NEVER local —
always escalate to `blocked_escalate` per design §1 row 6).

The CLI exits `0` when every criterion evaluates to `PASS`, and `1` on
the first non-PASS criterion (with a `FAIL: criterion <idx> ...`
diagnostic on stderr). Treat exit 1 as a Phase 3 blocker — investigate
and fix before proceeding.

The legacy test + codex-only gate below runs ONLY when
`acceptance_criteria` is empty (backward compat for v0.6 / v0.7 plans
without v0.8.1 contracts).

### Step 1: Quality gate (gstack-dependent)

If gstack is installed, invoke `{{capability:quality_health}}` for a composite 0-10 quality score (typecheck + lint + tests + dead code). Treat scores < 7 as a Phase 3 blocker — investigate and fix before proceeding to review.

If gstack is not installed, this capability is skipped (`skip_if_not_available: true`). Manual quality check the user judges sufficient.

### Step 2: Code review (size-based routing)

Determine diff size: `git diff --stat <base>..HEAD | tail -1`

**BEFORE invoking any reviewer**: invoke the FIRST half of `{{capability:review_request_etiquette}}` — the requesting skill — for request scope discipline (clear scope, what to look at).

Then route by diff size:
- **Diff < 200 lines** → invoke `{{capability:code_review_small}}` (5 Sonnet parallel + Haiku confidence scoring; daily-driver)
- **Diff ≥ 200 lines OR multi-module** → invoke `{{capability:code_review_large}}` (6-specialist agent panel; high coverage)

Threshold configurable in `.flow/config.yaml` via `phases.check.review_size_threshold` (default 200).

**AFTER reviewer responds**: invoke the SECOND half of `{{capability:review_request_etiquette}}` — the receiving skill — for feedback processing discipline (verify before agreeing — don't blindly accept all suggestions).

### Step 3: Pre-land review (conditional)

If the diff includes any of: SQL migrations, LLM prompt changes, conditional side-effects (feature flags, environment-dependent code paths), invoke `{{capability:pre_land_review}}` for specialist patterns the general reviewers miss.

### Step 4: Performance baseline (conditional)

If the task touches hot paths or critical user flows, invoke `{{capability:perf_baseline}}` for Web Vitals + resource size regression compare against the previous baseline. Pairs with quality_health (one for code quality, the other for runtime performance).

### Step 5: Post-deploy QA (deploy task only)

For deploy task types (`Type: deploy` field in prd.md), after ship completes invoke `{{capability:post_deploy_qa}}` for active QA on the deployed site (clicks login, fills forms). Complements canary's passive monitoring — canary alerts on failure; post_deploy_qa actively verifies success.

## Step 6 — Final verify (Generator/Evaluator pattern)

**Critical**: dispatch a **fresh-context** sub-agent. NOT main session self-check.

Why: Anthropic research shows agents praise their own work. Fresh context eliminates self-praise bias.

```
Agent(
  subagent_type: "general-purpose",
  model: "{{model:review}}",
  description: "Fresh-context final verify",
  prompt: """
    You have NO history of this task. You see only:
    - git diff (uncommitted)
    - <task_dir>/prd.md (Acceptance Criteria + DoD)
    
    Tasks:
    1. Does the diff satisfy each Acceptance Criterion? (yes/no per item)
    2. Run: lint, typecheck, tests
    3. Run credential grep:
       grep -rE "(password|secret|api[_-]?key|token).*[:=]\\s*['\"][^'\"]+['\"]" .flow/ ~/data/knowledge-base/
    4. Check Definition of Done items
    
    Return: pass/fail per criterion + issue list.
    DO NOT praise the work. DO NOT skip checks.
  """
)
```

## Step 7 — Codex cross-model review (if triggered)

**Mandatory triggers**:
- Task labeled `--critical` / `--breaking`
- Diff includes DB migration / public API contract change
- Diff has ≥10 files OR ≥500 lines

**Optional (user can request)**:
- Medium tasks (3-10 files), borderline complex
- After long debugging session

**Skip**:
- Trivial / pure docs / pure tests

If triggered: invoke `{{capability:cross_model_review}}` (mode={{capability:cross_model_review.args.mode}}). Pass full diff + prd.md.

For UI tasks: also invoke `{{capability:ui_audit}}` (auto-follows with `{{capability:ui_audit.follow_with}}`) + `{{capability:ui_visual_review}}` (real browser visual audit).

## Step 8 — Process verify + review output

Aggregate findings:
- **Must-fix**: address before commit (return to Phase 2 if needed)
- **Should-fix**: address now or note for follow-up
- **Nit**: ignore with rationale

Apply must-fix items → re-run Step 6 fresh-context verify.

## Step 9 — Write Verify Report

Append to progress.md `## Verify Report`:

```markdown
## Verify Report
- Self-check (fresh-context Sonnet): pass / [issues addressed: ...]
- Cross-model review (Codex GPT-5.5): pass / [N issues addressed: ...] / skipped (reason)
- Lint / typecheck / tests: pass
- Credential grep self-check: pass
- Acceptance Criteria all checked: yes
- Commit hash: <pending>
```

## Step 10 — Commit

Before drafting the commit, re-anchor the three-step delivery rule from `~/.claude/rules/code-delivery.md`:
1. **自测** — run small data through the full pipeline, no errors
2. **审查** — check quality, error handling, edge cases
3. **交付** — only commit after the first two pass

For tool routing on cross-model review (which alternative reviewer to pick), see `~/.claude/rules/code-review.md`. Flow defaults to `{{capability:cross_model_review}}` (codex), but `/code-review` and `/review-pr` are valid alternatives — the rule documents when each fits.

```bash
git status --porcelain
git log --oneline -5  # learn commit message style
git diff --stat
```

Draft commit message based on prd.md + Execute Log. Group changes into logical commits if many files.

Show plan to user for **one-shot confirm**:
```
Proposed commits:
1. feat: <message>
   - <file>
   - <file>

Reply 'ok' to execute. Reply with edits or 'manual' to abort.
```

On confirm:
- `git add <files>` per commit
- `git commit -m "<msg>"` (no amend, no force)
- Don't push automatically

Update Verify Report with final commit hash.

## Step 11 — Phase 3 done

Tell user: "Phase 3 complete. `/flow:finish` to run Phase 4 sediment + auto-save + archive."

## Constraints

- **Verify is fresh-context** — no exception. Self-praise bias is documented.
- **Never commit before verify** — even if user pushes
- **Never amend** — three-stage commit flow (work → archive → journal)
- **Never push** without explicit user request
- **Codex review must record outcome** in Verify Report (even if "skipped: reason")
