---
description: "Finish current task — final verify + Phase 4 sediment + auto-save + archive"
---

# /flow:finish

Complete current Flow task end-to-end: Phase 3 final verify → Phase 4 sediment → auto-save → archive.

## Protocol

```bash
CURRENT=$(cat .flow/.current-task 2>/dev/null)
[ -z "$CURRENT" ] && { echo "No active task"; exit; }
```

## Step 1 — Phase 3 final verify (if not done)

If `## Verify Report` in progress.md is empty:
- Dispatch fresh-context verify sub-agent (Sonnet)
- Run lint / typecheck / tests / credential grep
- If task qualifies: run `gstack:codex` review
- If UI: `impeccable:audit` + `polish` + `gstack:design-review`
- Write `## Verify Report`

## Step 2 — Commit (if dirty tree)

Check `git status --porcelain`. If dirty:
- Draft commit message based on prd.md + execute log
- Show plan to user for one-shot confirm
- On confirm: `git add` + `git commit` (no amend)
- On reject: stop and let user commit manually

## Step 3 — Phase 4 sediment

Write `## Sediment Notes` section with these decisions:

1. **ADR worth keeping?** → write to `.flow/ADRs/<slug>.md` using `templates/ADR-lite.md.template`
2. **Pattern emerged?** → write to `.flow/patterns/<slug>.md` using `templates/pattern.md.template`
3. **Pitfall to capture?** → write to `.flow/pitfalls/<slug>.md` using `templates/pitfall.md.template`
4. **Cross-project promotion candidates?** → mark in Sediment Notes, defer to `/flow:promote`

**Even if "no new sediment"**: write a one-liner noting that. Forces conscious thought.

## Step 4 — Auto-save

Invoke `yangpeng-claude-skills:save` skill to write session breakpoint.

Also append to `.flow/workspace/${USER}/journal.md`:
```markdown
## ${TASK_SLUG} — ${DATE}
- Title: ${TASK_TITLE}
- Outcome: ${COMMIT_HASH or "no commit"}
- Sediment: ${SEDIMENT_SUMMARY}
- Next: <user-suggested or "none">
```

If `~/projects/flow-framework/scripts/flow_save.py` available, run it for automation.

## Step 5 — Archive

```bash
YEAR_MONTH=$(date +%Y-%m)
mkdir -p .flow/tasks/archive/${YEAR_MONTH}
mv "${CURRENT}" .flow/tasks/archive/${YEAR_MONTH}/
rm .flow/.current-task
```

## Step 6 — Confirm

Tell user:
- Task archived to `.flow/tasks/archive/${YEAR_MONTH}/${TASK_SLUG}/`
- Sediment summary (1-2 lines)
- Next-suggested action (if any)

## Constraints

- **Never amend** existing commits
- **Never skip** Phase 4 sediment (even if "nothing to sediment")
- **Never write** credentials into ADRs / patterns / pitfalls — use `credentials_ref:`
