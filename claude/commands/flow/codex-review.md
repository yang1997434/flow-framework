---
description: "Trigger cross-model review with Codex (GPT-5.5) on current diff"
---

# /flow:codex-review

Manually trigger cross-model review via `{{capability:cross_model_review}}` (mode={{capability:cross_model_review.args.mode}}).

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

## Step 1 — Detect git repo + active changes

```bash
git -C . rev-parse --is-inside-work-tree 2>/dev/null
```

If output is `true` → **git mode** (default). Run:

```bash
git status --porcelain
git diff --stat
```

If no changes: tell user "Nothing to review."

If output is anything else (non-zero, "false", or `git` missing) →
**non-git fallback mode** (Issue #2): the cwd is a regular project,
not a git repo. `codex review --uncommitted` would fail with
"Not inside a trusted directory and --skip-git-repo-check was not specified."
Surface a one-liner to the user:

> Non-git project — running codex exec on full file contents instead of a diff.

Then continue to Step 2 (non-git branch).

## Step 2 — Invoke gstack codex

### git mode (default)

Use `{{capability:cross_model_review}}` skill (mode={{capability:cross_model_review.args.mode}}).

Pass to codex:
- The full git diff (current uncommitted or current branch vs base)
- The current task's prd.md (Acceptance Criteria + Technical Approach)
- Any relevant pitfalls from `.flow/pitfalls/` matching the changed files

### non-git fallback mode

`codex review` requires a git repo. Build a content-based prompt
(prd.md sections + each file in the active task's `## Files Touched`,
or all source files in the task dir + cwd if the section is missing)
and invoke `codex exec --skip-git-repo-check -` instead:

~~~bash
codex exec --skip-git-repo-check - <<'PROMPT'
You are doing a code review. There is no git diff because this project
is not a git repo. Review the full file contents below for bugs,
design issues, missing edge cases, and unclear logic.

# Acceptance Criteria
<paste prd.md "Acceptance Criteria" section>

# Decision (ADR-lite)
<paste prd.md "Decision (ADR-lite)" section, if present>

# Files

## <file path 1>
<paste full contents in a fenced code block tagged with the language>

## <file path 2>
<repeat for each file>
PROMPT
~~~

Pass the same fields as git mode (prd.md sections + pitfalls), just
with file contents instead of a diff.

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
