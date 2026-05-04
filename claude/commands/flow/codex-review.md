---
description: "Trigger cross-model review with Codex (GPT-5.5) on current diff"
---

# /flow:codex-review

Manually trigger cross-model review via gstack `/codex review`.

## When to use this manual trigger

Phase 3 verify automatically triggers Codex review for:
- critical / breaking changes
- DB migration / API contract change
- ≥10 files OR ≥500 lines

This manual command is for **other** cases where you want a second opinion:
- Borderline complex change (3-10 files, 100-500 lines)
- High-stakes refactor
- Unfamiliar territory
- After a long debugging session, want fresh eyes

## Protocol

## Step 1 — Confirm active diff

```bash
git status --porcelain
git diff --stat
```

If no changes: tell user "Nothing to review."

## Step 2 — Invoke gstack codex

Use `gstack:codex` skill in **review mode**.

Pass to codex:
- The full git diff (current uncommitted or current branch vs base)
- The current task's prd.md (Acceptance Criteria + Technical Approach)
- Any relevant pitfalls from `.flow/pitfalls/` matching the changed files

## Step 3 — Process codex output

Codex returns: pass / fail + specific issues.

For each issue:
- Type: must-fix / should-fix / nit
- Decide: address now / address later / disagree (with reason)

## Step 4 — Update progress.md

Append to `## Verify Report`:
```markdown
- Cross-model review (Codex GPT-5.5): pass / [N issues addressed: ...] / skipped (reason)
```

## Step 5 — Address must-fix items

If must-fix exist: invoke `/flow:continue` to handle them in Phase 2 mode (sub-agent or main session).

## Constraints

- **Don't auto-apply** codex suggestions — surface for user decision
- **Address must-fix** before commit; should-fix can defer with rationale; nit can ignore with rationale
- **Don't skip recording** — even "all clear" gets a line in Verify Report
