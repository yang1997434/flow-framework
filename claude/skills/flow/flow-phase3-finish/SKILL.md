---
name: flow-phase3-finish
description: "Use when running Phase 3 of Flow framework — fresh-context verify (Generator/Evaluator), Codex review, commit. Invoked after Phase 2 implementation done. Trigger: 'Phase 3', 'flow:verify', 'finish and commit'."
---

# Flow Phase 3 — Finish (Verify + Commit)

Verify the diff against prd.md + spec, run cross-model review if triggered, commit.

## Step 1 — Final verify (Generator/Evaluator pattern)

**Critical**: dispatch a **fresh-context** sub-agent. NOT main session self-check.

Why: Anthropic research shows agents praise their own work. Fresh context eliminates self-praise bias.

```
Agent(
  subagent_type: "general-purpose",
  model: "sonnet",  # verify = sonnet
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

## Step 2 — Codex cross-model review (if triggered)

**Mandatory triggers**:
- Task labeled `--critical` / `--breaking`
- Diff includes DB migration / public API contract change
- Diff has ≥10 files OR ≥500 lines

**Optional (user can request)**:
- Medium tasks (3-10 files), borderline complex
- After long debugging session

**Skip**:
- Trivial / pure docs / pure tests

If triggered: invoke `gstack:codex` (review mode). Pass full diff + prd.md.

For UI tasks: also invoke `impeccable:audit` + `impeccable:polish` + `gstack:design-review` (real browser visual audit).

## Step 3 — Process verify + review output

Aggregate findings:
- **Must-fix**: address before commit (return to Phase 2 if needed)
- **Should-fix**: address now or note for follow-up
- **Nit**: ignore with rationale

Apply must-fix items → re-run Step 1 fresh-context verify.

## Step 4 — Write Verify Report

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

## Step 5 — Commit

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

## Step 6 — Phase 3 done

Tell user: "Phase 3 complete. `/flow:finish` to run Phase 4 sediment + auto-save + archive."

## Constraints

- **Verify is fresh-context** — no exception. Self-praise bias is documented.
- **Never commit before verify** — even if user pushes
- **Never amend** — three-stage commit flow (work → archive → journal)
- **Never push** without explicit user request
- **Codex review must record outcome** in Verify Report (even if "skipped: reason")
